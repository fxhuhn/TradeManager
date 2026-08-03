# filename: test_alert_watcher.py
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.services.alert_watcher import (
    AlertState,
    alert_watcher,
    check_dead_orders,
    check_high_slippage,
    order_status_sync_loop,
)


@pytest.fixture
def test_config() -> Config:
    """Fixture providing a mock Config instance."""
    tws = TwsConfig(
        host="127.0.0.1",
        port=7496,
        client_id=0,
        connection_timeout_s=5.0,
        reconnect_initial_delay_s=0.1,
        reconnect_max_attempts=3,
        reconnect_max_delay_s=120.0,
        request_timeout_s=5.0,
        completed_orders_timeout_s=5.0,
        heartbeat_interval_s=60.0,
        heartbeat_timeout_s=5.0,
    )
    app = AppConfig(
        max_retries=3,
        order_rate_limit_s=0.0,
        dead_order_threshold_minutes=15,
        alert_watcher_interval_s=60,
        csv_watcher_interval_s=60,
        order_sync_interval_s=1,
        retry_backoff_base_s=5.0,
        shutdown_join_timeout_s=15.0,
        database_timeout_s=30.0,
        max_csv_size_bytes=5242880,
        log_file_path="data/app.log",
        log_rotation_backup_count=5,
    )
    account = AccountConfig(
        default_limit_pct=0.05,
        margin_multiplier_factor=2.0,
        sizing_mode="margin_adjusted_capital",
        max_margin_usage_pct=0.80,
        min_cushion_pct=0.10,
    )
    telegram = TelegramConfig(
        bot_token="test_token",
        chat_id="test_chat",
        rate_limit_delay_s=0.0,
        request_timeout_s=10.0,
    )
    return Config(
        tws=tws, app=app, account=account, telegram=telegram, strategy_limits={}
    )


@pytest.fixture
async def temp_db():
    """Fixture providing an in-memory SQLite database initialized with schemas."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            trade_group_id TEXT,
            symbol TEXT,
            order_type TEXT,
            status TEXT,
            transmitted_at TEXT,
            bracket_role TEXT,
            account_id TEXT,
            action TEXT,
            quantity INTEGER,
            target_price REAL,
            sec_type TEXT,
            exchange TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE trades_settlement (
            trade_group_id TEXT,
            price_diff_slippage REAL,
            avg_entry_price REAL
        )
        """
    )
    await db.commit()
    yield db
    await db.close()


def test_alert_state_deduplication() -> None:
    """Verifies that AlertState correctly records and reports order and group alerts."""
    # Arrange
    state = AlertState()

    # Act & Assert
    assert not state.is_order_reported(42)
    state.mark_order_reported(42)
    assert state.is_order_reported(42)

    assert not state.is_group_reported("G1")
    state.mark_group_reported("G1")
    assert state.is_group_reported("G1")


