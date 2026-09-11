# filename: tests/trading/test_worker_gtd.py
"""
Integration and demolition tests for LOC/MOC GTD order cutoff in app.trading.worker.

Edge-Case Attack Matrix:
| Test ID       | Scenario Description                                 | Expected Outcome                                    |
|---------------|------------------------------------------------------|-----------------------------------------------------|
| TC_INT_01     | Standard US Bracket: ENTRY + TP (LMT) + SL + LOC     | TP gets tif='GTD' & goodTillDate. SL retains 'GTC'.  |
|               |                                                      | DB orders table updated with tif='GTD' for TP.      |
| TC_INT_02     | German Equity (.DE) with EXIT (LMT) + EXIT (LOC)     | LMT gets tif='GTD' & cutoff '17:18:00 Europe/Berlin'.|
| TC_INT_03     | Late processing: After 15:48 US/Eastern Cutoff       | LMT exit skipped/Cancelled, alert sent, LOC active.  |
| TC_INT_04     | Post-Fill group with EXIT (LMT) + EXIT (LOC)         | LMT gets GTD & transmit=True, DB synced.             |
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.trading.worker import process_trade_group


def _create_mock_notifier() -> MagicMock:
    """Creates a mock TelegramNotifier with all coroutines mocked as AsyncMock."""
    mock_notifier = MagicMock()
    mock_notifier.send_bracket_order_submitted = AsyncMock()
    mock_notifier.send_order_failed = AsyncMock()
    mock_notifier.send_margin_utilization_warning = AsyncMock()
    mock_notifier.send_high_margin_usage_warning = AsyncMock()
    mock_notifier.send_margin_limit_exceeded = AsyncMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_notifier.send_message = AsyncMock()
    return mock_notifier


@pytest.fixture
def test_config() -> Config:
    """Provides a deterministic test configuration."""
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
        heartbeat_interval_s=60.0,
        heartbeat_timeout_s=15.0,
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
async def test_db():
    """Initializes in-memory SQLite database with required orders table."""
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.execute(
        """
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            perm_id INTEGER,
            parent_id INTEGER,
            trade_group_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            bracket_role TEXT NOT NULL,
            symbol TEXT NOT NULL,
            sec_type TEXT NOT NULL,
            exchange TEXT NOT NULL,
            action TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            order_type TEXT NOT NULL,
            target_price TEXT,
            tif TEXT DEFAULT 'GTC',
            strategy_name TEXT,
            status TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            transmitted_at TIMESTAMP
        )
        """
    )
    await connection.commit()
    yield connection
    await connection.close()


@pytest.mark.asyncio
async def test_process_trade_group_applies_gtd_to_lmt_tp_with_loc_sibling(
    test_db: aiosqlite.Connection, test_config: Config
) -> None:
    """Verifies that TP LMT order receives GTD and goodTillDate when LOC sibling is present."""
    # Arrange
    trade_group_id = "TG_US_LOC_01"
    account_id = "U999999"

    # Insert bracket orders: ENTRY, TP (LMT), SL (STP), EXIT (LOC)
    await test_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, status)
        VALUES
        (-1, ?, ?, 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', '150.00', 'GTC', 'Created'),
        (-2, ?, ?, 'TP', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'LMT', '155.00', 'GTC', 'Created'),
        (-3, ?, ?, 'SL', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'STP', '145.00', 'GTC', 'Created'),
        (-4, ?, ?, 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'LOC', '150.00', 'DAY', 'Created')
        """,
        (
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
        ),
    )
    await test_db.commit()

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = []
    mock_order_state = MagicMock()
    mock_order_state.initMarginAfter = "100.0"
    mock_order_state.equityWithLoanAfter = "1000.0"
    mock_ib.whatIfOrderAsync = AsyncMock(return_value=mock_order_state)

    placed_tws_orders: list[tuple] = []

    def mock_place_order(contract, ib_order):
        placed_tws_orders.append((contract, ib_order))
        trade = MagicMock()
        trade.orderStatus.status = "Submitted"
        return trade

    mock_ib.placeOrder.side_effect = mock_place_order
    mock_ib.client.getReqId.side_effect = [101, 102, 103, 104]
    mock_notifier = _create_mock_notifier()

    # Act
    with (
        patch(
            "app.trading.worker.compute_loc_gtd_cutoff",
            return_value="20260910 15:48:00 US/Eastern",
        ),
        patch("app.trading.worker.is_past_loc_gtd_cutoff", return_value=False),
    ):
        await process_trade_group(
            test_db, mock_ib, trade_group_id, mock_notifier, test_config
        )

    # Assert
    assert len(placed_tws_orders) == 4

    # Extract placed orders by orderType / action
    tp_order = next(
        o for _, o in placed_tws_orders if o.orderType == "LMT" and o.action == "SELL"
    )
    sl_order = next(o for _, o in placed_tws_orders if o.orderType == "STP")
    loc_order = next(o for _, o in placed_tws_orders if o.orderType == "LOC")

    # 1. TP must have GTD and cutoff
    assert tp_order.tif == "GTD"
    assert tp_order.goodTillDate == "20260910 15:48:00 US/Eastern"

    # 2. SL must retain GTC (never GTD!)
    assert sl_order.tif == "GTC"
    assert not hasattr(sl_order, "goodTillDate") or not sl_order.goodTillDate

    # 3. LOC retains its type
    assert loc_order.orderType == "LOC"

    # 4. Verify DB persistence of tif='GTD' for TP order
    async with test_db.execute(
        "SELECT tif FROM orders WHERE bracket_role = 'TP'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["tif"] == "GTD"

    # 5. Verify DB persistence of tif='GTC' for SL order
    async with test_db.execute(
        "SELECT tif FROM orders WHERE bracket_role = 'SL'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["tif"] == "GTC"


@pytest.mark.asyncio
async def test_process_trade_group_applies_gtd_to_german_equity_with_berlin_tz(
    test_db: aiosqlite.Connection, test_config: Config
) -> None:
    """Verifies German equity (.DE) gets 17:18:00 Europe/Berlin cutoff."""
    # Arrange
    trade_group_id = "TG_DE_LOC_02"
    account_id = "U999999"

    await test_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, status)
        VALUES
        (-10, ?, ?, 'ENTRY', 'SXRV.DE', 'STK', 'SMART', 'BUY', 5, 'LMT', '1400.00', 'GTC', 'Created'),
        (-11, ?, ?, 'EXIT', 'SXRV.DE', 'STK', 'SMART', 'SELL', 5, 'LMT', '1450.00', 'GTC', 'Created'),
        (-12, ?, ?, 'EXIT', 'SXRV.DE', 'STK', 'SMART', 'SELL', 5, 'LOC', '1400.00', 'DAY', 'Created')
        """,
        (
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
        ),
    )
    await test_db.commit()

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = []
    mock_order_state = MagicMock()
    mock_order_state.initMarginAfter = "100.0"
    mock_order_state.equityWithLoanAfter = "1000.0"
    mock_ib.whatIfOrderAsync = AsyncMock(return_value=mock_order_state)

    placed_tws_orders: list[tuple] = []
    mock_ib.placeOrder.side_effect = lambda contract, order: (
        placed_tws_orders.append((contract, order))
        or MagicMock(orderStatus=MagicMock(status="Submitted"))
    )
    mock_ib.client.getReqId.side_effect = [201, 202, 203]
    mock_notifier = _create_mock_notifier()

    # Act
    with (
        patch(
            "app.trading.worker.compute_loc_gtd_cutoff",
            return_value="20260910 17:18:00 Europe/Berlin",
        ),
        patch("app.trading.worker.is_past_loc_gtd_cutoff", return_value=False),
    ):
        await process_trade_group(
            test_db, mock_ib, trade_group_id, mock_notifier, test_config
        )

    # Assert
    lmt_exit = next(
        o for _, o in placed_tws_orders if o.orderType == "LMT" and o.action == "SELL"
    )
    assert lmt_exit.tif == "GTD"
    assert lmt_exit.goodTillDate == "20260910 17:18:00 Europe/Berlin"

    async with test_db.execute(
        "SELECT tif FROM orders WHERE order_type = 'LMT' AND bracket_role = 'EXIT'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["tif"] == "GTD"


@pytest.mark.asyncio
async def test_process_trade_group_skips_lmt_exit_when_past_cutoff(
    test_db: aiosqlite.Connection, test_config: Config
) -> None:
    """Verifies that if submission occurs past cutoff, the LMT order is cancelled in DB and not sent."""
    # Arrange
    trade_group_id = "TG_LATE_03"
    account_id = "U999999"

    await test_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, status)
        VALUES
        (-20, ?, ?, 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', '150.00', 'GTC', 'Created'),
        (-21, ?, ?, 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'LMT', '155.00', 'GTC', 'Created'),
        (-22, ?, ?, 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'LOC', '150.00', 'DAY', 'Created')
        """,
        (
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
        ),
    )
    await test_db.commit()

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = []
    mock_order_state = MagicMock()
    mock_order_state.initMarginAfter = "100.0"
    mock_order_state.equityWithLoanAfter = "1000.0"
    mock_ib.whatIfOrderAsync = AsyncMock(return_value=mock_order_state)

    placed_tws_orders: list[tuple] = []
    mock_ib.placeOrder.side_effect = lambda contract, order: (
        placed_tws_orders.append((contract, order))
        or MagicMock(orderStatus=MagicMock(status="Submitted"))
    )
    mock_ib.client.getReqId.side_effect = [301, 302, 303]
    mock_notifier = _create_mock_notifier()

    # Act: Simulate being past the cutoff (15:52 EST)
    with patch("app.trading.worker.is_past_loc_gtd_cutoff", return_value=True):
        await process_trade_group(
            test_db, mock_ib, trade_group_id, mock_notifier, test_config
        )

    # Assert:
    # 1. Entry (BUY LMT) and LOC should be placed, but NOT the LMT exit (SELL LMT)
    assert any(o.action == "BUY" and o.orderType == "LMT" for _, o in placed_tws_orders)
    assert any(o.orderType == "LOC" for _, o in placed_tws_orders)
    assert not any(
        o.action == "SELL" and o.orderType == "LMT" for _, o in placed_tws_orders
    )

    # 2. In DB, the LMT exit must be 'Cancelled'
    async with test_db.execute(
        "SELECT status FROM orders WHERE order_type = 'LMT' AND action = 'SELL'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Cancelled"

    # 3. A warning telegram message was sent
    mock_notifier.send_message.assert_called_once()
    assert "LMT-EXIT ÜBERSPRINGEN" in mock_notifier.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_process_trade_group_post_fill_lmt_with_loc(
    test_db: aiosqlite.Connection, test_config: Config
) -> None:
    """Verifies that post-fill child processing applies GTD to LMT exit alongside LOC."""
    # Arrange
    trade_group_id = "TG_POST_FILL_04"
    account_id = "U999999"

    # Entry is already 'Filled'
    await test_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, status)
        VALUES
        (50, ?, ?, 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', '150.00', 'GTC', 'Filled'),
        (-51, ?, ?, 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'LMT', '155.00', 'GTC', 'Created'),
        (-52, ?, ?, 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'LOC', '150.00', 'DAY', 'Created')
        """,
        (
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
            trade_group_id,
            account_id,
        ),
    )
    await test_db.commit()

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = []
    # Mock position matching account_id and symbol
    mock_position = MagicMock()
    mock_position.account = account_id
    mock_position.contract.symbol = "AAPL"
    mock_position.position = 10.0
    mock_ib.positions.return_value = [mock_position]

    placed_tws_orders: list[tuple] = []
    mock_ib.placeOrder.side_effect = lambda contract, order: (
        placed_tws_orders.append((contract, order))
        or MagicMock(orderStatus=MagicMock(status="Submitted"))
    )
    mock_ib.client.getReqId.side_effect = [501, 502]
    mock_notifier = _create_mock_notifier()

    # Act
    with (
        patch(
            "app.trading.worker.compute_loc_gtd_cutoff",
            return_value="20260910 15:48:00 US/Eastern",
        ),
        patch("app.trading.worker.is_past_loc_gtd_cutoff", return_value=False),
    ):
        await process_trade_group(
            test_db, mock_ib, trade_group_id, mock_notifier, test_config
        )

    # Assert
    assert len(placed_tws_orders) == 2
    lmt_order = next(o for _, o in placed_tws_orders if o.orderType == "LMT")
    assert lmt_order.tif == "GTD"
    assert lmt_order.transmit is True
    assert lmt_order.goodTillDate == "20260910 15:48:00 US/Eastern"
