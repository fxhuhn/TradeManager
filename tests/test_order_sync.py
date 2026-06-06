import asyncio
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
        dead_order_threshold_min=15,
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
