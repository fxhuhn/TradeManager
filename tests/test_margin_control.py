from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.trading.worker import process_trade_group


@pytest.fixture
def test_config() -> Config:
    """Erstellt eine Testkonfiguration für die Margin-Prüfungen."""
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
async def test_cushion_check_blocks_order(db, test_config: Config) -> None:
    """Prüft, dass die Order blockiert wird, wenn Cushion < min_cushion_pct."""
    # 1. Datenbank-Setup: Eine neue ENTRY-Order (Created)
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (-10, 'TG_CUSHION_FAIL', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'Created')
        """
    )
    await db.commit()

    # 2. IB-Mocking: Cushion bei 5% (0.05), was unter dem Limit von 10% (0.10) liegt
    mock_cushion_value = MagicMock()
    mock_cushion_value.tag = "Cushion"
    mock_cushion_value.value = "0.05"
    mock_cushion_value.account = "ACC_1"

    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [mock_cushion_value]
    mock_ib.positions.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_margin_limit_exceeded = AsyncMock(return_value=True)

    # 3. Ausführen
    await process_trade_group(
        db, mock_ib, "TG_CUSHION_FAIL", mock_notifier, test_config
    )

    # 4. Verifikation: Order muss auf Error gesetzt sein und Telegram gesendet
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

    # Cushion ist bei 20% (OK)
    mock_cushion_value = MagicMock()
    mock_cushion_value.tag = "Cushion"
    mock_cushion_value.value = "0.20"
    mock_cushion_value.account = "ACC_1"

    # What-If Simulation Rückgabe: Initial Margin nach Order = $90,000, Equity = $100,000.
    # Auslastung = 90% (über Limit von 80%)
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

    # Order in DB muss auf Error sein
    async with db.execute("SELECT status FROM orders WHERE order_id = -11") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Error"

    # Telegram Notification muss gesendet sein
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

    # Cushion bei 20% (OK)
    mock_cushion_value = MagicMock()
    mock_cushion_value.tag = "Cushion"
    mock_cushion_value.value = "0.20"
    mock_cushion_value.account = "ACC_1"

    # TotalCashValue bei $5,000. Kaufwert ist 100 * $150 = $15,000.
    # Zusätzliche Margin benötigt = $10,000
    mock_cash_value = MagicMock()
    mock_cash_value.tag = "TotalCashValue"
    mock_cash_value.value = "5000.0"
    mock_cash_value.account = "ACC_1"

    # What-If Simulation Rückgabe: Initial Margin nach Order = $10,000, Equity = $100,000.
    # Auslastung = 10% (unter Limit von 80% und unter Warnstufe von 50%)
    mock_what_if_info = MagicMock()
    mock_what_if_info.initMarginAfter = "10000.0"
    mock_what_if_info.maintMarginAfter = "8000.0"
    mock_what_if_info.equityWithLoanAfter = "100000.0"

    # Echte Order Placement Rückgabe
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

    # Verifizieren, dass die Warnung zur Margin-Nutzung abgesetzt wurde
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

    # Cushion bei 20% (OK)
    mock_cushion_value = MagicMock()
    mock_cushion_value.tag = "Cushion"
    mock_cushion_value.value = "0.20"
    mock_cushion_value.account = "ACC_1"

    # TotalCashValue bei $20,000 (reicht für Kaufwert von $15,000)
    mock_cash_value = MagicMock()
    mock_cash_value.tag = "TotalCashValue"
    mock_cash_value.value = "20000.0"
    mock_cash_value.account = "ACC_1"

    # What-If Simulation Rückgabe: Initial Margin nach Order = $60,000, Equity = $100,000.
    # Auslastung = 60% (über 50% Warnstufe, aber unter 80% Blockierungs-Limit)
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

    # Verifizieren, dass die Warnung gesendet wurde
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
    # Setup: Entry in 'Error' und Exit in 'Created'
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

    # Ausführen
    await process_trade_group(
        db, mock_ib, "TG_FAILED_ENTRY", mock_notifier, test_config
    )

    # Verifizieren: Die Exit-Order muss nun ebenfalls den Status 'Error' haben
    async with db.execute("SELECT status FROM orders WHERE order_id = -15") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Error"
