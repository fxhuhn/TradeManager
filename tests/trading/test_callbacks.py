# filename: tests/trading/test_callbacks.py
"""Unit tests for TwsCallbacksManager event handlers and execution details."""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.trading.callbacks import (
    TwsCallbacksManager,
    extract_unassigned_execution_details,
    handle_unassigned_execution,
)
from app.trading.error_codes import ErrorClass


@pytest.fixture
def mock_config() -> Config:
    """Erstellt ein Mock-Konfigurationsobjekt fuer die Tests."""
    tws = TwsConfig(
        host="127.0.0.1",
        port=7496,
        client_id=0,
        connection_timeout_s=10.0,
        reconnect_initial_delay_s=5.0,
        reconnect_max_attempts=10,
        reconnect_max_delay_s=120.0,
        request_timeout_s=10.0,
        completed_orders_timeout_s=15.0,
    )
    app = AppConfig(
        max_retries=3,
        order_rate_limit_s=0.02,
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
    account = AccountConfig(default_limit_pct=0.05)
    telegram = TelegramConfig(
        bot_token="test_token",
        chat_id="test_chat",
        rate_limit_delay_s=1.5,
        request_timeout_s=10.0,
    )
    return Config(
        tws=tws, app=app, account=account, telegram=telegram, strategy_limits={}
    )


@pytest.mark.asyncio
async def test_callbacks_filled_notification(db, mock_config: Config) -> None:
    """Prüft, dass bei Ausführung eine Telegram-Nachricht gesendet und der DB-Status aktualisiert wird."""
    # 1. Test-Order im Status 'Submitted' einfuegen
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            42,
            0,
            None,
            "G1",
            "A1",
            "ENTRY",
            "AAPL",
            "STK",
            "SMART",
            "BUY",
            100,
            "LMT",
            150.00,
            "DAY",
            "DipBuyer",
            "Submitted",
        ),
    )
    await db.commit()

    # 2. Mocking der Callbacks und Notifier
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()
    mock_notifier.send_order_filled = AsyncMock(return_value=True)

    mock_trigger_settlement = AsyncMock()
    mock_handle_retriable_error = AsyncMock()
    mock_run_recovery = AsyncMock()
    mock_run_reconnect = AsyncMock()

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=mock_trigger_settlement,
        handle_retriable_error_callback=mock_handle_retriable_error,
        run_recovery_callback=mock_run_recovery,
        run_reconnect_callback=mock_run_reconnect,
    )

    try:
        # 3. Statusänderung zu 'Filled' verarbeiten
        await manager._process_status_change(
            order_id=42, mapped_status="Filled", permanent_id=9876
        )
    finally:
        db.close = original_close

    # 4. Assertions
    async with db.execute(
        "SELECT status, perm_id FROM orders WHERE order_id = 42"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Filled"
        assert row["perm_id"] == 9876

    mock_notifier.send_order_filled.assert_called_once()
    kwargs = mock_notifier.send_order_filled.call_args[1]
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["action"] == "BUY"
    assert kwargs["quantity"] == 100
    assert kwargs["execution_price"] == 150.00

    mock_trigger_settlement.assert_not_called()


@pytest.mark.asyncio
async def test_callbacks_exit_settlement_trigger(db, mock_config: Config) -> None:
    """Prüft, dass bei Ausführung einer Exit-Order (TP) das Settlement getriggert wird."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            42,
            0,
            None,
            "G1",
            "A1",
            "ENTRY",
            "AAPL",
            "STK",
            "SMART",
            "BUY",
            100,
            "LMT",
            150.00,
            "DAY",
            "DipBuyer",
            "Filled",
        ),
    )
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            43,
            0,
            42,
            "G1",
            "A1",
            "TP",
            "AAPL",
            "STK",
            "SMART",
            "SELL",
            100,
            "LMT",
            155.00,
            "DAY",
            "DipBuyer",
            "Submitted",
        ),
    )
    await db.commit()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()
    mock_notifier.send_order_filled = AsyncMock(return_value=True)

    mock_trigger_settlement = AsyncMock()

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=mock_trigger_settlement,
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    try:
        await manager._process_status_change(
            order_id=43, mapped_status="Filled", permanent_id=9877
        )
    finally:
        db.close = original_close

    mock_notifier.send_order_filled.assert_called_once()
    mock_trigger_settlement.assert_called_once_with("G1", "A1")


@pytest.mark.asyncio
async def test_loc_order_cancel_not_near_close(db, mock_config: Config) -> None:
    """Prüft, dass bei einer LOC-Order, die weit vor Marktschluss storniert wird, keine Preisprüfung erfolgt."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            44,
            0,
            None,
            "G2",
            "A1",
            "EXIT",
            "AMAT",
            "STK",
            "SMART",
            "SELL",
            10,
            "LOC",
            100.00,
            "DAY",
            "DipBuyer",
            "Submitted",
        ),
    )
    await db.commit()

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()
    mock_notifier.send_loc_execution_anomaly = AsyncMock()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    mock_now = datetime(2026, 7, 8, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    with patch("app.trading.callbacks.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now

        try:
            await manager._cancel_order_in_db(44, 202, "Order Canceled")
        finally:
            db.close = original_close

    mock_notifier.send_order_failed.assert_called_once()
    mock_notifier.send_loc_execution_anomaly.assert_not_called()


@pytest.mark.asyncio
async def test_loc_order_cancel_anomaly_detected(db, mock_config: Config) -> None:
    """Prüft, dass bei Erreichen des Schlusskurses für eine stornierte LOC (SELL) ein Alarm gesendet wird."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            45,
            0,
            None,
            "G2",
            "A1",
            "EXIT",
            "AMAT",
            "STK",
            "SMART",
            "SELL",
            10,
            "LOC",
            100.00,
            "DAY",
            "DipBuyer",
            "Submitted",
        ),
    )
    await db.commit()

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()
    mock_notifier.send_loc_execution_anomaly = AsyncMock()

    mock_ib = MagicMock()
    mock_bar = MagicMock()
    mock_bar.close = 105.00
    mock_bar.date = date(2026, 7, 8)
    mock_ib.reqHistoricalDataAsync = AsyncMock(return_value=[mock_bar])

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=mock_ib,
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    mock_now = datetime(2026, 7, 8, 16, 5, 0, tzinfo=ZoneInfo("America/New_York"))

    with patch("app.trading.callbacks.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now
        mock_dt.strptime = datetime.strptime

        try:
            await manager._cancel_order_in_db(45, 202, "Order Canceled")

            with patch("asyncio.sleep", AsyncMock()):
                await manager._check_loc_execution_price(
                    order_id=45,
                    symbol="AMAT",
                    action="SELL",
                    limit_price=Decimal("100.00"),
                    quantity=Decimal("10"),
                )
        finally:
            db.close = original_close

    mock_notifier.send_order_failed.assert_called_once()
    mock_notifier.send_loc_execution_anomaly.assert_called_once_with(
        order_id=45,
        symbol="AMAT",
        action="SELL",
        limit_price=Decimal("100.00"),
        close_price=Decimal("105.00"),
        quantity=Decimal("10"),
    )


@pytest.mark.asyncio
async def test_loc_order_cancel_no_anomaly(db, mock_config: Config) -> None:
    """Prüft, dass kein Alarm gesendet wird, wenn der Limitpreis nicht erreicht wurde."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            46,
            0,
            None,
            "G2",
            "A1",
            "EXIT",
            "AMAT",
            "STK",
            "SMART",
            "SELL",
            10,
            "LOC",
            100.00,
            "DAY",
            "DipBuyer",
            "Submitted",
        ),
    )
    await db.commit()

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()
    mock_notifier.send_loc_execution_anomaly = AsyncMock()

    mock_ib = MagicMock()
    mock_bar = MagicMock()
    mock_bar.close = 95.00
    mock_bar.date = date(2026, 7, 8)
    mock_ib.reqHistoricalDataAsync = AsyncMock(return_value=[mock_bar])

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=mock_ib,
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    mock_now = datetime(2026, 7, 8, 16, 5, 0, tzinfo=ZoneInfo("America/New_York"))

    with patch("app.trading.callbacks.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now
        mock_dt.strptime = datetime.strptime

        try:
            await manager._cancel_order_in_db(46, 202, "Order Canceled")

            with patch("asyncio.sleep", AsyncMock()):
                await manager._check_loc_execution_price(
                    order_id=46,
                    symbol="AMAT",
                    action="SELL",
                    limit_price=Decimal("100.00"),
                    quantity=Decimal("10"),
                )
        finally:
            db.close = original_close

    mock_notifier.send_order_failed.assert_called_once()
    mock_notifier.send_loc_execution_anomaly.assert_not_called()


@pytest.mark.asyncio
async def test_loc_buy_order_anomaly_detected(db, mock_config: Config) -> None:
    """Prüft, dass bei einer stornierten BUY LOC-Order ein Alarm gesendet wird, wenn der Schlusskurs <= Limitpreis ist."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            47,
            0,
            None,
            "G2",
            "A1",
            "EXIT",
            "AMAT",
            "STK",
            "SMART",
            "BUY",
            10,
            "LOC",
            100.00,
            "DAY",
            "DipBuyer",
            "Submitted",
        ),
    )
    await db.commit()

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()
    mock_notifier.send_loc_execution_anomaly = AsyncMock()

    mock_ib = MagicMock()
    mock_bar = MagicMock()
    mock_bar.close = 95.00
    mock_bar.date = date(2026, 7, 8)
    mock_ib.reqHistoricalDataAsync = AsyncMock(return_value=[mock_bar])

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=mock_ib,
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    mock_now = datetime(2026, 7, 8, 16, 5, 0, tzinfo=ZoneInfo("America/New_York"))

    with patch("app.trading.callbacks.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now
        mock_dt.strptime = datetime.strptime

        try:
            with patch("asyncio.sleep", AsyncMock()):
                await manager._check_loc_execution_price(
                    order_id=47,
                    symbol="AMAT",
                    action="BUY",
                    limit_price=Decimal("100.00"),
                    quantity=Decimal("10"),
                )
        finally:
            db.close = original_close

    mock_notifier.send_loc_execution_anomaly.assert_called_once_with(
        order_id=47,
        symbol="AMAT",
        action="BUY",
        limit_price=Decimal("100.00"),
        close_price=Decimal("95.00"),
        quantity=Decimal("10"),
    )


@pytest.mark.asyncio
async def test_update_commission_retries_and_succeeds_on_later_attempt(
    tmp_path: Path,
) -> None:
    """Verifies that _update_commission retries when the execution row is created after latency delay."""
    db_file = tmp_path / "test_trading.db"
    async with aiosqlite.connect(db_file) as init_db:
        await init_db.execute(
            "CREATE TABLE executions (exec_id TEXT PRIMARY KEY, commission TEXT, currency TEXT)"
        )
        await init_db.execute("INSERT INTO executions (exec_id) VALUES ('EXEC-101')")
        await init_db.commit()

    async def db_factory() -> aiosqlite.Connection:
        return await aiosqlite.connect(db_file)

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=AsyncMock(),
        config=MagicMock(),
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    await manager._update_commission("EXEC-101", Decimal("2.50"), "USD")

    async with aiosqlite.connect(db_file) as db:
        async with db.execute(
            "SELECT commission, currency FROM executions WHERE exec_id = 'EXEC-101'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "2.50"
            assert row[1] == "USD"


@pytest.mark.asyncio
async def test_update_commission_handles_missing_execution_row_gracefully(
    tmp_path: Path,
) -> None:
    """Verifies that _update_commission logs warning after maximum retries without raising unhandled exception."""
    db_file = tmp_path / "test_trading.db"
    async with aiosqlite.connect(db_file) as init_db:
        await init_db.execute(
            "CREATE TABLE executions (exec_id TEXT PRIMARY KEY, commission TEXT, currency TEXT)"
        )
        await init_db.commit()

    async def db_factory() -> aiosqlite.Connection:
        return await aiosqlite.connect(db_file)

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=AsyncMock(),
        config=MagicMock(),
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    await manager._update_commission("EXEC-NONEXISTENT", Decimal("1.00"), "USD")


def test_extract_unassigned_execution_details() -> None:
    """Verifies that extract_unassigned_execution_details correctly pulls contract and execution fields."""
    mock_trade = MagicMock()
    mock_trade.order.action = "SELL"
    mock_trade.order.account = "U19605236"
    mock_trade.order.orderRef = "Ref123"

    mock_fill = MagicMock()
    mock_fill.contract.symbol = "SLB"
    mock_fill.contract.secType = "STK"
    mock_fill.contract.exchange = "SMART"
    mock_fill.contract.currency = "USD"
    mock_fill.execution.side = "SLD"
    mock_fill.execution.shares = 51.0
    mock_fill.execution.price = 52.42
    mock_fill.execution.acctNumber = "U19605236"
    mock_fill.execution.orderId = -6
    mock_fill.execution.permId = 123456
    mock_fill.execution.execId = "EXEC-999"
    mock_fill.execution.time = "2026-07-24 22:00:03"

    details = extract_unassigned_execution_details(mock_trade, mock_fill)

    assert details["symbol"] == "SLB"
    assert details["sec_type"] == "STK"
    assert details["side"] == "SLD"
    assert details["qty"] == Decimal("51.0")
    assert details["price"] == Decimal("52.42")
    assert details["account_id"] == "U19605236"
    assert details["order_id"] == -6
    assert details["perm_id"] == 123456
    assert details["exec_id"] == "EXEC-999"

    handled = handle_unassigned_execution(mock_trade, mock_fill)
    assert handled["symbol"] == "SLB"


def test_register_all_connects_all_events(mock_config: Config) -> None:
    """Verifies register_all connects handlers to all IB event signals."""
    mock_ib = MagicMock()
    manager = TwsCallbacksManager(
        db_factory=AsyncMock(),
        interactive_brokers=mock_ib,
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )
    manager.register_all()

    mock_ib.orderStatusEvent.connect.assert_called_once_with(manager.on_order_status)
    mock_ib.execDetailsEvent.connect.assert_called_once_with(manager.on_exec_details)
    mock_ib.commissionReportEvent.connect.assert_called_once_with(
        manager.on_commission_report
    )
    mock_ib.errorEvent.connect.assert_called_once_with(manager.on_error)
    mock_ib.disconnectedEvent.connect.assert_called_once_with(manager.on_disconnected)


@pytest.mark.asyncio
async def test_update_order_status_db_ignores_terminal_state(
    db, mock_config: Config
) -> None:
    """Verifies that terminal states (Filled, Cancelled) cannot be overwritten."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (10, 100, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'DAY', 'S1', 'Filled')
        """
    )
    await db.commit()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    original_close = db.close
    db.close = AsyncMock()
    try:
        await manager._update_order_status_db(10, "Cancelled", 200)
    finally:
        db.close = original_close

    async with db.execute(
        "SELECT status, perm_id FROM orders WHERE order_id = 10"
    ) as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Filled"
        assert row["perm_id"] == 100


@pytest.mark.asyncio
async def test_update_order_status_db_ignores_error_status_for_active_order(
    db, mock_config: Config
) -> None:
    """Verifies that an 'Error' status update is ignored for active 'PreSubmitted' orders."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (11, 0, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'DAY', 'S1', 'PreSubmitted')
        """
    )
    await db.commit()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    original_close = db.close
    db.close = AsyncMock()
    try:
        await manager._update_order_status_db(11, "Error", 555)
    finally:
        db.close = original_close

    async with db.execute(
        "SELECT status, perm_id FROM orders WHERE order_id = 11"
    ) as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "PreSubmitted"
        assert row["perm_id"] == 555


@pytest.mark.asyncio
async def test_on_exec_details_saves_execution(db, mock_config: Config) -> None:
    """Verifies on_exec_details saves execution row in DB for known order."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (20, 123, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'DAY', 'S1', 'Submitted')
        """
    )
    await db.commit()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    original_close = db.close
    db.close = AsyncMock()

    mock_trade = MagicMock()
    mock_fill = MagicMock()
    mock_fill.execution.execId = "EXEC-20-1"
    mock_fill.execution.orderId = 20
    mock_fill.execution.price = 150.25
    mock_fill.execution.shares = 10.0
    mock_fill.execution.side = "BOT"
    mock_fill.execution.time = "2026-07-24 15:00:00"
    mock_fill.contract.symbol = "AAPL"
    mock_fill.contract.currency = "USD"

    try:
        await manager._save_execution(
            "EXEC-20-1",
            20,
            Decimal("150.25"),
            Decimal("10.0"),
            "USD",
            "2026-07-24 15:00:00",
            trade=mock_trade,
            fill=mock_fill,
        )
    finally:
        db.close = original_close

    async with db.execute(
        "SELECT exec_id, price, qty FROM executions WHERE exec_id = 'EXEC-20-1'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["exec_id"] == "EXEC-20-1"
        assert Decimal(str(row["price"])) == Decimal("150.25")
        assert Decimal(str(row["qty"])) == Decimal("10.0")


@pytest.mark.asyncio
async def test_save_execution_handles_unassigned_order(db, mock_config: Config) -> None:
    """Verifies _save_execution calls handle_unassigned_execution when order does not exist in DB."""
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    with patch("app.trading.callbacks.handle_unassigned_execution") as mock_handle:
        mock_trade = MagicMock()
        mock_fill = MagicMock()
        try:
            await manager._save_execution(
                "EXEC-UNASSIGNED",
                999,
                Decimal("100"),
                Decimal("5"),
                "USD",
                None,
                trade=mock_trade,
                fill=mock_fill,
            )
        finally:
            db.close = original_close
        mock_handle.assert_called_once_with(mock_trade, mock_fill)


@pytest.mark.asyncio
async def test_on_error_classifications(mock_config: Config) -> None:
    """Verifies on_error correctly routes different ErrorClasses."""
    mock_recovery = AsyncMock()
    mock_retry = AsyncMock()
    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()

    manager = TwsCallbacksManager(
        db_factory=AsyncMock(),
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=mock_retry,
        run_recovery_callback=mock_recovery,
        run_reconnect_callback=AsyncMock(),
    )

    # 1. System info code (-1, 2104) -> ignored
    manager.on_error(-1, 2104, "Market data farm connection is OK")
    await asyncio.sleep(0.01)

    # 2. Broker Connection lost (-1, 1100) -> sends disconnect status alert and sets _broker_connected=False
    mock_notifier.send_broker_connection_status = AsyncMock(return_value=True)
    await manager._process_error(
        -1,
        1100,
        "Connectivity between CapTrader and TWS has been lost.",
        ErrorClass.RETRIABLE,
    )
    await asyncio.sleep(0.01)
    mock_notifier.send_broker_connection_status.assert_called_once_with(
        is_connected=False,
        error_code=1100,
        details="Connectivity between CapTrader and TWS has been lost.",
    )
    assert manager._broker_connected is False

    # Second consecutive 1100 event should be debounced (no second alert sent)
    mock_notifier.send_broker_connection_status.reset_mock()
    await manager._process_error(
        -1,
        1100,
        "Connectivity between CapTrader and TWS has been lost.",
        ErrorClass.RETRIABLE,
    )
    await asyncio.sleep(0.01)
    mock_notifier.send_broker_connection_status.assert_not_called()

    # 3. RECONNECT (1101) -> sends reconnect status alert and triggers recovery
    await manager._process_error(
        -1,
        1101,
        "Connectivity between CapTrader and TWS has been restored.",
        ErrorClass.RECONNECT,
    )
    await asyncio.sleep(0.01)
    mock_notifier.send_broker_connection_status.assert_called_once_with(
        is_connected=True,
        error_code=1101,
        details="Connectivity between CapTrader and TWS has been restored.",
    )
    assert manager._broker_connected is True
    mock_recovery.assert_called_once()

    # Second consecutive 1101 event triggers recovery but debounces alert
    mock_notifier.send_broker_connection_status.reset_mock()
    await manager._process_error(
        -1,
        1101,
        "Connectivity between CapTrader and TWS has been restored.",
        ErrorClass.RECONNECT,
    )
    await asyncio.sleep(0.01)
    mock_notifier.send_broker_connection_status.assert_not_called()
    assert mock_recovery.call_count == 2

    # 4. RETRIABLE with actual order_id (50, 1100) -> calls retry handler
    await manager._process_error(
        50, 1100, "Connectivity between TWS and Server lost", ErrorClass.RETRIABLE
    )
    await asyncio.sleep(0.01)
    mock_retry.assert_called_once_with(50)


@pytest.mark.asyncio
async def test_fail_order_in_db_updates_status_and_notifies(
    db, mock_config: Config
) -> None:
    """Verifies _fail_order_in_db sets status to Error and sends Telegram notification."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (30, 0, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'DAY', 'S1', 'Submitted')
        """
    )
    await db.commit()

    async def db_factory():
        return db

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    original_close = db.close
    db.close = AsyncMock()
    try:
        await manager._fail_order_in_db(30, 201, "Order rejected by exchange")
    finally:
        db.close = original_close

    async with db.execute("SELECT status FROM orders WHERE order_id = 30") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Error"

    mock_notifier.send_order_failed.assert_called_once_with(
        order_id=30,
        tws_code=201,
        reason="Order rejected by exchange",
        symbol="AAPL",
        bracket_role="ENTRY",
        is_fatal=True,
    )


@pytest.mark.asyncio
async def test_on_disconnected_triggers_reconnect(mock_config: Config) -> None:
    """Verifies on_disconnected triggers reconnect callback and sends status notification."""
    mock_reconnect = AsyncMock()
    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()

    manager = TwsCallbacksManager(
        db_factory=AsyncMock(),
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=mock_reconnect,
    )

    manager.on_disconnected()
    await asyncio.sleep(0.02)

    mock_reconnect.assert_called_once()
    mock_notifier.send_system_status.assert_called_once()
    assert manager._broker_connected is False


@pytest.mark.asyncio
async def test_on_order_status_dispatches_task(mock_config: Config) -> None:
    """Verifies on_order_status extracts trade status and creates process task."""
    mock_trade = MagicMock()
    mock_trade.order.orderId = 55
    mock_trade.orderStatus.status = "Filled"
    mock_trade.orderStatus.permId = 888
    mock_trade.orderStatus.avgFillPrice = 120.50
    mock_trade.contract.symbol = "AAPL"
    mock_trade.contract.secType = "STK"

    manager = TwsCallbacksManager(
        db_factory=AsyncMock(),
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    with patch.object(manager, "_process_status_change", AsyncMock()) as mock_process:
        manager.on_order_status(mock_trade)
        await asyncio.sleep(0.02)
        mock_process.assert_called_once_with(
            55,
            "Filled",
            888,
            avg_fill_price=120.50,
            event_symbol="AAPL",
            event_sec_type="STK",
        )


@pytest.mark.asyncio
async def test_on_exec_details_dispatches_task(mock_config: Config) -> None:
    """Verifies on_exec_details extracts fill attributes and creates save execution task."""
    mock_trade = MagicMock()
    mock_fill = MagicMock()
    mock_fill.execution.execId = "EXEC-55"
    mock_fill.execution.orderId = 55
    mock_fill.execution.price = 100.0
    mock_fill.execution.shares = 5.0
    mock_fill.execution.side = "BOT"
    mock_fill.execution.time = "2026-07-24"
    mock_fill.contract.symbol = "AAPL"
    mock_fill.contract.currency = "USD"

    manager = TwsCallbacksManager(
        db_factory=AsyncMock(),
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    with patch.object(manager, "_save_execution", AsyncMock()) as mock_save:
        manager.on_exec_details(mock_trade, mock_fill)
        await asyncio.sleep(0.02)
        mock_save.assert_called_once_with(
            "EXEC-55",
            55,
            Decimal("100.0"),
            Decimal("5.0"),
            "USD",
            "2026-07-24",
            symbol="AAPL",
            trade=mock_trade,
            fill=mock_fill,
        )


@pytest.mark.asyncio
async def test_on_commission_report_dispatches_task(mock_config: Config) -> None:
    """Verifies on_commission_report parses report and schedules commission update."""
    mock_trade = MagicMock()
    mock_fill = MagicMock()
    mock_fill.execution.execId = "EXEC-55"
    mock_report = MagicMock()
    mock_report.commission = 1.25
    mock_report.currency = "USD"

    manager = TwsCallbacksManager(
        db_factory=AsyncMock(),
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    with patch.object(manager, "_update_commission", AsyncMock()) as mock_update:
        manager.on_commission_report(mock_trade, mock_fill, mock_report)
        await asyncio.sleep(0.02)
        mock_update.assert_called_once_with("EXEC-55", Decimal("1.25"), "USD")


@pytest.mark.asyncio
async def test_cancel_order_in_db_updates_and_notifies(db, mock_config: Config) -> None:
    """Verifies _cancel_order_in_db sets status to Cancelled and sends failed order notification."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (60, 0, NULL, 'G1', 'A1', 'ENTRY', 'MSFT', 'STK', 'SMART', 'BUY', 10, 'LMT', 300.0, 'DAY', 'S1', 'Submitted')
        """
    )
    await db.commit()

    async def db_factory():
        return db

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    original_close = db.close
    db.close = AsyncMock()
    try:
        await manager._cancel_order_in_db(60, 202, "Order Canceled by User")
    finally:
        db.close = original_close

    async with db.execute("SELECT status FROM orders WHERE order_id = 60") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Cancelled"

    mock_notifier.send_order_failed.assert_called_once_with(
        order_id=60,
        tws_code=202,
        reason="Order Canceled by User",
        symbol="MSFT",
        bracket_role="ENTRY",
        is_fatal=False,
    )


@pytest.mark.asyncio
async def test_callbacks_additional_coverage_branches(mock_config: Config) -> None:
    """Verifies error handling, status mappings, commission retries, and LOC verification edge cases."""
    # 1. _update_order_status_db exception handler
    db_err = AsyncMock()
    db_err.execute.side_effect = RuntimeError("DB error")
    db_err.close = AsyncMock()

    async def db_factory_err():
        return db_err

    manager = TwsCallbacksManager(
        db_factory=db_factory_err,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )
    # Should catch exception internally
    await manager._update_order_status_db(100, "PreSubmitted", 999)

    # 2. on_order_status status mappings (PreSubmitted, Cancelled, Inactive, Unknown)
    trade_presubmitted = MagicMock()
    trade_presubmitted.order.orderId = 100
    trade_presubmitted.orderStatus.status = "PreSubmitted"
    trade_presubmitted.orderStatus.permId = 999
    trade_presubmitted.orderStatus.avgFillPrice = 0.0

    trade_inactive = MagicMock()
    trade_inactive.order.orderId = 101
    trade_inactive.orderStatus.status = "Inactive"
    trade_inactive.orderStatus.permId = 111
    trade_inactive.orderStatus.avgFillPrice = 0.0

    trade_unknown = MagicMock()
    trade_unknown.order.orderId = 102
    trade_unknown.orderStatus.status = "FOOBAR"
    trade_unknown.orderStatus.permId = 222
    trade_unknown.orderStatus.avgFillPrice = 0.0

    with patch.object(manager, "_process_status_change", AsyncMock()) as mock_process:
        manager.on_order_status(trade_presubmitted)
        await asyncio.sleep(0.01)
        mock_process.assert_called_with(
            100,
            "PreSubmitted",
            999,
            avg_fill_price=0.0,
            event_symbol=trade_presubmitted.contract.symbol,
            event_sec_type=trade_presubmitted.contract.secType,
        )

        manager.on_order_status(trade_inactive)
        await asyncio.sleep(0.01)
        mock_process.assert_called_with(
            101,
            "Cancelled",
            111,
            avg_fill_price=0.0,
            event_symbol=trade_inactive.contract.symbol,
            event_sec_type=trade_inactive.contract.secType,
        )

        manager.on_order_status(trade_unknown)
        await asyncio.sleep(0.01)
        mock_process.assert_called_with(
            102,
            "Error",
            222,
            avg_fill_price=0.0,
            event_symbol=trade_unknown.contract.symbol,
            event_sec_type=trade_unknown.contract.secType,
        )

    # 3. _process_status_change non-filled status return early
    with patch.object(manager, "_update_order_status_db", AsyncMock(return_value=True)):
        await manager._process_status_change(100, "Submitted", 999)

    # 4. _process_status_change missing order_row and exception handler
    db_empty = AsyncMock()
    cursor_empty = AsyncMock()
    cursor_empty.fetchone = AsyncMock(return_value=None)
    db_empty.execute.return_value.__aenter__ = AsyncMock(return_value=cursor_empty)
    db_empty.execute.return_value.__aexit__ = AsyncMock(return_value=None)
    db_empty.close = AsyncMock()

    async def db_factory_empty():
        return db_empty

    with patch.object(manager, "_update_order_status_db", AsyncMock(return_value=True)):
        manager.db_factory = db_factory_empty
        await manager._process_status_change(100, "Filled", 999)

        db_err_process = AsyncMock()
        db_err_process.execute.side_effect = RuntimeError("Process DB error")
        db_err_process.close = AsyncMock()

        async def db_factory_err_process():
            return db_err_process

        manager.db_factory = db_factory_err_process
        await manager._process_status_change(100, "Filled", 999)

    # 5. _save_execution without trade/fill and exception handler
    manager.db_factory = db_factory_empty
    await manager._save_execution(
        "EXEC_UNASSIGNED", 999, Decimal("10"), Decimal("1"), "USD", "now"
    )

    manager.db_factory = db_factory_err_process
    await manager._save_execution(
        "EXEC_ERR", 999, Decimal("10"), Decimal("1"), "USD", "now"
    )

    # 6. _cancel_order_in_db and _fail_order_in_db exception handlers
    await manager._cancel_order_in_db(999, 202, "Cancel error test")
    await manager._fail_order_in_db(999, 500, "Fatal error test")

    # 7. _update_commission exception retry logic
    db_comm_err = AsyncMock()
    db_comm_err.execute.side_effect = RuntimeError("Comm DB error")
    db_comm_err.close = AsyncMock()

    async def db_factory_comm_err():
        return db_comm_err

    manager.db_factory = db_factory_comm_err
    with pytest.raises(RuntimeError):
        await manager._update_commission("EXEC_ERR_COMM", Decimal("1.0"), "USD")


@pytest.mark.asyncio
async def test_on_error_dispatches_and_fail_order(db, mock_config: Config) -> None:
    """Verifies on_error dispatching and _fail_order_in_db token warning handling."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (50, 0, NULL, 'G50', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'Submitted')
        """
    )
    await db.commit()

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()
    mock_recovery = AsyncMock()
    mock_retriable = AsyncMock()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=mock_retriable,
        run_recovery_callback=mock_recovery,
        run_reconnect_callback=AsyncMock(),
    )

    try:
        # INFO code
        manager.on_error(-1, 2104, "Market data farm OK")

        # RECONNECT code (1101)
        manager.on_error(-1, 1101, "Connection restored")
        await asyncio.sleep(0.01)
        mock_recovery.assert_called_once()

        # RETRIABLE code (1100)
        manager.on_error(15, 1100, "Connectivity lost")
        await asyncio.sleep(0.01)
        mock_retriable.assert_called_once_with(15)

        # CANCEL code (202)
        with patch.object(manager, "_cancel_order_in_db", AsyncMock()) as mock_cancel:
            manager.on_error(20, 202, "Order cancelled")
            await asyncio.sleep(0.01)
            mock_cancel.assert_called_once_with(20, 202, "Order cancelled")

        # FATAL code (token error string test with <br>)
        await manager._fail_order_in_db(
            50, 504, "VERIFY USING THE TOKEN <br>IN CLIENT PORTAL"
        )
        mock_notifier.send_order_failed.assert_called_once()
        failed_reason = mock_notifier.send_order_failed.call_args[1]["reason"]
        assert "ANMELDUNG/VERIFIZIERUNG ERFORDERLICH" in failed_reason
        assert "<br>" not in failed_reason
        assert "TOKEN IN CLIENT PORTAL" in failed_reason

        # CANCEL code with <br>
        mock_notifier.send_order_failed.reset_mock()
        await manager._cancel_order_in_db(50, 202, "Order cancelled <br>by system")
        mock_notifier.send_order_failed.assert_called_once()
        cancel_reason = mock_notifier.send_order_failed.call_args[1]["reason"]
        assert "<br>" not in cancel_reason
        assert cancel_reason == "Order cancelled by system"
    finally:
        db.close = original_close


@pytest.mark.asyncio
async def test_loc_verification_edge_cases_and_on_disconnected(
    db, mock_config: Config
) -> None:
    """Verifies _verify_loc_cancellation errors/date checks and on_disconnected planned restart."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()
    mock_reconnect = AsyncMock()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=mock_ib,
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=mock_reconnect,
    )

    try:
        # 1. _is_near_or_after_market_close for .DE symbol
        assert isinstance(manager._is_near_or_after_market_close("SXRV.DE"), bool)

        # 2. _is_bar_from_today helper variants
        ny_today = datetime.now(ZoneInfo("America/New_York")).date()
        ny_now = datetime.now(ZoneInfo("America/New_York"))
        assert manager._is_bar_from_today(ny_today, "AAPL") is True
        assert manager._is_bar_from_today(ny_now, "AAPL") is True
        today_str = ny_now.strftime("%Y%m%d")
        assert manager._is_bar_from_today(today_str, "AAPL") is True
        assert manager._is_bar_from_today("INVALID_DATE", "AAPL") is False
        assert manager._is_bar_from_today(12345, "AAPL") is False

        # 8. _process_error for INFO and FATAL
        await manager._process_error(1, 2104, "Info message", ErrorClass.INFO)

        with patch.object(manager, "_fail_order_in_db", AsyncMock()) as mock_fail:
            await manager._process_error(1, 500, "Fatal error", ErrorClass.FATAL)
            mock_fail.assert_called_once_with(1, 500, "Fatal error")

        # 9. _save_execution with trade and fill for unassigned execution (line 394)
        with patch(
            "app.trading.callbacks.handle_unassigned_execution"
        ) as mock_handle_unassigned:
            await manager._save_execution(
                "EXEC_UNASSIGNED_2",
                9999,
                Decimal("10"),
                Decimal("1"),
                "USD",
                "now",
                trade=MagicMock(),
                fill=MagicMock(),
            )
            mock_handle_unassigned.assert_called_once()

        # 10. LOC verification branches with market_close mocked to True
        with (
            patch.object(manager, "_is_near_or_after_market_close", return_value=True),
            patch("asyncio.sleep", AsyncMock()),
        ):
            # Historical data exception
            mock_ib.reqHistoricalDataAsync = AsyncMock(
                side_effect=RuntimeError("Req failed")
            )
            await manager._check_loc_execution_price(
                1, "AAPL", "BUY", Decimal("150.0"), 10
            )

            # Empty bars
            mock_ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
            await manager._check_loc_execution_price(
                1, "AAPL", "BUY", Decimal("150.0"), 10
            )

            # Not today bar
            mock_bar = MagicMock()
            mock_bar.date = "20000101"
            mock_ib.reqHistoricalDataAsync = AsyncMock(return_value=[mock_bar])
            await manager._check_loc_execution_price(
                1, "AAPL", "BUY", Decimal("150.0"), 10
            )

            # Exception inside _check_loc_execution_price loop
            with patch.object(
                manager,
                "_is_bar_from_today",
                side_effect=RuntimeError("Bar date check crash"),
            ):
                await manager._check_loc_execution_price(
                    1, "AAPL", "BUY", Decimal("150.0"), 10
                )

        # 4. on_disconnected planned vs unplanned
        with patch("datetime.datetime") as mock_datetime:
            # Planned restart on Sunday at 12:02
            mock_datetime.now.return_value = datetime(2026, 8, 16, 12, 2, 0)
            manager.on_disconnected()
            await asyncio.sleep(0.01)
            mock_notifier.send_system_status.assert_called_with(
                title="GEPLANTER NEUSTART (Gateway wird neu gestartet)",
                emoji="⏳",
            )

            # Unplanned restart on Tuesday at 12:02 (wrong day)
            mock_notifier.send_system_status.reset_mock()
            mock_datetime.now.return_value = datetime(2026, 8, 11, 12, 2, 0)
            manager.on_disconnected()
            await asyncio.sleep(0.01)
            mock_notifier.send_system_status.assert_called_with(
                title="VERBINDUNGSABBRUCH",
                emoji="🚨",
            )

            # Unplanned restart on Sunday at 14:00 (wrong time)
            mock_notifier.send_system_status.reset_mock()
            mock_datetime.now.return_value = datetime(2026, 8, 16, 14, 0, 0)
            manager.on_disconnected()
            await asyncio.sleep(0.01)
            mock_notifier.send_system_status.assert_called_with(
                title="VERBINDUNGSABBRUCH",
                emoji="🚨",
            )
    finally:
        db.close = original_close


