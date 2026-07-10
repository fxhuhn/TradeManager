from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.trading.callbacks import TwsCallbacksManager


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
    # DB-Status prüfen
    async with db.execute(
        "SELECT status, perm_id FROM orders WHERE order_id = 42"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Filled"
        assert row["perm_id"] == 9876

    # Telegram-Nachricht prüfen
    mock_notifier.send_order_filled.assert_called_once()
    kwargs = mock_notifier.send_order_filled.call_args[1]
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["action"] == "BUY"
    assert kwargs["quantity"] == 100
    assert kwargs["execution_price"] == 150.00

    # Kein Settlement für ENTRY-Order
    mock_trigger_settlement.assert_not_called()


@pytest.mark.asyncio
async def test_callbacks_exit_settlement_trigger(db, mock_config: Config) -> None:
    """Prüft, dass bei Ausführung einer Exit-Order (TP) das Settlement getriggert wird."""
    # 1. Parent-Order (ENTRY) einfuegen (erforderlich wegen FK-Constraint)
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
    # 2. Exit-Order (TP) im Status 'Submitted' einfuegen
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
        await manager._process_status_change(
            order_id=43, mapped_status="Filled", permanent_id=9877
        )
    finally:
        db.close = original_close

    # 2. Assertions
    mock_notifier.send_order_filled.assert_called_once()
    mock_trigger_settlement.assert_called_once_with("G1", "A1")


@pytest.mark.asyncio
async def test_loc_order_cancel_not_near_close(db, mock_config: Config) -> None:
    """Prüft, dass bei einer LOC-Order, die weit vor Marktschluss storniert wird, keine Preisprüfung erfolgt."""
    # 1. Test-Order (LOC) einfuegen
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

    # 10:00 Uhr New York-Zeit (deutlich vor Marktschluss)
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
    # 1. Test-Order (LOC SELL) einfuegen
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
    mock_bar.close = 105.00  # Preis wurde fuer SELL erreicht (105 >= 100)
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

    # 16:05 Uhr New York-Zeit (nach Marktschluss)
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
    # 1. Test-Order (LOC SELL) einfuegen
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
    mock_bar.close = 95.00  # Preis wurde fuer SELL nicht erreicht (95 < 100)
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

    # 16:05 Uhr New York-Zeit (nach Marktschluss)
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
    # 1. Test-Order (LOC BUY) einfuegen
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
    mock_bar.close = 95.00  # Preis wurde fuer BUY erreicht (95 <= 100)
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

    # 16:05 Uhr New York-Zeit (nach Marktschluss)
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