@pytest.mark.asyncio
async def test_check_dead_orders_returns_on_weekend(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that check_dead_orders exits immediately on Saturdays or Sundays."""
    # Arrange
    mock_notifier = MagicMock()
    state = AlertState()
    # Saturday, June 6, 2026, 12:00:00 NY Time
    saturday_time = datetime(2026, 6, 6, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    # Act
    with patch("app.services.alert_watcher._fetch_submitted_orders") as mock_fetch:
        await check_dead_orders(
            temp_db, mock_notifier, state, current_time=saturday_time
        )

        # Assert
        mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_check_dead_orders_returns_outside_trading_hours(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that check_dead_orders exits immediately when outside trading hours."""
    # Arrange
    mock_notifier = MagicMock()
    state = AlertState()
    # Thursday, June 4, 2026, 09:29:59 NY Time (Pre-market)
    pre_market_time = datetime(
        2026, 6, 4, 9, 29, 59, tzinfo=ZoneInfo("America/New_York")
    )

    # Act
    with patch("app.services.alert_watcher._fetch_submitted_orders") as mock_fetch:
        await check_dead_orders(
            temp_db, mock_notifier, state, current_time=pre_market_time
        )

        # Assert
        mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_check_dead_orders_handles_database_fetch_exception(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that check_dead_orders logs error and exits cleanly on DB exception."""
    # Arrange
    mock_notifier = MagicMock()
    state = AlertState()
    # Thursday, June 4, 2026, 10:00:00 NY Time (During trading hours)
    trading_time = datetime(2026, 6, 4, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    # Act & Assert (Should not raise exception)
    with patch(
        "app.services.alert_watcher._fetch_submitted_orders",
        side_effect=Exception("DB Corrupted"),
    ):
        await check_dead_orders(
            temp_db, mock_notifier, state, current_time=trading_time
        )


@pytest.mark.asyncio
async def test_check_dead_orders_ignores_empty_or_malformed_transmitted_at(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that orders with empty or malformed transmitted_at dates are ignored."""
    # Arrange
    mock_notifier = MagicMock()
    state = AlertState()
    trading_time = datetime(2026, 6, 4, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    # Malformed and empty transmitted_at orders
    await temp_db.execute(
        "INSERT INTO orders (order_id, trade_group_id, symbol, order_type, status, transmitted_at) "
        "VALUES (101, 'G1', 'AAPL', 'MKT', 'Submitted', '')"
    )
    await temp_db.execute(
        "INSERT INTO orders (order_id, trade_group_id, symbol, order_type, status, transmitted_at) "
        "VALUES (102, 'G1', 'AAPL', 'MKT', 'Submitted', 'invalid-date-format')"
    )
    await temp_db.commit()

    # Act
    await check_dead_orders(temp_db, mock_notifier, state, current_time=trading_time)

    # Assert
    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_dead_orders_triggers_alert_when_threshold_exceeded(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that a dead order alert is sent when an order exceeds the threshold during trading hours."""
    # Arrange
    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)
    state = AlertState()
    # Transmitted at 09:35:00 UTC (05:35:00 NY, pre-market). Effective activation = 09:30:00 NY.
    # Check at 10:00:00 NY. Active duration = 30 minutes > 15 minutes threshold.
    trading_time = datetime(2026, 6, 4, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    await temp_db.execute(
        "INSERT INTO orders (order_id, trade_group_id, symbol, order_type, status, transmitted_at) "
        "VALUES (1, 'G1', 'AAPL', 'MKT', 'Submitted', '2026-06-04 09:35:00')"
    )
    await temp_db.commit()

    # Act
    await check_dead_orders(temp_db, mock_notifier, state, current_time=trading_time)

    # Assert
    mock_notifier.send_message.assert_called_once_with(
        "⚠️ <b>DEAD ORDER</b> | <code>AAPL</code>"
    )
    assert state.is_order_reported(1)


@pytest.mark.asyncio
async def test_check_high_slippage_sends_alert(temp_db: aiosqlite.Connection) -> None:
    """Verifies that high slippage alerts are sent if slippage exceeds the threshold."""
    # Arrange
    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)
    state = AlertState()

    # avg_entry_price = 100.0, price_diff_slippage = 1.05, limit = 1.00 (1%) -> Exceeded
    await temp_db.execute(
        "INSERT INTO orders (order_id, trade_group_id, symbol, bracket_role, status, target_price) "
        "VALUES (1, 'G1', 'AAPL', 'ENTRY', 'Filled', '100.0')"
    )
    await temp_db.execute(
        "INSERT INTO trades_settlement (trade_group_id, price_diff_slippage, avg_entry_price) "
        "VALUES ('G1', 1.05, 100.0)"
    )
    await temp_db.commit()

    # Act
    await check_high_slippage(
        temp_db, mock_notifier, state, max_slippage_percentage=0.01
    )

    # Assert
    mock_notifier.send_message.assert_called_once_with(
        "📉 <b>HIGH SLIPPAGE</b> | <code>AAPL</code>"
    )
    assert state.is_group_reported("G1")


@pytest.mark.asyncio
async def test_check_high_slippage_does_not_alert_within_limits(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that check_high_slippage does not alert if slippage is within limit boundary."""
    # Arrange
    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)
    state = AlertState()

    # avg_entry_price = 100.0, price_diff_slippage = 0.99, limit = 1.00 -> Within limits
    await temp_db.execute(
        "INSERT INTO orders (order_id, trade_group_id, symbol, bracket_role, status, target_price) "
        "VALUES (1, 'G1', 'AAPL', 'ENTRY', 'Filled', '100.0')"
    )
    await temp_db.execute(
        "INSERT INTO trades_settlement (trade_group_id, price_diff_slippage, avg_entry_price) "
        "VALUES ('G1', 0.99, 100.0)"
    )
    await temp_db.commit()

    # Act
    await check_high_slippage(
        temp_db, mock_notifier, state, max_slippage_percentage=0.01
    )

    # Assert
    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_high_slippage_handles_zero_avg_entry_price(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that check_high_slippage ignores trades with zero entry price to prevent divisions or logic bugs."""
    # Arrange
    mock_notifier = MagicMock()
    state = AlertState()

    await temp_db.execute(
        "INSERT INTO orders (order_id, trade_group_id, symbol, bracket_role, status) "
        "VALUES (1, 'G1', 'AAPL', 'ENTRY', 'Filled')"
    )
    await temp_db.execute(
        "INSERT INTO trades_settlement (trade_group_id, price_diff_slippage, avg_entry_price) "
        "VALUES ('G1', 1.00, 0.0)"
    )
    await temp_db.commit()

    # Act
    await check_high_slippage(
        temp_db, mock_notifier, state, max_slippage_percentage=0.01
    )

    # Assert
    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_high_slippage_skips_market_orders(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that check_high_slippage skips orders with target_price=0.0 or NULL (e.g. MKT/MOC/MOO)."""
    # Arrange
    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)
    state = AlertState()

    await temp_db.execute(
        "INSERT INTO orders (order_id, trade_group_id, symbol, bracket_role, status, target_price) "
        "VALUES (1, 'G1', 'AAPL', 'ENTRY', 'Filled', '0.0')"
    )
    await temp_db.execute(
        "INSERT INTO trades_settlement (trade_group_id, price_diff_slippage, avg_entry_price) "
        "VALUES ('G1', 150.0, 150.0)"
    )
    await temp_db.commit()

    # Act
    await check_high_slippage(
        temp_db, mock_notifier, state, max_slippage_percentage=0.01
    )

    # Assert
    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_high_slippage_handles_database_exception(
    temp_db: aiosqlite.Connection,
) -> None:
    """Verifies that check_high_slippage catches database query exceptions gracefully."""
    # Arrange
    mock_notifier = MagicMock()
    state = AlertState()

    # Act & Assert (Should not raise exception)
    with patch.object(temp_db, "execute", side_effect=Exception("Database locked")):
        await check_high_slippage(temp_db, mock_notifier, state)


@pytest.mark.asyncio
async def test_alert_watcher_service_loop_run(test_config: Config) -> None:
    """Verifies that the alert_watcher loop correctly starts, executes once, and handles cancellations."""
    # Arrange
    mock_db = MagicMock()
    mock_db.close = AsyncMock()

    db_factory = AsyncMock(return_value=mock_db)
    mock_notifier = MagicMock()

    # Act & Assert
    with (
        patch(
            "app.services.alert_watcher.check_dead_orders", new_callable=AsyncMock
        ) as mock_dead,
        patch(
            "app.services.alert_watcher.check_high_slippage", new_callable=AsyncMock
        ) as mock_slippage,
    ):
        watcher_task = asyncio.create_task(
            alert_watcher(
                db_factory=db_factory,
                notifier=mock_notifier,
                config=test_config,
                interval_seconds=1,
            )
        )
        # Yield to let it execute once
        await asyncio.sleep(0.05)
        watcher_task.cancel()

        try:
            await watcher_task
        except asyncio.CancelledError:
            pass

        mock_dead.assert_called()
        mock_slippage.assert_called()
        mock_db.close.assert_called()


@pytest.mark.asyncio
async def test_order_status_sync_loop_run(test_config: Config) -> None:
    """Verifies that the order_status_sync_loop starts, sleeps first, and handles recovery calls."""
    # Arrange
    mock_db = MagicMock()
    mock_db.close = AsyncMock()

    db_factory = AsyncMock(return_value=mock_db)
    mock_ib = MagicMock()
    mock_queue = asyncio.Queue()
    mock_notifier = MagicMock()

    # Act & Assert
    with patch(
        "app.services.alert_watcher.run_recovery", new_callable=AsyncMock
    ) as mock_recovery:
        sync_task = asyncio.create_task(
            order_status_sync_loop(
                db_factory=db_factory,
                interactive_brokers=mock_ib,
                queue=mock_queue,
                notifier=mock_notifier,
                trigger_settlement_callback=AsyncMock(),
                config=test_config,
                interval_seconds=1,
            )
        )
        # Yield to let it run (note: sync loop sleeps first before executing)
        await asyncio.sleep(1.05)
        sync_task.cancel()

        try:
            await sync_task
        except asyncio.CancelledError:
            pass

        mock_recovery.assert_called()
        mock_db.close.assert_called()