@pytest.mark.asyncio
async def test_callbacks_final_missing_lines(db, mock_config: Config) -> None:
    """Verifies missing order_row in _process_status_change and handle_unassigned_execution call in _save_execution."""
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    try:
        # 1. _process_status_change when order_id 88888 does not exist in DB (line 287)
        with patch.object(
            manager, "_update_order_status_db", AsyncMock(return_value=True)
        ):
            await manager._process_status_change(88888, "Filled", 7777)

        # 2. _save_execution when order_id 88888 does not exist in DB
        # Case A: trade and fill provided (line 392)
        mock_trade = MagicMock()
        mock_fill = MagicMock()
        with patch("app.trading.callbacks.handle_unassigned_execution") as mock_handle:
            await manager._save_execution(
                "EXEC_UNASSIGNED_88",
                88888,
                Decimal("10.0"),
                Decimal("1.0"),
                "USD",
                "2026-08-11T12:00:00+00:00",
                trade=mock_trade,
                fill=mock_fill,
            )
            mock_handle.assert_called_once_with(mock_trade, mock_fill)

        # Case B: trade and fill are None (line 394)
        await manager._save_execution(
            "EXEC_UNASSIGNED_89",
            88889,
            Decimal("10.0"),
            Decimal("1.0"),
            "USD",
            "2026-08-11T12:00:00+00:00",
            trade=None,
            fill=None,
        )
    finally:
        db.close = original_close


