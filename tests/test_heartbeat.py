import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.main import TradingSystemOrchestrator
from app.trading.callbacks import TwsCallbacksManager


@pytest.fixture
def test_config() -> Config:
    """Erstellt eine Testkonfiguration für die Heartbeat-Prüfungen."""
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
        heartbeat_interval_s=0.1,  # Sehr kurzes Intervall für Tests
        heartbeat_timeout_s=0.05,  # Sehr kurzer Timeout für Tests
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
async def test_heartbeat_ping_success(test_config: Config) -> None:
    """Prüft, dass der Heartbeat bei erfolgreichem reqCurrentTimeAsync normal weiterläuft."""
    mock_ib = MagicMock()
    # reqCurrentTimeAsync liefert ein Future, das wir hier mit Mock-Uhrzeit auflösen
    future = asyncio.Future()
    future.set_result(dt.datetime.now())
    mock_ib.reqCurrentTimeAsync.return_value = future
    mock_ib.isConnected.return_value = True

    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=MagicMock(),
        database_path=MagicMock(),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    # Wir lassen den Loop genau einmal laufen, indem wir ihn nach einem kurzen Sleep abbrechen (Cancel)
    heartbeat_task = asyncio.create_task(orchestrator.heartbeat_loop())
    await asyncio.sleep(0.15)
    heartbeat_task.cancel()

    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    # reqCurrentTimeAsync muss aufgerufen worden sein
    mock_ib.reqCurrentTimeAsync.assert_called()
    # disconnect darf nicht aufgerufen worden sein
    mock_ib.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_ping_timeout_disconnects(test_config: Config) -> None:
    """Prüft, dass der Heartbeat bei Timeout die Verbindung trennt und alarmiert."""
    mock_ib = MagicMock()
    # reqCurrentTimeAsync bleibt blockiert und liefert kein Ergebnis (Timeout)
    future = asyncio.Future()  # Unresolved future to cause timeout
    mock_ib.reqCurrentTimeAsync.return_value = future
    mock_ib.isConnected.return_value = True

    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=MagicMock(),
        database_path=MagicMock(),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    # Loop starten
    heartbeat_task = asyncio.create_task(orchestrator.heartbeat_loop())
    await asyncio.sleep(0.15)
    heartbeat_task.cancel()

    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    # reqCurrentTimeAsync muss aufgerufen worden sein
    mock_ib.reqCurrentTimeAsync.assert_called()
    # disconnect MUSS aufgerufen worden sein wegen des Timeouts
    mock_ib.disconnect.assert_called_once()
    # Telegram Warnung muss rausgegangen sein
    mock_notifier.send_message.assert_called_once()
    assert "HEARTBEAT TIMEOUT" in mock_notifier.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_heartbeat_paused_during_restart_window(test_config: Config) -> None:
    """Prüft, dass der Heartbeat-Ping im Restart-Fenster um 12:00 Uhr ausgesetzt wird."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True

    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=MagicMock(),
        database_path=MagicMock(),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    # Wir mocken datetime.now(), so dass es genau 12:01:00 liefert (im Restart-Fenster)
    mock_now = dt.datetime(2026, 6, 21, 12, 1, 0)
    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.now.return_value = mock_now
        # Wir lassen den heartbeat_loop laufen
        heartbeat_task = asyncio.create_task(orchestrator.heartbeat_loop())
        await asyncio.sleep(0.15)
        heartbeat_task.cancel()

        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    # Im Restart-Fenster darf reqCurrentTimeAsync NICHT aufgerufen werden
    mock_ib.reqCurrentTimeAsync.assert_not_called()


@pytest.mark.asyncio
async def test_callbacks_planned_restart_disconnected(test_config: Config) -> None:
    """Prüft, dass das on_disconnected Callback bei einem geplanten Neustart die richtige Benachrichtigung sendet."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock(return_value=True)

    callbacks_manager = TwsCallbacksManager(
        db_factory=AsyncMock(),
        interactive_brokers=mock_ib,
        notifier=mock_notifier,
        config=test_config,
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    # 1. Szenario: Geplanter Neustart um 12:01 Uhr
    mock_now_planned = dt.datetime(2026, 6, 21, 12, 1, 0)
    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.now.return_value = mock_now_planned
        callbacks_manager.on_disconnected()
        # Kurzes Yield für die Background-Tasks
        await asyncio.sleep(0.01)

    # Warn-Status muss gesendet worden sein mit Sanduhr-Symbol (⏳)
    mock_notifier.send_system_status.assert_called_with(
        title="GEPLANTER NEUSTART (Gateway wird neu gestartet)",
        emoji="⏳",
    )

    # 2. Szenario: Ungeplanter Verbindungsabbruch um 14:00 Uhr
    mock_now_unexpected = dt.datetime(2026, 6, 21, 14, 0, 0)
    mock_notifier.send_system_status.reset_mock()
    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.now.return_value = mock_now_unexpected
        callbacks_manager.on_disconnected()
        await asyncio.sleep(0.01)

    # Rotes Alarm-Symbol (🚨) muss gesendet worden sein
    mock_notifier.send_system_status.assert_called_with(
        title="VERBINDUNGSABBRUCH",
        emoji="🚨",
    )
