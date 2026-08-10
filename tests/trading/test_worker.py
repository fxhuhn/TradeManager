# filename: tests/trading/test_worker.py
"""Unit and integration tests for execution worker logic in app.trading.worker."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.core.models import OrderRow
from app.trading.worker import (
    _check_cushion_limit,
    _get_next_non_colliding_order_id,
    _place_and_verify_order,
    process_trade_group,
)


@pytest.fixture
def test_config() -> Config:
    """Erstellt eine Testkonfiguration für die Worker-Prüfungen."""
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


@pytest.mark.asyncio
async def test_get_next_non_colliding_order_id_uses_db_max_plus_one_when_higher() -> (
    None
):
    """Verifies that DB MAX(order_id) + 1 is returned if DB max is greater than or equal to TWS reqId."""
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, status TEXT)"
        )
        await db.execute(
            "INSERT INTO orders (order_id, status) VALUES (5000, 'Created')"
        )
        await db.commit()

        ib_mock = MagicMock()
        ib_mock.client.getReqId.return_value = 1200

        next_id = await _get_next_non_colliding_order_id(db, ib_mock)
        assert next_id == 5001


@pytest.mark.asyncio
async def test_get_next_non_colliding_order_id_uses_tws_id_when_higher() -> None:
    """Verifies that TWS getReqId() is returned when it is strictly greater than DB MAX(order_id)."""
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, status TEXT)"
        )
        await db.execute(
            "INSERT INTO orders (order_id, status) VALUES (1000, 'Created')"
        )
        await db.commit()

        ib_mock = MagicMock()
        ib_mock.client.getReqId.return_value = 2500

        next_id = await _get_next_non_colliding_order_id(db, ib_mock)
        assert next_id == 2500


@pytest.mark.asyncio
async def test_check_cushion_limit_blocks_order_when_cushion_below_threshold() -> None:
    """Verifies that _check_cushion_limit sets order status to Error when cushion is below minimum."""
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, status TEXT)"
        )
        await db.execute("INSERT INTO orders (order_id, status) VALUES (1, 'Created')")
        await db.commit()

        account_val = MagicMock()
        account_val.tag = "Cushion"
        account_val.account = "U123456"
        account_val.value = "0.02"

        ib_mock = MagicMock()
        ib_mock.accountValues.return_value = [account_val]

        entry_order = OrderRow(
            order_id=1,
            perm_id=None,
            parent_id=None,
            trade_group_id="TG-101",
            account_id="U123456",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            action="BUY",
            quantity=10,
            order_type="LMT",
            target_price=Decimal("150.00"),
            tif="GTC",
            strategy_name="DipBuyer",
            status="Created",
        )

        config_mock = MagicMock()
        config_mock.account.min_cushion_pct = 0.05

        notifier_mock = AsyncMock()

        passed, updated_order, cushion_pct = await _check_cushion_limit(
            db, ib_mock, entry_order, config_mock, notifier_mock
        )

        assert passed is False
        assert updated_order.status == "Error"
        assert cushion_pct == Decimal("2.0")
        notifier_mock.send_margin_limit_exceeded.assert_called_once()


@pytest.mark.asyncio
async def test_cushion_check_blocks_order(db, test_config: Config) -> None:
    """Prüft, dass die Order blockiert wird, wenn Cushion < min_cushion_pct."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (-10, 'TG_CUSHION_FAIL', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'Created')
        """
    )
    await db.commit()

    mock_cushion_value = MagicMock()
    mock_cushion_value.tag = "Cushion"
    mock_cushion_value.value = "0.05"
    mock_cushion_value.account = "ACC_1"

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [mock_cushion_value]
    mock_ib.positions.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_margin_limit_exceeded = AsyncMock(return_value=True)

    await process_trade_group(
        db, mock_ib, "TG_CUSHION_FAIL", mock_notifier, test_config
    )

    async with db.execute("SELECT status FROM orders WHERE order_id = -10") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Error"

    mock_ib.placeOrder.assert_not_called()
    mock_notifier.send_margin_limit_exceeded.assert_called_once_with(
        symbol="AAPL",
        account_id="ACC_1",
        init_margin_after=0.0,
        limit_value=0.0,
        cushion_percentage=5.0,
    )


@pytest.mark.asyncio
async def test_what_if_limit_exceeded(db, test_config: Config) -> None:
    """Prüft, dass die Order blockiert wird, wenn die simulierte Margin das max_margin_usage_pct übersteigt."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (-11, 'TG_MARGIN_FAIL', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 150.0, 'Created')
        """
    )
    await db.commit()

    mock_cushion_value = MagicMock()
    mock_cushion_value.tag = "Cushion"
    mock_cushion_value.value = "0.20"
    mock_cushion_value.account = "ACC_1"

    mock_what_if_info = MagicMock()
    mock_what_if_info.initMarginAfter = "90000.0"
    mock_what_if_info.maintMarginAfter = "70000.0"
    mock_what_if_info.equityWithLoanAfter = "100000.0"

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [mock_cushion_value]
    mock_ib.whatIfOrderAsync = AsyncMock(return_value=mock_what_if_info)
    mock_ib.positions.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_margin_limit_exceeded = AsyncMock(return_value=True)

    await process_trade_group(db, mock_ib, "TG_MARGIN_FAIL", mock_notifier, test_config)

    async with db.execute("SELECT status FROM orders WHERE order_id = -11") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Error"

    mock_notifier.send_margin_limit_exceeded.assert_called_once_with(
        symbol="AAPL",
        account_id="ACC_1",
        init_margin_after=90000.0,
        limit_value=80000.0,
        cushion_percentage=20.0,
    )


@pytest.mark.asyncio
async def test_margin_utilization_warning(db, test_config: Config) -> None:
    """Prüft, dass eine Warnung gesendet wird, wenn der Kaufwert das freie Cash übersteigt (Fremdkapitalnutzung)."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (-12, 'TG_CASH_WARNING', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 150.0, 'Created')
        """
    )
    await db.commit()

    mock_cushion_value = MagicMock()
    mock_cushion_value.tag = "Cushion"
    mock_cushion_value.value = "0.20"
    mock_cushion_value.account = "ACC_1"

    mock_cash_value = MagicMock()
    mock_cash_value.tag = "TotalCashValue"
    mock_cash_value.value = "5000.0"
    mock_cash_value.account = "ACC_1"

    mock_what_if_info = MagicMock()
    mock_what_if_info.initMarginAfter = "10000.0"
    mock_what_if_info.maintMarginAfter = "8000.0"
    mock_what_if_info.equityWithLoanAfter = "100000.0"

    mock_live_trade = MagicMock()
    mock_live_trade.orderStatus.status = "Submitted"

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [mock_cushion_value, mock_cash_value]
    mock_ib.whatIfOrderAsync = AsyncMock(return_value=mock_what_if_info)
    mock_ib.placeOrder.return_value = mock_live_trade
    mock_ib.client.getReqId.return_value = 102
    mock_ib.positions.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_margin_utilization_warning = AsyncMock(return_value=True)
    mock_notifier.send_bracket_order_submitted = AsyncMock(return_value=True)

    await process_trade_group(
        db, mock_ib, "TG_CASH_WARNING", mock_notifier, test_config
    )

    mock_notifier.send_margin_utilization_warning.assert_called_once_with(
        symbol="AAPL",
        account_id="ACC_1",
        purchase_value=15000.0,
        total_cash=5000.0,
        margin_needed=10000.0,
    )


@pytest.mark.asyncio
async def test_high_margin_usage_warning(db, test_config: Config) -> None:
    """Prüft, dass eine Warnung gesendet wird, wenn die Margin-Auslastung 50% übersteigt."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (-13, 'TG_HIGH_USAGE', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 150.0, 'Created')
        """
    )
    await db.commit()

    mock_cushion_value = MagicMock()
    mock_cushion_value.tag = "Cushion"
    mock_cushion_value.value = "0.20"
    mock_cushion_value.account = "ACC_1"

    mock_cash_value = MagicMock()
    mock_cash_value.tag = "TotalCashValue"
    mock_cash_value.value = "20000.0"
    mock_cash_value.account = "ACC_1"

    mock_what_if_info = MagicMock()
    mock_what_if_info.initMarginAfter = "60000.0"
    mock_what_if_info.maintMarginAfter = "45000.0"
    mock_what_if_info.equityWithLoanAfter = "100000.0"

    mock_live_trade = MagicMock()
    mock_live_trade.orderStatus.status = "Submitted"

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [mock_cushion_value, mock_cash_value]
    mock_ib.whatIfOrderAsync = AsyncMock(return_value=mock_what_if_info)
    mock_ib.placeOrder.return_value = mock_live_trade
    mock_ib.client.getReqId.return_value = 103
    mock_ib.positions.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_high_margin_usage_warning = AsyncMock(return_value=True)
    mock_notifier.send_bracket_order_submitted = AsyncMock(return_value=True)

    await process_trade_group(db, mock_ib, "TG_HIGH_USAGE", mock_notifier, test_config)

    mock_notifier.send_high_margin_usage_warning.assert_called_once_with(
        symbol="AAPL",
        account_id="ACC_1",
        usage_percentage=60.0,
        init_margin_after=60000.0,
        net_liquidation=100000.0,
    )


@pytest.mark.asyncio
async def test_failed_entry_order_cancels_created_child_orders(
    db, test_config: Config
) -> None:
    """Prüft, dass verbleibende Child-Orders auf Error gesetzt werden, wenn das Entry fehlschlägt/fehlgeschlagen ist."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (-14, 'TG_FAILED_ENTRY', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 150.0, 'Error')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (-15, 'TG_FAILED_ENTRY', 'ACC_1', 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 100, 'MKT', NULL, 'Created')
        """
    )
    await db.commit()

    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    await process_trade_group(
        db, mock_ib, "TG_FAILED_ENTRY", mock_notifier, test_config
    )

    async with db.execute("SELECT status FROM orders WHERE order_id = -15") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Error"


@pytest.mark.asyncio
async def test_place_and_verify_order_warning_399(db) -> None:
    """Prüft, dass _place_and_verify_order bei Warnung 399 (ValidationError) Erfolg (True) zurückgibt."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (42, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 180.0, 'Submitted')
        """
    )
    await db.commit()

    order_row = OrderRow(
        order_id=42,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("180.0"),
        tif="GTC",
        strategy_name="NDXMomentum",
        status="Submitted",
    )

    mock_log_entry = MagicMock()
    mock_log_entry.errorCode = 399
    mock_log_entry.status = "ValidationError"
    mock_log_entry.message = "Warning 399: order held"

    mock_trade = MagicMock()
    mock_trade.orderStatus.status = "ValidationError"
    mock_trade.log = [mock_log_entry]

    mock_ib = MagicMock()
    mock_ib.placeOrder.return_value = mock_trade

    mock_notifier = AsyncMock()
    mock_notifier.send_message = AsyncMock(return_value=True)

    result = await _place_and_verify_order(
        db=db,
        interactive_brokers=mock_ib,
        contract=MagicMock(),
        ib_order=MagicMock(),
        order_row=order_row,
        tws_order_id=42,
        notifier=mock_notifier,
    )

    assert result is True
    async with db.execute("SELECT status FROM orders WHERE order_id = 42") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Submitted"


@pytest.mark.asyncio
async def test_place_and_verify_order_real_error(db) -> None:
    """Prüft, dass _place_and_verify_order bei einem echten Fehler False zurückgibt und DB-Status auf Error setzt."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (43, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 180.0, 'Submitted')
        """
    )
    await db.commit()

    order_row = OrderRow(
        order_id=43,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("180.0"),
        tif="GTC",
        strategy_name="NDXMomentum",
        status="Submitted",
    )

    mock_log_entry = MagicMock()
    mock_log_entry.errorCode = 201
    mock_log_entry.status = "ValidationError"
    mock_log_entry.message = "Order rejected"

    mock_trade = MagicMock()
    mock_trade.orderStatus.status = "ValidationError"
    mock_trade.log = [mock_log_entry]

    mock_ib = MagicMock()
    mock_ib.placeOrder.return_value = mock_trade

    mock_notifier = AsyncMock()

    result = await _place_and_verify_order(
        db=db,
        interactive_brokers=mock_ib,
        contract=MagicMock(),
        ib_order=MagicMock(),
        order_row=order_row,
        tws_order_id=43,
        notifier=mock_notifier,
    )

    assert result is False
    async with db.execute("SELECT status FROM orders WHERE order_id = 43") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Error"


@pytest.mark.asyncio
async def test_place_and_verify_order_token_verification_error(db) -> None:
    """Prüft, dass bei Error 201 der Hinweis auf die erforderliche Anmeldung im Client Portal an Telegram gesendet wird."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (44, NULL, 'G1', 'A1', 'ENTRY', 'GOOGL', 'STK', 'SMART', 'BUY', 10, 'LMT', 300.0, 'Submitted')
        """
    )
    await db.commit()

    order_row = OrderRow(
        order_id=44,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="GOOGL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=10,
        order_type="LMT",
        target_price=Decimal("300.0"),
        tif="GTC",
        strategy_name="DipBuyer",
        status="Submitted",
    )

    mock_log_entry = MagicMock()
    mock_log_entry.errorCode = 201
    mock_log_entry.status = "ValidationError"
    mock_log_entry.message = "Order rejected - reason:BEFORE WE CAN ACCEPT YOUR ORDER IN THIS SECURITY, PLEASE LOGIN TO CLIENT PORTAL AND VERIFY USING THE TOKEN WE EMAILED TO YOU."

    mock_trade = MagicMock()
    mock_trade.orderStatus.status = "Inactive"
    mock_trade.log = [mock_log_entry]

    mock_ib = MagicMock()
    mock_ib.placeOrder.return_value = mock_trade

    mock_notifier = AsyncMock()

    result = await _place_and_verify_order(
        db=db,
        interactive_brokers=mock_ib,
        contract=MagicMock(),
        ib_order=MagicMock(),
        order_row=order_row,
        tws_order_id=44,
        notifier=mock_notifier,
    )

    assert result is False
    mock_notifier.send_order_failed.assert_called_once()
    call_args = mock_notifier.send_order_failed.call_args[1]
    assert call_args["tws_code"] == 201
    assert "ANMELDUNG/VERIFIZIERUNG ERFORDERLICH" in call_args["reason"]
    assert call_args["symbol"] == "GOOGL"


@pytest.mark.asyncio
async def test_process_trade_group_exit_cancelled_if_no_position(
    db, test_config: Config
) -> None:
    """
    Prüft, dass eine Post-Fill Exit-Order storniert wird,
    wenn kein Depotbestand für das Symbol vorhanden ist.
    """
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (1, 'TG_NO_POS', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (2, 'TG_NO_POS', 'ACC_1', 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'MKT', 'Created')
        """
    )
    await db.commit()

    mock_ib = MagicMock()
    mock_ib.positions.return_value = []
    mock_ib.client.getReqId.return_value = 100

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_notifier.send_message = AsyncMock()

    await process_trade_group(db, mock_ib, "TG_NO_POS", mock_notifier, test_config)

    async with db.execute("SELECT status FROM orders WHERE order_id = 2") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Cancelled"

    mock_ib.placeOrder.assert_not_called()
    mock_notifier.send_importer_info.assert_called_once()
    assert (
        "Keine offene Position"
        in mock_notifier.send_importer_info.call_args[1]["details"]
    )


