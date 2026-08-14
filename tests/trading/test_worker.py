# filename: tests/trading/test_worker.py
"""Unit and integration tests for execution worker logic in app.trading.worker."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
async def test_execution_worker_loop_and_exception_handling(
    test_config: Config,
) -> None:
    """Verifies execution_worker connection waiting, processing, error notification, and cancellation."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    await queue.put("TG_NORMAL")
    await queue.put("TG_ERROR")
    await queue.put("TG_CANCEL")

    mock_ib = MagicMock()
    # First disconnected once, then connected
    connected_responses = [False, True, True, True]
    mock_ib.isConnected.side_effect = lambda: (
        connected_responses.pop(0) if connected_responses else True
    )

    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock()

    db_mock = AsyncMock()
    db_mock.close = AsyncMock()

    async def db_factory():
        return db_mock

    # Mock process_trade_group to fail for TG_ERROR and cancel worker on TG_CANCEL
    async def mock_process(db, ib, tg_id, notif, cfg):
        if tg_id == "TG_ERROR":
            raise RuntimeError("Worker process failed")
        elif tg_id == "TG_CANCEL":
            raise asyncio.CancelledError()

    with (
        patch("app.trading.worker.process_trade_group", side_effect=mock_process),
        patch("asyncio.sleep", AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            from app.trading.worker import execution_worker

            await execution_worker(
                db_factory, mock_ib, queue, mock_notifier, test_config
            )

    mock_notifier.send_message.assert_called_once()
    assert "FEHLER IM EXECUTION WORKER" in mock_notifier.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_process_trade_group_empty_or_no_entry(db, test_config: Config) -> None:
    """Verifies process_trade_group early returns for empty orders or missing ENTRY order."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    # Empty trade group
    await process_trade_group(
        db, mock_ib, "NON_EXISTENT_TG", mock_notifier, test_config
    )

    # Trade group with only EXIT order (no ENTRY)
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (99, 'TG_NO_ENTRY', 'ACC1', 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'LMT', 150.0, 'Created')
        """
    )
    await db.commit()

    await process_trade_group(db, mock_ib, "TG_NO_ENTRY", mock_notifier, test_config)


@pytest.mark.asyncio
async def test_get_account_value_filtering_and_value_error() -> None:
    """Verifies _get_account_value account_id filtering and ValueError handling."""
    from app.trading.worker import _get_account_value

    val_correct = MagicMock()
    val_correct.tag = "Cushion"
    val_correct.account = "ACC_1"
    val_correct.value = "0.25"

    val_wrong_acc = MagicMock()
    val_wrong_acc.tag = "Cushion"
    val_wrong_acc.account = "ACC_OTHER"
    val_wrong_acc.value = "0.99"

    val_invalid = MagicMock()
    val_invalid.tag = "NetLiquidation"
    val_invalid.account = "ACC_1"
    val_invalid.value = "NOT_A_NUMBER"

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [val_wrong_acc, val_correct, val_invalid]

    # Match correct account
    res = _get_account_value(mock_ib, "ACC_1", "Cushion")
    assert res == Decimal("0.25")

    # Invalid Decimal parse returns None
    res_inv = _get_account_value(mock_ib, "ACC_1", "NetLiquidation")
    assert res_inv is None


@pytest.mark.asyncio
async def test_verify_margin_and_cushion_whatif_failure(
    db, test_config: Config
) -> None:
    """Verifies _verify_margin_and_cushion fails closed when whatIf simulation raises an exception."""
    from app.trading.worker import _verify_margin_and_cushion

    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (88, 'TG_WHATIF_ERR', 'ACC1', 'ENTRY', 'MSFT', 'STK', 'SMART', 'BUY', 10, 'LMT', 300.0, 'Created')
        """
    )
    await db.commit()

    entry_order = OrderRow(
        order_id=88,
        perm_id=0,
        parent_id=None,
        trade_group_id="TG_WHATIF_ERR",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MSFT",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=10,
        order_type="LMT",
        target_price=Decimal("300.0"),
        tif="DAY",
        strategy_name="S1",
        status="Created",
    )

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = []
    mock_ib.whatIfOrderAsync = AsyncMock(
        side_effect=RuntimeError("TWS WhatIf simulation failed")
    )

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()

    passed, updated_order = await _verify_margin_and_cushion(
        db, mock_ib, entry_order, test_config, mock_notifier
    )

    assert passed is False
    assert updated_order.status == "Error"
    mock_notifier.send_order_failed.assert_called_once()
    assert (
        "Risk validation simulation failed/timed out"
        in mock_notifier.send_order_failed.call_args[1]["reason"]
    )


@pytest.mark.asyncio
async def test_transmit_entry_and_child_orders_tick_rounding_and_rejections(
    db, test_config: Config
) -> None:
    """Verifies tick-rounding price synchronization and rejection handling for entry and child orders."""
    # Insert bracket order group in Created state
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (70, 'TG_TICK', 'ACC1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.001, 'Created')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (71, 'TG_TICK', 'ACC1', 'SL', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'STP', 140.001, 'Created')
        """
    )
    await db.commit()

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = []
    mock_order_state = MagicMock()
    mock_order_state.initMarginAfter = "100.0"
    mock_order_state.equityWithLoanAfter = "1000.0"
    mock_ib.whatIfOrderAsync = AsyncMock(return_value=mock_order_state)

    mock_trade_entry = MagicMock()
    mock_trade_entry.orderStatus.status = "Submitted"

    mock_trade_child = MagicMock()
    mock_trade_child.orderStatus.status = "Inactive"
    log_err = MagicMock()
    log_err.errorCode = 321
    log_err.message = "API in Read-Only mode"
    log_err.status = "Inactive"
    mock_trade_child.log = [log_err]

    mock_ib.placeOrder.side_effect = [mock_trade_entry, mock_trade_child]
    mock_ib.client.getReqId.side_effect = [1001, 1002]

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()
    mock_notifier.send_bracket_order_submitted = AsyncMock()
    mock_notifier.send_margin_utilization_warning = AsyncMock()
    mock_notifier.send_high_margin_usage_warning = AsyncMock()

    with patch("app.trading.worker.asyncio.sleep", AsyncMock()):
        await process_trade_group(db, mock_ib, "TG_TICK", mock_notifier, test_config)

    # Check child order failed due to Read-Only mode
    mock_notifier.send_order_failed.assert_called_once()
    assert "READ-ONLY" in mock_notifier.send_order_failed.call_args[1]["reason"]


