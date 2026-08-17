# filename: tests/integration/test_main.py
"""Integration tests for TradingSystemOrchestrator, TWS connection loop, and heartbeat monitoring."""

import asyncio
import datetime as dt
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.main import (
    TradingSystemOrchestrator,
    _attempt_connection,
    _enable_socket_keepalive,
    _initialize_config_and_logging,
    _setup_graceful_shutdown,
    _verify_database_integrity,
    connect_to_tws,
)
from app.trading.callbacks import TwsCallbacksManager


@pytest.fixture
def test_config() -> Config:
    """Fixture providing a valid system Configuration instance."""
    tws = TwsConfig(
        host="127.0.0.1",
        port=7496,
        client_id=0,
        connection_timeout_s=5.0,
        reconnect_initial_delay_s=0.01,  # Fast testing delay
        reconnect_max_attempts=3,
        reconnect_max_delay_s=0.1,
        request_timeout_s=5.0,
        completed_orders_timeout_s=5.0,
        heartbeat_interval_s=0.1,
        heartbeat_timeout_s=0.05,
    )
    app = AppConfig(
        max_retries=3,
        order_rate_limit_s=0.0,
        dead_order_threshold_minutes=15,
        alert_watcher_interval_s=60,
        csv_watcher_interval_s=60,
        order_sync_interval_s=1,
        retry_backoff_base_s=0.01,
        shutdown_join_timeout_s=0.05,
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
async def test_attempt_connection_success(test_config: Config) -> None:
    """Verifies that _attempt_connection returns True when connectAsync succeeds."""
    mock_ib = MagicMock()
    mock_ib.connectAsync = AsyncMock()

    with patch("app.main._enable_socket_keepalive") as mock_keepalive:
        result = await _attempt_connection(mock_ib, test_config, attempt=1)

        assert result is True
        mock_ib.connectAsync.assert_called_once_with("127.0.0.1", 7496, clientId=0)
        mock_keepalive.assert_called_once_with(mock_ib)


@pytest.mark.asyncio
async def test_attempt_connection_failure(test_config: Config) -> None:
    """Verifies that _attempt_connection returns False when connectAsync raises exception."""
    mock_ib = MagicMock()
    mock_ib.connectAsync = AsyncMock(side_effect=Exception("Connection refused"))

    result = await _attempt_connection(mock_ib, test_config, attempt=1)
    assert result is False


@pytest.mark.asyncio
async def test_connect_to_tws_success_on_first_try(test_config: Config) -> None:
    """Verifies that connect_to_tws succeeds immediately if first connection attempt works."""
    mock_ib = MagicMock()

    with patch("app.main._attempt_connection", new_callable=AsyncMock) as mock_attempt:
        mock_attempt.return_value = True
        result = await connect_to_tws(mock_ib, test_config)

        assert result is True
        mock_attempt.assert_called_once_with(mock_ib, test_config, 1)


@pytest.mark.asyncio
async def test_connect_to_tws_retries_and_succeeds(test_config: Config) -> None:
    """Verifies that connect_to_tws retries on connection failure and succeeds on subsequent try."""
    mock_ib = MagicMock()

    with patch("app.main._attempt_connection", new_callable=AsyncMock) as mock_attempt:
        mock_attempt.side_effect = [False, True]
        result = await connect_to_tws(mock_ib, test_config)

        assert result is True
        assert mock_attempt.call_count == 2


@pytest.mark.asyncio
async def test_connect_to_tws_exhausts_retries_and_fails(
    test_config: Config,
) -> None:
    """Verifies that connect_to_tws returns False after exhausting all reconnect attempts."""
    mock_ib = MagicMock()

    with patch("app.main._attempt_connection", new_callable=AsyncMock) as mock_attempt:
        mock_attempt.return_value = False
        result = await connect_to_tws(mock_ib, test_config)

        assert result is False
        assert mock_attempt.call_count == test_config.tws.reconnect_max_attempts


def test_enable_socket_keepalive_ignores_disconnected_ib() -> None:
    """Verifies that _enable_socket_keepalive returns early if IB client is not connected."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = False

    _enable_socket_keepalive(mock_ib)
    mock_ib.client.conn.transport.get_extra_info.assert_not_called()


def test_enable_socket_keepalive_handles_missing_socket() -> None:
    """Verifies that _enable_socket_keepalive exits cleanly if socket cannot be retrieved."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.client.conn.transport.get_extra_info.return_value = None

    _enable_socket_keepalive(mock_ib)
    mock_ib.client.conn.transport.get_extra_info.assert_called_once_with("socket")


def test_enable_socket_keepalive_applies_socket_options() -> None:
    """Verifies that _enable_socket_keepalive successfully configures socket keepalive options."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_socket = MagicMock()
    mock_ib.client.conn.transport.get_extra_info.return_value = mock_socket

    _enable_socket_keepalive(mock_ib)
    mock_socket.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


def test_enable_socket_keepalive_handles_socket_exception() -> None:
    """Verifies that _enable_socket_keepalive catches and logs keepalive exceptions cleanly."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_socket = MagicMock()
    mock_socket.setsockopt.side_effect = Exception("Socket error")
    mock_ib.client.conn.transport.get_extra_info.return_value = mock_socket

    _enable_socket_keepalive(mock_ib)


@pytest.mark.asyncio
async def test_verify_database_integrity_success() -> None:
    """Verifies that _verify_database_integrity returns database path on success."""
    mock_notifier = MagicMock()

    with patch(
        "app.main.verify_db_integrity", new_callable=AsyncMock
    ) as mock_integrity:
        mock_integrity.return_value = True
        path = await _verify_database_integrity(Path("/root"), mock_notifier)

        assert path == Path("/root/data/trading.db")
        mock_notifier.send_system_status.assert_not_called()


@pytest.mark.asyncio
async def test_verify_database_integrity_failure() -> None:
    """Verifies that _verify_database_integrity calls sys.exit(1) on integrity failure."""
    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()

    with (
        patch("app.main.verify_db_integrity", new_callable=AsyncMock) as mock_integrity,
        pytest.raises(SystemExit) as exit_info,
    ):
        mock_integrity.return_value = False
        await _verify_database_integrity(Path("/root"), mock_notifier)

    assert exit_info.value.code == 1
    mock_notifier.send_system_status.assert_called_once()


def test_initialize_config_and_logging_success() -> None:
    """Verifies that config is loaded and directory structure is initialized successfully."""
    mock_config = MagicMock()
    mock_config.app.log_file_path = "data/app.log"
    mock_config.app.log_rotation_backup_count = 5

    with (
        patch("app.main.load_config", return_value=mock_config),
        patch("pathlib.Path.mkdir") as mock_mkdir,
        patch("app.main.configure_logging") as mock_logging,
    ):
        config = _initialize_config_and_logging(Path("/root"))

        assert config == mock_config
        mock_mkdir.assert_called_once()
        mock_logging.assert_called_once()


def test_initialize_config_and_logging_failure() -> None:
    """Verifies that config loading failures call sys.exit(1)."""
    with (
        patch("app.main.load_config", side_effect=Exception("TOML error")),
        patch("app.main.configure_logging"),
        pytest.raises(SystemExit) as exit_info,
    ):
        _initialize_config_and_logging(Path("/root"))

    assert exit_info.value.code == 1


def test_setup_graceful_shutdown() -> None:
    """Verifies that signal handlers are added to the running asyncio loop."""
    mock_loop = MagicMock()
    mock_orchestrator = MagicMock()

    with patch("asyncio.get_running_loop", return_value=mock_loop):
        _setup_graceful_shutdown(mock_orchestrator)

        assert mock_loop.add_signal_handler.call_count >= 1


@pytest.mark.asyncio
async def test_orchestrator_graceful_shutdown(test_config: Config) -> None:
    """Verifies that graceful_shutdown cancels tasks and disconnects IB connection."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()

    mock_task = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=Path("/root"),
        database_path=Path("/root/data/trading.db"),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )
    orchestrator.tasks = (mock_task,)

    await orchestrator.graceful_shutdown()

    mock_task.cancel.assert_called_once()
    mock_ib.disconnect.assert_called_once()
    assert mock_notifier.send_system_status.call_count == 2


@pytest.mark.asyncio
async def test_heartbeat_ping_success(test_config: Config) -> None:
    """Prüft, dass der Heartbeat bei erfolgreichem reqCurrentTimeAsync normal weiterläuft."""
    mock_ib = MagicMock()
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

    heartbeat_task = asyncio.create_task(orchestrator.heartbeat_loop())
    await asyncio.sleep(0.15)
    heartbeat_task.cancel()

    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    mock_ib.reqCurrentTimeAsync.assert_called()
    mock_ib.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_ping_timeout_disconnects(test_config: Config) -> None:
    """Prüft, dass der Heartbeat bei Timeout die Verbindung trennt und alarmiert."""
    mock_ib = MagicMock()
    future = asyncio.Future()
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

    heartbeat_task = asyncio.create_task(orchestrator.heartbeat_loop())
    await asyncio.sleep(0.15)
    heartbeat_task.cancel()

    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    mock_ib.reqCurrentTimeAsync.assert_called()
    mock_ib.disconnect.assert_called_once()
    mock_notifier.send_message.assert_called_once()
    assert "HEARTBEAT TIMEOUT" in mock_notifier.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_heartbeat_paused_during_restart_window(test_config: Config) -> None:
    """Prüft, dass der Heartbeat-Ping im Restart-Fenster am Sonntag um 12:00 Uhr ausgesetzt wird."""
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

    # 1. Sonntag 12:01 Uhr (geplantes Wartungsfenster) -> Heartbeat pausiert
    mock_now_sunday = dt.datetime(2026, 6, 21, 12, 1, 0)
    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.side_effect = dt.datetime
        mock_datetime.now.return_value = mock_now_sunday
        heartbeat_task = asyncio.create_task(orchestrator.heartbeat_loop())
        await asyncio.sleep(0.15)
        heartbeat_task.cancel()

        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    mock_ib.reqCurrentTimeAsync.assert_not_called()

    # 2. Montag 12:01 Uhr (kein Wartungsfenster) -> Heartbeat aktiv
    mock_now_monday = dt.datetime(2026, 6, 22, 12, 1, 0)
    future = asyncio.Future()
    future.set_result(mock_now_monday)
    mock_ib.reqCurrentTimeAsync.return_value = future

    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.side_effect = dt.datetime
        mock_datetime.now.return_value = mock_now_monday
        heartbeat_task = asyncio.create_task(orchestrator.heartbeat_loop())
        await asyncio.sleep(0.15)
        heartbeat_task.cancel()

        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    mock_ib.reqCurrentTimeAsync.assert_called()


@pytest.mark.asyncio
async def test_callbacks_planned_restart_disconnected(test_config: Config) -> None:
    """Prüft, dass das on_disconnected Callback nur sonntags um 12:00 Uhr einen geplanten Neustart meldet."""
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

    # 1. Sonntag 12:01 Uhr -> Geplanter Neustart
    mock_now_planned = dt.datetime(2026, 6, 21, 12, 1, 0)
    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.side_effect = dt.datetime
        mock_datetime.now.return_value = mock_now_planned
        callbacks_manager.on_disconnected()
        await asyncio.sleep(0.01)

    mock_notifier.send_system_status.assert_called_with(
        title="GEPLANTER NEUSTART (Gateway wird neu gestartet)",
        emoji="⏳",
    )

    # 2. Montag 12:01 Uhr (Wochentag) -> Unerwarteter Verbindungsabbruch
    mock_now_weekday = dt.datetime(2026, 6, 22, 12, 1, 0)
    mock_notifier.send_system_status.reset_mock()
    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.side_effect = dt.datetime
        mock_datetime.now.return_value = mock_now_weekday
        callbacks_manager.on_disconnected()
        await asyncio.sleep(0.01)

    mock_notifier.send_system_status.assert_called_with(
        title="VERBINDUNGSABBRUCH",
        emoji="🚨",
    )

    # 3. Sonntag 14:00 Uhr (Falsche Uhrzeit) -> Unerwarteter Verbindungsabbruch
    mock_now_unexpected = dt.datetime(2026, 6, 21, 14, 0, 0)
    mock_notifier.send_system_status.reset_mock()
    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.side_effect = dt.datetime
        mock_datetime.now.return_value = mock_now_unexpected
        callbacks_manager.on_disconnected()
        await asyncio.sleep(0.01)

    mock_notifier.send_system_status.assert_called_with(
        title="VERBINDUNGSABBRUCH",
        emoji="🚨",
    )


@pytest.mark.asyncio
async def test_orchestrator_callbacks_and_tasks(
    test_config: Config, tmp_path: Path
) -> None:
    """Verifies orchestrator helper callbacks and background task initialization."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_notifier = MagicMock()
    db_path = tmp_path / "trading.db"

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=tmp_path,
        database_path=db_path,
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    with (
        patch("app.main.trigger_settlement", AsyncMock()) as mock_settlement,
        patch("app.main.handle_retriable_error", AsyncMock()) as mock_retry,
        patch("app.main.run_recovery", AsyncMock()) as mock_recovery,
    ):
        await orchestrator.trigger_settlement_callback("G1", "A1")
        mock_settlement.assert_called_once()

        await orchestrator.handle_retriable_error_callback(101)
        mock_retry.assert_called_once()

        await orchestrator.run_recovery_callback()
        mock_recovery.assert_called_once()


@pytest.mark.asyncio
async def test_run_reconnect_callback_prevents_concurrent_runs(
    test_config: Config,
) -> None:
    """Verifies that run_reconnect_callback skips if reconnection is already in progress."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=MagicMock(),
        database_path=MagicMock(),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    orchestrator.is_reconnecting = True
    await orchestrator.run_reconnect_callback()
    assert orchestrator.is_reconnecting is True


@pytest.mark.asyncio
async def test_execute_reconnect_loop_success(test_config: Config) -> None:
    """Verifies _execute_reconnect_loop triggers recovery run on successful reconnection."""
    mock_ib = MagicMock()
    mock_ib.isConnected.side_effect = [False, True]
    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=MagicMock(),
        database_path=MagicMock(),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    with (
        patch("asyncio.sleep", AsyncMock()),
        patch.object(
            orchestrator, "_attempt_single_reconnect", AsyncMock(return_value=True)
        ),
        patch.object(
            orchestrator, "run_recovery_callback", AsyncMock()
        ) as mock_recovery,
    ):
        await orchestrator._execute_reconnect_loop()
        mock_notifier.send_system_status.assert_called_once()
        mock_recovery.assert_called_once()


@pytest.mark.asyncio
async def test_database_backup_loop_resiliency(
    test_config: Config, tmp_path: Path
) -> None:
    """Verifies database_backup_loop handles non-fatal exceptions gracefully."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=tmp_path,
        database_path=tmp_path / "trading.db",
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    with (
        patch(
            "app.main.run_db_backup",
            AsyncMock(side_effect=[Exception("Backup fail"), None]),
        ),
        patch("asyncio.sleep", AsyncMock()),
    ):
        backup_task = asyncio.create_task(orchestrator.database_backup_loop())
        await asyncio.sleep(0.05)
        backup_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_graceful_shutdown_queue_timeout(test_config: Config) -> None:
    """Verifies that graceful_shutdown logs warning on queue join timeout."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = False
    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()

    queue = asyncio.Queue()
    await queue.put("unprocessed_group")

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=Path("/root"),
        database_path=Path("/root/data/trading.db"),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=queue,
    )
    orchestrator.tasks = ()

    # Fast shutdown timeout
    await orchestrator.graceful_shutdown()


@pytest.mark.asyncio
async def test_execute_reconnect_loop_already_connected(test_config: Config) -> None:
    """Verifies _execute_reconnect_loop exits immediately if IB is already connected."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=Path("/root"),
        database_path=Path("/root/data/trading.db"),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    with patch("asyncio.sleep", AsyncMock()):
        await orchestrator._execute_reconnect_loop()


@pytest.mark.asyncio
async def test_execute_reconnect_loop_exhausting_attempts(test_config: Config) -> None:
    """Verifies reconnect loop when max attempts are reached and hourly mode activates."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = False
    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=Path("/root"),
        database_path=Path("/root/data/trading.db"),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    call_count = 0

    async def mock_single_reconnect(attempt: int) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= test_config.tws.reconnect_max_attempts + 1:
            raise asyncio.CancelledError()
        return False

    with (
        patch("asyncio.sleep", AsyncMock()),
        patch.object(
            orchestrator, "_attempt_single_reconnect", side_effect=mock_single_reconnect
        ),
    ):
        try:
            await orchestrator._execute_reconnect_loop()
        except asyncio.CancelledError:
            pass

    assert mock_notifier.send_system_status.called


@pytest.mark.asyncio
async def test_attempt_single_reconnect_success_and_failure(
    test_config: Config,
) -> None:
    """Verifies _attempt_single_reconnect return values on success and failure."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=Path("/root"),
        database_path=Path("/root/data/trading.db"),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    with patch("app.main._enable_socket_keepalive"):
        # Success
        mock_ib.connectAsync = AsyncMock(return_value=None)
        res_ok = await orchestrator._attempt_single_reconnect(attempt=1)
        assert res_ok is True

        # Exception
        mock_ib.connectAsync = AsyncMock(side_effect=Exception("Socket timeout"))
        res_fail = await orchestrator._attempt_single_reconnect(attempt=2)
        assert res_fail is False


@pytest.mark.asyncio
async def test_heartbeat_loop_not_connected_and_exception(test_config: Config) -> None:
    """Verifies heartbeat skips when disconnected and handles generic ping exceptions."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = False
    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=Path("/root"),
        database_path=Path("/root/data/trading.db"),
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    # 1. Skip when disconnected
    await orchestrator._execute_heartbeat_cycle()
    mock_ib.reqCurrentTimeAsync.assert_not_called()

    # 2. Ping generic exception
    mock_ib.isConnected.return_value = True
    mock_ib.reqCurrentTimeAsync = AsyncMock(side_effect=Exception("Generic ping error"))
    await orchestrator._send_ping_and_handle_timeout()


@pytest.mark.asyncio
async def test_database_backup_loop_cancelled_error(
    test_config: Config, tmp_path: Path
) -> None:
    """Verifies database_backup_loop re-raises asyncio.CancelledError."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=tmp_path,
        database_path=tmp_path / "trading.db",
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    with patch("app.main.run_db_backup", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.database_backup_loop()


@pytest.mark.asyncio
async def test_run_database_migrations_failure(tmp_path: Path) -> None:
    """Verifies _run_database_migrations disconnects and exits on migration failure."""
    from app.main import _run_database_migrations

    mock_ib = MagicMock()
    mock_db_conn = AsyncMock()

    with (
        patch("app.main.get_db", AsyncMock(return_value=mock_db_conn)),
        patch(
            "app.main.run_migrations",
            AsyncMock(side_effect=Exception("Migration crashed")),
        ),
        pytest.raises(SystemExit) as exit_info,
    ):
        await _run_database_migrations(tmp_path, tmp_path / "trading.db", mock_ib)

    assert exit_info.value.code == 1
    mock_ib.disconnect.assert_called_once()
    mock_db_conn.close.assert_called()


@pytest.mark.asyncio
async def test_initialize_and_start_orchestrator(
    test_config: Config, tmp_path: Path
) -> None:
    """Verifies _initialize_and_start_orchestrator creates orchestrator and registers callbacks."""
    from app.main import _initialize_and_start_orchestrator

    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    with (
        patch("app.main.TradingSystemOrchestrator.run_recovery_callback", AsyncMock()),
        patch(
            "app.main.TradingSystemOrchestrator.start_background_tasks"
        ) as mock_start_tasks,
    ):
        orchestrator = await _initialize_and_start_orchestrator(
            root_directory_path=tmp_path,
            database_path=tmp_path / "trading.db",
            config=test_config,
            notifier=mock_notifier,
            interactive_brokers=mock_ib,
        )

        assert orchestrator is not None
        mock_start_tasks.assert_called_once()


@pytest.mark.asyncio
async def test_main_function_execution_flow(
    test_config: Config, tmp_path: Path
) -> None:
    """Verifies full main() execution flow up to graceful shutdown."""
    from app.main import main

    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()
    mock_ib = MagicMock()

    mock_orchestrator = MagicMock()
    mock_orchestrator.shutdown_event = asyncio.Event()
    mock_orchestrator.shutdown_event.set()  # Immediately trigger shutdown loop exit
    mock_orchestrator.graceful_shutdown = AsyncMock()

    with (
        patch("app.main._initialize_config_and_logging", return_value=test_config),
        patch("app.main.TelegramNotifier", return_value=mock_notifier),
        patch(
            "app.main._verify_database_integrity",
            AsyncMock(return_value=tmp_path / "trading.db"),
        ),
        patch("app.main.IB", return_value=mock_ib),
        patch("app.main.connect_to_tws", AsyncMock(return_value=True)),
        patch("app.main._run_database_migrations", AsyncMock()),
        patch(
            "app.main._initialize_and_start_orchestrator",
            AsyncMock(return_value=mock_orchestrator),
        ),
        patch("app.main._setup_graceful_shutdown"),
    ):
        await main()
        mock_orchestrator.graceful_shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_main_function_exits_when_not_connected(
    test_config: Config, tmp_path: Path
) -> None:
    """Verifies that main() exits with code 1 if connect_to_tws returns False."""
    from app.main import main

    mock_notifier = MagicMock()
    mock_notifier.send_system_status = AsyncMock()

    with (
        patch("app.main._initialize_config_and_logging", return_value=test_config),
        patch("app.main.TelegramNotifier", return_value=mock_notifier),
        patch(
            "app.main._verify_database_integrity",
            AsyncMock(return_value=tmp_path / "trading.db"),
        ),
        patch("app.main.IB"),
        patch("app.main.connect_to_tws", AsyncMock(return_value=False)),
        pytest.raises(SystemExit) as exit_info,
    ):
        await main()

    assert exit_info.value.code == 1


@pytest.mark.asyncio
async def test_start_background_tasks(test_config: Config, tmp_path: Path) -> None:
    """Verifies start_background_tasks creates all 6 background tasks."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=tmp_path,
        database_path=tmp_path / "trading.db",
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    with patch(
        "asyncio.create_task",
        side_effect=lambda coroutine: (coroutine.close(), MagicMock())[1],
    ):
        orchestrator.start_background_tasks()
        assert len(orchestrator.tasks) == 6


@pytest.mark.asyncio
async def test_signal_handler_execution(test_config: Config) -> None:
    """Verifies that signal_handler registered in _setup_graceful_shutdown sets shutdown event."""
    mock_orchestrator = MagicMock()
    mock_orchestrator.shutdown_event = asyncio.Event()

    registered_handlers = []

    def mock_add_signal_handler(sig, handler):
        registered_handlers.append(handler)

    mock_loop = MagicMock()
    mock_loop.add_signal_handler = mock_add_signal_handler

    with patch("asyncio.get_running_loop", return_value=mock_loop):
        _setup_graceful_shutdown(mock_orchestrator)
        assert len(registered_handlers) >= 1
        # Trigger signal handler
        registered_handlers[0]()
        assert mock_orchestrator.shutdown_event.is_set()


@pytest.mark.asyncio
async def test_heartbeat_loop_and_backup_loop_exceptions(
    test_config: Config, tmp_path: Path
) -> None:
    """Verifies error handling in heartbeat_loop and database_backup_loop."""
    mock_ib = MagicMock()
    mock_notifier = MagicMock()

    orchestrator = TradingSystemOrchestrator(
        root_directory_path=tmp_path,
        database_path=tmp_path / "trading.db",
        config=test_config,
        notifier=mock_notifier,
        interactive_brokers=mock_ib,
        queue=asyncio.Queue(),
    )

    hb_count = 0

    async def mock_hb_cycle():
        nonlocal hb_count
        hb_count += 1
        if hb_count == 1:
            raise RuntimeError("HB error")
        raise asyncio.CancelledError()

    with (
        patch.object(
            orchestrator, "_execute_heartbeat_cycle", side_effect=mock_hb_cycle
        ),
        patch("asyncio.sleep", AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.heartbeat_loop()

    bk_count = 0

    async def mock_backup(db_path):
        nonlocal bk_count
        bk_count += 1
        if bk_count == 1:
            raise RuntimeError("Backup error")
        raise asyncio.CancelledError()

    with (
        patch("app.main.run_db_backup", side_effect=mock_backup),
        patch("asyncio.sleep", AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.database_backup_loop()