@pytest.mark.asyncio
async def test_process_trade_group_exit_quantity_reduced(
    db, test_config: Config
) -> None:
    """
    Prüft, dass die Menge der Exit-Order reduziert wird,
    wenn der Depotbestand kleiner ist als die geplante Exit-Menge.
    """
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (10, 'TG_RED_POS', 'ACC_1', 'ENTRY', 'MSFT', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (11, 'TG_RED_POS', 'ACC_1', 'EXIT', 'MSFT', 'STK', 'SMART', 'SELL', 10, 'MKT', 'Created')
        """
    )
    await db.commit()

    mock_position = MagicMock()
    mock_position.account = "ACC_1"
    mock_position.contract.symbol = "MSFT"
    mock_position.position = 4.0

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]
    mock_ib.client.getReqId.return_value = 101

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_notifier.send_bracket_order_submitted = AsyncMock()
    mock_notifier.send_message = AsyncMock()

    await process_trade_group(db, mock_ib, "TG_RED_POS", mock_notifier, test_config)

    async with db.execute(
        "SELECT status, quantity FROM orders WHERE order_id = 101"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Submitted"
        assert row["quantity"] == 4

    mock_ib.placeOrder.assert_called_once()
    called_order = mock_ib.placeOrder.call_args[0][1]
    assert called_order.totalQuantity == 4.0
    mock_notifier.send_importer_info.assert_called_once()
    assert "reduziert" in mock_notifier.send_importer_info.call_args[1]["details"]
    mock_notifier.send_bracket_order_submitted.assert_called_once()


@pytest.mark.asyncio
async def test_process_trade_group_exit_matching_with_dot_de_suffix(
    db, test_config: Config
) -> None:
    """Verifiziert, dass SXRV.DE in der DB gegen eine IB-Position SXRV gematcht wird."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (30, 'TG_SXRV_DE', 'ACC_GERMANY', 'ENTRY', 'SXRV.DE', 'STK', 'SMART', 'BUY', 5, 'LMT', 1400.0, 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (31, 'TG_SXRV_DE', 'ACC_GERMANY', 'EXIT', 'SXRV.DE', 'STK', 'SMART', 'SELL', 5, 'MKT', 0.0, 'Created')
        """
    )
    await db.commit()

    mock_position = MagicMock()
    mock_position.account = "ACC_GERMANY"
    mock_position.contract.symbol = "SXRV"
    mock_position.position = 5.0

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]
    mock_ib.client.getReqId.return_value = 301

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_notifier.send_bracket_order_submitted = AsyncMock()
    mock_notifier.send_message = AsyncMock()

    await process_trade_group(db, mock_ib, "TG_SXRV_DE", mock_notifier, test_config)

    async with db.execute(
        "SELECT status, quantity FROM orders WHERE order_id = 301"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Submitted"
        assert row["quantity"] == 5

    mock_ib.placeOrder.assert_called_once()
    called_contract = mock_ib.placeOrder.call_args[0][0]
    assert called_contract.symbol == "SXRV"
    assert called_contract.primaryExchange == "IBIS2"