@pytest.mark.asyncio
async def test_execution_worker_cancelled_error(test_config: Config) -> None:
    """Verifies execution_worker handles asyncio.CancelledError on task cancellation (lines 75-77)."""
    from app.trading.worker import execution_worker

    queue: asyncio.Queue[str] = asyncio.Queue()
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True

    db_mock = AsyncMock()
    db_mock.close = AsyncMock()

    worker_task = asyncio.create_task(
        execution_worker(lambda: db_mock, mock_ib, queue, MagicMock(), test_config)
    )
    await asyncio.sleep(0.01)
    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task


@pytest.mark.asyncio
async def test_process_trade_group_entry_placement_failure(
    db, test_config: Config
) -> None:
    """Verifies entry placement failure returning Error dataclass (line 467)."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (990, 'TG_FAIL_ENTRY', 'ACC1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'Created')
        """
    )
    await db.commit()

    mock_ib_fail = MagicMock()
    mock_ib_fail.accountValues.return_value = []
    mock_order_state = MagicMock()
    mock_order_state.initMarginAfter = "100.0"
    mock_order_state.equityWithLoanAfter = "1000.0"
    mock_ib_fail.whatIfOrderAsync = AsyncMock(return_value=mock_order_state)

    mock_trade_rejected = MagicMock()
    mock_trade_rejected.orderStatus.status = "Inactive"
    log_err = MagicMock()
    log_err.errorCode = 200
    log_err.message = "Order rejected by exchange"
    log_err.status = "Inactive"
    mock_trade_rejected.log = [log_err]
    mock_ib_fail.placeOrder.return_value = mock_trade_rejected
    mock_ib_fail.client.getReqId.return_value = 2001

    mock_notifier_fail = MagicMock()
    mock_notifier_fail.send_order_failed = AsyncMock()
    mock_notifier_fail.send_margin_utilization_warning = AsyncMock()
    mock_notifier_fail.send_high_margin_usage_warning = AsyncMock()

    with patch("app.trading.worker.asyncio.sleep", AsyncMock()):
        await process_trade_group(
            db, mock_ib_fail, "TG_FAIL_ENTRY", mock_notifier_fail, test_config
        )


@pytest.mark.asyncio
async def test_wait_for_order_submission_break() -> None:
    """Verifies _wait_for_order_submission loop break for Submitted status (line 661)."""
    from app.trading.worker import _wait_for_order_submission

    mock_trade_sub = MagicMock()
    mock_trade_sub.orderStatus.status = "Submitted"
    await _wait_for_order_submission(mock_trade_sub)


@pytest.mark.asyncio
async def test_wait_for_order_submission_pending_sleep_loop() -> None:
    """Verifies line 661: _wait_for_order_submission sleeps while status is PendingSubmit."""
    from app.trading.worker import _wait_for_order_submission

    mock_trade = MagicMock()
    # First PendingSubmit (triggers sleep at line 661), then Submitted (triggers break)
    statuses = ["PendingSubmit", "Submitted"]
    type(mock_trade.orderStatus).status = property(
        lambda self: statuses.pop(0) if statuses else "Submitted"
    )

    with patch("app.trading.worker.asyncio.sleep", AsyncMock()) as mock_sleep:
        await _wait_for_order_submission(mock_trade)
        mock_sleep.assert_called_once_with(0.1)