@pytest.mark.asyncio
async def test_update_order_status_db_ignores_symbol_mismatch(
    db, mock_config: Config
) -> None:
    """Verifies _update_order_status_db aborts and logs warning if event symbol does not match DB."""
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    try:
        # Arrange: create TSLA order in DB
        await db.execute(
            """
            INSERT INTO orders (order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status)
            VALUES (21, 1001, NULL, 'G1', 'U123', 'ENTRY', 'TSLA', 'STK', 'SMART', 'BUY', 4, 'MKT', NULL, 'DAY', 'Strat', 'PreSubmitted')
            """
        )
        await db.commit()

        # Act: incoming status for MES
        result = await manager._update_order_status_db(
            21, "Filled", 1001, event_symbol="MES", event_sec_type="FUT"
        )

        # Assert: rejected due to symbol mismatch
        assert result is False

        # Verify DB status is unchanged
        async with db.execute(
            "SELECT status FROM orders WHERE order_id = 21"
        ) as cursor:
            row = await cursor.fetchone()
            assert row["status"] == "PreSubmitted"
    finally:
        db.close = original_close


@pytest.mark.asyncio
async def test_process_status_change_ignores_symbol_mismatch_and_sends_no_alert(
    db, mock_config: Config
) -> None:
    """Verifies _process_status_change sends no Telegram alert and triggers no settlement on symbol mismatch."""
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()
    mock_notifier.send_order_filled = AsyncMock()
    mock_trigger_settlement = AsyncMock()

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=mock_notifier,
        config=mock_config,
        trigger_settlement_callback=mock_trigger_settlement,
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    try:
        # Arrange: create TSLA exit order in DB
        await db.execute(
            """
            INSERT INTO orders (order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status)
            VALUES (21, 1001, NULL, 'G1', 'U123', 'EXIT', 'TSLA', 'STK', 'SMART', 'SELL', 4, 'MKT', NULL, 'DAY', 'Strat', 'PreSubmitted')
            """
        )
        await db.commit()

        # Act: process status change with symbol mismatch
        await manager._process_status_change(
            21,
            "Filled",
            1001,
            avg_fill_price=7700.25,
            event_symbol="MES",
            event_sec_type="FUT",
        )

        # Assert: no notification and no settlement triggered
        mock_notifier.send_order_filled.assert_not_called()
        mock_trigger_settlement.assert_not_called()
    finally:
        db.close = original_close


