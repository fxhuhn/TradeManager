import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.services.alert_watcher import order_status_sync_loop
from app.trading.recovery import run_recovery


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
        order_sync_interval_s=1,  # Kurzes Intervall fuer Tests
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
async def test_order_status_sync_loop_calls_run_recovery(mock_config: Config) -> None:
    """
    Prueft, dass der Sync-Hintergrund-Loop periodisch
    die run_recovery Logik aufruft.
    """
    mock_db_conn = AsyncMock()
    mock_db_conn.close = AsyncMock()

    async def db_factory():
        return mock_db_conn

    mock_interactive_brokers = MagicMock()
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()
    mock_trigger_settlement = AsyncMock()

    with patch(
        "app.services.alert_watcher.run_recovery", new_callable=AsyncMock
    ) as mock_run_recovery:
        # Loop starten
        sync_task = asyncio.create_task(
            order_status_sync_loop(
                db_factory=db_factory,
                interactive_brokers=mock_interactive_brokers,
                queue=mock_queue,
                notifier=mock_notifier,
                trigger_settlement_callback=mock_trigger_settlement,
                config=mock_config,
                interval_seconds=1,
            )
        )

        await asyncio.sleep(1.5)
        sync_task.cancel()

        try:
            await sync_task
        except asyncio.CancelledError:
            pass

        mock_run_recovery.assert_called()


@pytest.mark.asyncio
async def test_recovery_syncs_presubmitted_order_to_submitted(
    db, mock_config: Config
) -> None:
    """
    Prueft, dass run_recovery eine lokale Order im Status 'PreSubmitted',
    die in TWS aktiv ist, in der Datenbank auf 'Submitted' aktualisiert.
    """
    # 1. Test-Order im Status 'PreSubmitted' in die echte In-Memory-Datenbank einfuegen
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
            "20260601_TurnoverTiming_0.5_001",
            "U19605236",
            "ENTRY",
            "MU",
            "STK",
            "SMART",
            "BUY",
            2,
            "LMT",
            938.82,
            "DAY",
            "TurnoverTiming_0.5",
            "PreSubmitted",
        ),
    )
    await db.commit()

    # 2. IB/TWS Mocking fuer offene Trades
    mock_trade = MagicMock()
    mock_trade.order.orderId = 42
    mock_trade.order.permId = 987654321
    mock_trade.orderStatus.status = "Submitted"

    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.reqOpenOrdersAsync = AsyncMock()
    mock_interactive_brokers.reqCompletedOrdersAsync = AsyncMock()
    mock_interactive_brokers.openTrades.return_value = [mock_trade]
    mock_interactive_brokers.trades.return_value = [mock_trade]

    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()
    mock_trigger_settlement = AsyncMock()

    # 3. run_recovery ausfuehren
    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=mock_queue,
        notifier=mock_notifier,
        trigger_settlement_callback=mock_trigger_settlement,
        config=mock_config,
    )

    # 4. Assertions: check the status of order 42 in the database
    async with db.execute(
        "SELECT status, perm_id FROM orders WHERE order_id = 42"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Submitted"
        assert row["perm_id"] == 987654321


@pytest.mark.asyncio
async def test_recovery_recovers_filled_entry_with_active_child(
    db, mock_config: Config
) -> None:
    """
    Prüft, dass eine ENTRY-Order, die in TWS nicht mehr aktiv oder abgeschlossen gelistet ist,
    aber eine aktive Child-Order (z. B. TP) besitzt, korrekt als 'Filled' rekonstruiert wird.
    """
    # 1. Parent-Order (ENTRY) und Child-Order (TP) in die In-Memory-Datenbank einfügen
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            177,
            48380410,
            None,
            "890_DipBuyer_BG",
            "U19605236",
            "ENTRY",
            "BG",
            "STK",
            "SMART",
            "BUY",
            21,
            "LMT",
            115.17,
            "DAY",
            "DipBuyer",
            "Submitted",
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
            178,
            48380411,
            177,
            "890_DipBuyer_BG",
            "U19605236",
            "TP",
            "BG",
            "STK",
            "SMART",
            "SELL",
            21,
            "LOC",
            122.47,
            "DAY",
            "DipBuyer",
            "Submitted",
        ),
    )
    await db.commit()

    # 2. IB/TWS Mocking:
    # Parent (177) ist nicht in TWS aktiv/komplettiert (wird also von openTrades/trades nicht zurückgegeben).
    # Child (178) ist in TWS aktiv (openTrades).
    mock_trade_child = MagicMock()
    mock_trade_child.order.orderId = 178
    mock_trade_child.order.permId = 48380411
    mock_trade_child.orderStatus.status = "Submitted"

    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.reqOpenOrdersAsync = AsyncMock()
    mock_interactive_brokers.reqCompletedOrdersAsync = AsyncMock()
    mock_interactive_brokers.openTrades.return_value = [mock_trade_child]
    mock_interactive_brokers.trades.return_value = [mock_trade_child]
    mock_interactive_brokers.positions.return_value = []

    # Keine Fills in TWS vorhanden -> testet auch den Fallback
    mock_interactive_brokers.fills.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_order_filled = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()
    mock_trigger_settlement = AsyncMock()

    # 3. run_recovery ausführen
    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=mock_queue,
        notifier=mock_notifier,
        trigger_settlement_callback=mock_trigger_settlement,
        config=mock_config,
    )

    # 4. Assertions:
    # Parent-Order (177) muss auf 'Filled' aktualisiert worden sein
    async with db.execute("SELECT status FROM orders WHERE order_id = 177") as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Filled"

    # Eine Fallback-Ausführung für die Parent-Order muss angelegt worden sein
    async with db.execute("SELECT * FROM executions WHERE order_id = 177") as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["exec_id"] == "RECOVERED_177"
        assert abs(row["price"] - 115.17) < 0.001
        assert row["qty"] == 21.0

    # Assert Telegram Notifier was called for filled trade
    mock_notifier.send_order_filled.assert_called_once_with(
        symbol="BG",
        bracket_role="ENTRY",
        action="BUY",
        quantity=Decimal("21"),
        price=Decimal("115.17"),
        order_type="LMT",
        order_id=177,
        strategy_name="DipBuyer",
    )