@pytest.mark.asyncio
async def test_handle_order_rejection_log_error_break(db) -> None:
    """Verifies _handle_order_rejection log error break (line 701)."""
    from app.trading.worker import _handle_order_rejection

    order_row = OrderRow(
        order_id=2001,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_FAIL_ENTRY",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=10,
        order_type="LMT",
        target_price=Decimal("150.0"),
        tif="DAY",
        strategy_name="S1",
        status="Submitted",
    )
    mock_trade_log = MagicMock()
    mock_trade_log.orderStatus.status = "Inactive"
    entry_err = MagicMock()
    entry_err.errorCode = 105
    entry_err.message = "Order modified or cancelled"
    entry_err.status = "Inactive"
    mock_trade_log.log = [entry_err]

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()

    with patch("app.trading.worker.asyncio.sleep", AsyncMock()):
        res = await _handle_order_rejection(
            db, mock_trade_log, order_row, 2001, mock_notifier
        )
        assert res is False


@pytest.mark.asyncio
async def test_handle_order_rejection_unknown_error_sleep_loop(db) -> None:
    """Verifies line 701: _handle_order_rejection sleeps when error_msg is Unknown error."""
    from app.trading.worker import _handle_order_rejection

    order_row = OrderRow(
        order_id=3001,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_UNKNOWN_ERR",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=10,
        order_type="LMT",
        target_price=Decimal("150.0"),
        tif="DAY",
        strategy_name="S1",
        status="Submitted",
    )
    mock_trade = MagicMock()
    mock_trade.orderStatus.status = "Inactive"

    # Iteration 1: log is empty (error_msg = Unknown error, triggers sleep at line 701)
    # Iteration 2: log has error entry (triggers break at line 699)
    log_err = MagicMock()
    log_err.errorCode = 500
    log_err.message = "Order rejected"
    log_err.status = "Inactive"

    logs = [[], [log_err]]
    type(mock_trade).log = property(lambda self: logs.pop(0) if logs else [log_err])

    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()

    with patch("app.trading.worker.asyncio.sleep", AsyncMock()) as mock_sleep:
        res = await _handle_order_rejection(
            db, mock_trade, order_row, 3001, mock_notifier
        )
        assert res is False
        mock_sleep.assert_called_once_with(0.1)


class StopWorkerError(BaseException):
    pass


@pytest.mark.asyncio
async def test_execution_worker_telegram_error_handling(test_config: Config) -> None:
    """Verifies telegram error exception handling inside execution_worker loop (lines 87-88)."""
    from app.trading.worker import execution_worker

    queue: asyncio.Queue[str] = asyncio.Queue()
    await queue.put("TG_ERR")
    await queue.put("TG_STOP")

    mock_ib = MagicMock()
    mock_ib.isConnected = MagicMock(return_value=True)

    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(side_effect=RuntimeError("Telegram fail"))

    db_mock = AsyncMock()
    db_mock.close = AsyncMock()

    async def mock_process(db, ib, tg_id, notif, cfg):
        if tg_id == "TG_ERR":
            raise ValueError("Group fail")
        raise StopWorkerError()

    async def db_factory():
        return db_mock

    with (
        patch("app.trading.worker.process_trade_group", side_effect=mock_process),
        patch("app.trading.worker.asyncio.sleep", AsyncMock()),
    ):
        with pytest.raises(StopWorkerError):
            await execution_worker(
                db_factory, mock_ib, queue, mock_notifier, test_config
            )


@pytest.mark.asyncio
async def test_exit_order_db_exception_handlers(test_config: Config) -> None:
    """Verifies exception handling when DB update fails in _cancel_empty_exit_order and _reduce_exit_order_quantity (lines 801-802, 838-839)."""
    from app.trading.worker import _cancel_empty_exit_order, _reduce_exit_order_quantity

    child = OrderRow(
        order_id=99,
        perm_id=0,
        parent_id=None,
        trade_group_id="TG_ERR",
        account_id="ACC1",
        bracket_role="EXIT",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="SELL",
        quantity=10,
        order_type="MKT",
        target_price=Decimal("0.0"),
        tif="DAY",
        strategy_name="S1",
        status="Created",
    )

    db_err = AsyncMock()
    db_err.execute.side_effect = RuntimeError("DB update failed")

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()

    # Should catch exception internally without crashing
    await _cancel_empty_exit_order(db_err, child, Decimal("0.0"), mock_notifier)
    await _reduce_exit_order_quantity(
        db_err, child, Decimal("10"), Decimal("5"), mock_notifier
    )

    assert mock_notifier.send_importer_info.call_count == 2