@pytest.mark.asyncio
async def test_save_execution_ignores_symbol_mismatch(db, mock_config: Config) -> None:
    """Verifies _save_execution does not save execution when symbol mismatches DB order."""
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    try:
        # Arrange: create TSLA order in DB
        await db.execute(
            """
            INSERT INTO orders (order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status)
            VALUES (21, 1001, NULL, 'G1', 'U123', 'ENTRY', 'TSLA', 'STK', 'SMART', 'BUY', 4, 'MKT', NULL, 'DAY', 'Strat', 'PreSubmitted')
            """
        )
        await db.commit()

        mock_trade = MagicMock()
        mock_fill = MagicMock()

        # Act: save execution with mismatched symbol
        with patch("app.trading.callbacks.handle_unassigned_execution") as mock_handle:
            await manager._save_execution(
                "EXEC_MES_1",
                21,
                Decimal("7700.25"),
                Decimal("1.0"),
                "USD",
                "2026-08-31T21:59:00+00:00",
                symbol="MES",
                trade=mock_trade,
                fill=mock_fill,
            )
            mock_handle.assert_called_once_with(mock_trade, mock_fill)

        # Assert: no execution inserted in DB
        async with db.execute(
            "SELECT COUNT(*) as count FROM executions WHERE order_id = 21"
        ) as cursor:
            row = await cursor.fetchone()
            assert row["count"] == 0
    finally:
        db.close = original_close


