# filename: tests/trading/test_recovery.py
"""Unit and integration tests for state recovery and broker position reconciliation in app.trading.recovery."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.services.alert_watcher import order_status_sync_loop
from app.trading.recovery import reconcile_broker_positions, run_recovery


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

    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=mock_queue,
        notifier=mock_notifier,
        trigger_settlement_callback=mock_trigger_settlement,
        config=mock_config,
    )

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
    mock_interactive_brokers.fills.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_order_filled = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()
    mock_trigger_settlement = AsyncMock()

    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=mock_queue,
        notifier=mock_notifier,
        trigger_settlement_callback=mock_trigger_settlement,
        config=mock_config,
    )

    async with db.execute("SELECT status FROM orders WHERE order_id = 177") as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Filled"

    async with db.execute("SELECT * FROM executions WHERE order_id = 177") as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["exec_id"] == "RECOVERED_177"
        assert abs(row["price"] - 115.17) < 0.001
        assert row["qty"] == 21.0

    mock_notifier.send_order_filled.assert_called_once_with(
        symbol="BG",
        bracket_role="ENTRY",
        action="BUY",
        quantity=Decimal("21"),
        execution_price=Decimal("115.17"),
        order_type="LMT",
        order_id=177,
        strategy_name="DipBuyer",
        limit_price=Decimal("115.17"),
    )


@pytest.mark.asyncio
async def test_recovery_ignores_negative_order_ids(db, mock_config: Config) -> None:
    """
    Prüft, dass run_recovery Orders mit negativer ID (temporäre lokale ID)
    ignoriert/nicht mit TWS abgleicht und sie stattdessen neu einreiht.
    """
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

    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=mock_queue,
        notifier=mock_notifier,
        trigger_settlement_callback=mock_trigger_settlement,
        config=mock_config,
    )

    async with db.execute(
        "SELECT status, perm_id FROM orders WHERE order_id = -1"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Created"
        assert row["perm_id"] == 0

    assert mock_queue.qsize() == 1
    assert await mock_queue.get() == "918_TurnoverTiming_0.5_MU"


@pytest.mark.asyncio
async def test_recovery_recovers_filled_order_downtime(db, mock_config: Config) -> None:
    """
    Prüft, dass eine Submitted Order, die während der Downtime in TWS gefüllt wurde,
    korrekt auf 'Filled' gesetzt wird, eine Benachrichtigung sendet und das Settlement triggert.
    """
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

    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=mock_queue,
        notifier=mock_notifier,
        trigger_settlement_callback=mock_trigger_settlement,
        config=mock_config,
    )

    async with db.execute("SELECT status FROM orders WHERE order_id = 180") as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Filled"

    mock_notifier.send_order_filled.assert_called_once_with(
        symbol="XYZ",
        bracket_role="ENTRY",
        action="BUY",
        quantity=Decimal("10"),
        execution_price=Decimal("50.0"),
        order_type="LMT",
        order_id=180,
        strategy_name="DipBuyer",
        limit_price=Decimal("50.0"),
    )

    await asyncio.sleep(0.1)
    mock_trigger_settlement.assert_called_once_with("895_DipBuyer_XYZ", "U19605236")


@pytest.mark.asyncio
async def test_recovery_cancels_ghost_order(db, mock_config: Config) -> None:
    """Prüft, dass run_recovery Ghost Orders (Submitted in DB, nicht in TWS) storniert."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status
        ) VALUES (999, 111, NULL, 'G_GHOST', 'U19605236', 'ENTRY', 'GHOST', 'STK', 'SMART', 'BUY', 10, 'LMT', 50.0, 'DAY', 'DipBuyer', 'Submitted')
        """
    )
    await db.commit()

    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.reqOpenOrdersAsync = AsyncMock()
    mock_interactive_brokers.reqCompletedOrdersAsync = AsyncMock()
    mock_interactive_brokers.openTrades.return_value = []
    mock_interactive_brokers.trades.return_value = []
    mock_interactive_brokers.positions.return_value = []
    mock_interactive_brokers.fills.return_value = []

    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)

    await run_recovery(
        database_connection=db,
        interactive_brokers_session=mock_interactive_brokers,
        queue=asyncio.Queue(),
        notifier=mock_notifier,
        trigger_settlement_callback=AsyncMock(),
        config=mock_config,
    )

    async with db.execute("SELECT status FROM orders WHERE order_id = 999") as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Cancelled"

    mock_notifier.send_message.assert_called_once()
    assert "GHOST ORDER RECOVERED" in mock_notifier.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_reconcile_broker_positions_recovers_unassigned_position(db) -> None:
    """
    Prüft, dass reconcile_broker_positions bei einer Diskrepanz zwischen Broker
    und DB synthetische ENTRY-Orders (strategy_name=None) und Executions anlegt.
    """
    mock_position = MagicMock()
    mock_position.account = "U19605236"
    mock_position.contract.symbol = "AKAM"
    mock_position.contract.currency = "USD"
    mock_position.position = 15.0
    mock_position.avgCost = 133.48

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]

    mock_notifier = MagicMock()
    mock_notifier.send_unassigned_position_recovered = AsyncMock()

    await reconcile_broker_positions(db, mock_ib, mock_notifier)

    async with db.execute(
        "SELECT order_id, trade_group_id, symbol, action, quantity, bracket_role, status, strategy_name FROM orders WHERE symbol = 'AKAM'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["order_id"] < 0
        assert row["trade_group_id"] == "UNASSIGNED_AKAM_U19605236"
        assert row["action"] == "BUY"
        assert row["quantity"] == 15
        assert row["bracket_role"] == "ENTRY"
        assert row["status"] == "Filled"
        assert row["strategy_name"] is None

    async with db.execute(
        "SELECT exec_id, order_id, price, qty, currency FROM executions WHERE order_id = ?",
        (row["order_id"],),
    ) as cursor:
        exec_row = await cursor.fetchone()
        assert exec_row is not None
        assert exec_row["exec_id"] == f"RECOVERED_POS_AKAM_{abs(row['order_id'])}"
        assert Decimal(str(exec_row["price"])) == Decimal("133.48")
        assert Decimal(str(exec_row["qty"])) == Decimal("15.0")
        assert exec_row["currency"] == "USD"

    mock_notifier.send_unassigned_position_recovered.assert_called_once_with(
        symbol="AKAM",
        quantity=Decimal("15"),
        avg_cost=Decimal("133.48"),
        account_id="U19605236",
    )


@pytest.mark.asyncio
async def test_reconcile_broker_positions_skips_when_synced(db) -> None:
    """
    Prüft, dass reconcile_broker_positions keine neuen Orders/Executions anlegt,
    wenn die DB-Stückzahl bereits exakt mit dem Broker übereinstimmt.
    """
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (100, 'TG_ALAB', 'U19605236', 'ENTRY', 'ALAB', 'STK', 'SMART', 'BUY', 10, 'MKT', 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO executions (exec_id, order_id, price, qty, commission, currency, executed_at)
        VALUES ('EXEC_ALAB_1', 100, '400.00', '10.0', '0.0', 'USD', '2026-08-01')
        """
    )
    await db.commit()

    mock_position = MagicMock()
    mock_position.account = "U19605236"
    mock_position.contract.symbol = "ALAB"
    mock_position.contract.currency = "USD"
    mock_position.position = 10.0
    mock_position.avgCost = 400.00

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]

    mock_notifier = MagicMock()
    mock_notifier.send_unassigned_position_recovered = AsyncMock()

    await reconcile_broker_positions(db, mock_ib, mock_notifier)

    async with db.execute(
        "SELECT COUNT(*) as count FROM orders WHERE symbol = 'ALAB'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row["count"] == 1

    mock_notifier.send_unassigned_position_recovered.assert_not_called()