@pytest.mark.asyncio
async def test_recovery_ignores_negative_order_ids(db, mock_config: Config) -> None:
    """
    Prüft, dass run_recovery Orders mit negativer ID (temporäre lokale ID)
    ignoriert/nicht mit TWS abgleicht und sie stattdessen neu einreiht.
    """
    # 1. Test-Order mit negativer ID in die echte In-Memory-Datenbank einfügen
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            -1,
            0,
            None,
            "918_TurnoverTiming_0.5_MU",
            "U19605236",
            "ENTRY",
            "MU",
            "STK",
            "SMART",
            "BUY",
            2,
            "LMT",
            1086.72,
            "DAY",
            "TurnoverTiming_0.5",
            "Created",
        ),
    )
    await db.commit()

    # 2. IB/TWS Mocking: Ein gefälschter aktiver Trade mit ID -1
    mock_trade = MagicMock()
    mock_trade.order.orderId = -1
    mock_trade.order.permId = 0
    mock_trade.orderStatus.status = "PreSubmitted"

    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.reqOpenOrdersAsync = AsyncMock()
    mock_interactive_brokers.reqCompletedOrdersAsync = AsyncMock()
    mock_interactive_brokers.openTrades.return_value = [mock_trade]
    mock_interactive_brokers.trades.return_value = [mock_trade]
    mock_interactive_brokers.positions.return_value = []

    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()
    mock_trigger_settlement = AsyncMock()

    # 3. run_recovery ausführen
    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=mock_queue,
        notifier=mock_notifier,
        trigger_settlement_callback=mock_trigger_settlement,
        config=mock_config,
    )

    # 4. Assertions:
    # Die Order mit ID -1 darf NICHT geändert worden sein (Status bleibt 'Created', perm_id bleibt 0)
    async with db.execute(
        "SELECT status, perm_id FROM orders WHERE order_id = -1"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Created"
        assert row["perm_id"] == 0

    # Aber die Trade-Gruppe muss in der Queue eingereiht worden sein
    assert mock_queue.qsize() == 1
    assert await mock_queue.get() == "918_TurnoverTiming_0.5_MU"


@pytest.mark.asyncio
async def test_recovery_recovers_filled_order_downtime(db, mock_config: Config) -> None:
    """
    Prüft, dass eine Submitted Order, die während der Downtime in TWS gefüllt wurde (Szenario 2),
    korrekt auf 'Filled' gesetzt wird, eine Benachrichtigung sendet und das Settlement triggert.
    """
    # 1. Test-Order im Status 'Submitted' in die echte In-Memory-Datenbank einfügen
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            180,
            48380420,
            None,
            "895_DipBuyer_XYZ",
            "U19605236",
            "ENTRY",
            "XYZ",
            "STK",
            "SMART",
            "BUY",
            10,
            "LMT",
            50.0,
            "DAY",
            "DipBuyer",
            "Submitted",
        ),
    )
    await db.commit()

    # 2. IB/TWS Mocking:
    # Die Order ist in completed trades (nicht in openTrades) mit Status "Filled"
    mock_trade = MagicMock()
    mock_trade.order.orderId = 180
    mock_trade.order.permId = 48380420
    mock_trade.orderStatus.status = "Filled"

    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.reqOpenOrdersAsync = AsyncMock()
    mock_interactive_brokers.reqCompletedOrdersAsync = AsyncMock()
    mock_interactive_brokers.openTrades.return_value = []
    mock_interactive_brokers.trades.return_value = [mock_trade]
    mock_interactive_brokers.fills.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_order_filled = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()
    mock_trigger_settlement = AsyncMock()

    # 3. run_recovery ausführen
    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=mock_queue,
        notifier=mock_notifier,
        trigger_settlement_callback=mock_trigger_settlement,
        config=mock_config,
    )

    # 4. Assertions:
    # Status muss auf 'Filled' sein
    async with db.execute("SELECT status FROM orders WHERE order_id = 180") as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Filled"

    # Notifier muss gerufen worden sein
    mock_notifier.send_order_filled.assert_called_once_with(
        symbol="XYZ",
        bracket_role="ENTRY",
        action="BUY",
        quantity=Decimal("10"),
        price=Decimal("50.0"),
        order_type="LMT",
        order_id=180,
        strategy_name="DipBuyer",
    )

    # Settlement-Trigger muss asynchron aufgerufen worden sein (wir warten kurz, da mit create_task gestartet)
    await asyncio.sleep(0.1)
    mock_trigger_settlement.assert_called_once_with("895_DipBuyer_XYZ", "U19605236")