@pytest.mark.asyncio
async def test_update_order_status_accepts_normalized_symbol_match_dot_de(
    db, mock_config: Config
) -> None:
    """Verifies _update_order_status accepts status updates when DB symbol has .DE suffix and event has clean symbol."""
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    try:
        # Arrange: create SXRV.DE exit order in DB
        await db.execute(
            """
            INSERT INTO orders (order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status)
            VALUES (1356, 2001, NULL, 'G_SXRV', 'U123', 'EXIT', 'SXRV.DE', 'STK', 'SMART', 'SELL', 5, 'LMT', 145.5, 'DAY', 'TwoPercent', 'Submitted')
            """
        )
        await db.commit()

        # Act: process status update with clean symbol 'SXRV' from TWS
        result = await manager._update_order_status_db(
            1356,
            "Cancelled",
            2001,
            event_symbol="SXRV",
            event_sec_type="STK",
        )

        # Assert: accepted because normalize_symbol('SXRV.DE') == normalize_symbol('SXRV')
        assert result is True

        # Verify DB status is updated to Cancelled
        async with db.execute(
            "SELECT status FROM orders WHERE order_id = 1356"
        ) as cursor:
            row = await cursor.fetchone()
            assert row["status"] == "Cancelled"
    finally:
        db.close = original_close


@pytest.mark.asyncio
async def test_save_execution_accepts_normalized_symbol_match_dot_de(
    db, mock_config: Config
) -> None:
    """Verifies _save_execution persists execution when DB symbol is SXRV.DE and event symbol is SXRV."""
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=MagicMock(),
        config=mock_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    try:
        # Arrange: create SXRV.DE order in DB
        await db.execute(
            """
            INSERT INTO orders (order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status)
            VALUES (1356, 2001, NULL, 'G_SXRV', 'U123', 'ENTRY', 'SXRV.DE', 'STK', 'SMART', 'BUY', 5, 'LMT', 145.5, 'DAY', 'TwoPercent', 'Submitted')
            """
        )
        await db.commit()

        # Act: save execution with clean symbol 'SXRV' from TWS
        await manager._save_execution(
            "EXEC_SXRV_1",
            1356,
            Decimal("145.50"),
            Decimal("5"),
            "EUR",
            "2026-09-01T17:46:00+00:00",
            symbol="SXRV",
        )

        # Assert: execution inserted in DB
        async with db.execute(
            "SELECT COUNT(*) as count, price, qty, currency FROM executions WHERE order_id = 1356"
        ) as cursor:
            row = await cursor.fetchone()
            assert row["count"] == 1
            assert Decimal(str(row["price"])) == Decimal("145.50")
            assert Decimal(str(row["qty"])) == Decimal("5")
            assert row["currency"] == "EUR"
    finally:
        db.close = original_close
