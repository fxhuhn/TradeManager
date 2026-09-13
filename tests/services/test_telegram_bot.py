"""Tests für TelegramCommandListener Service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.notifier import DEFAULT_BOT_KEYBOARD
from app.services.telegram_bot import TelegramCommandListener


@pytest.fixture
def mock_config() -> MagicMock:
    """Mock-Konfiguration für Telegram-Tests."""
    config = MagicMock()
    config.telegram.bot_token = "123456:TEST_BOT_TOKEN_ABC123"
    config.telegram.chat_id = "987654321"
    config.telegram.enable_commands = True
    config.telegram.ibkr_container_name = "ibkr"
    return config


def test_is_active(mock_config: MagicMock) -> None:
    """Verifiziert die is_active Eigenschaft unter verschiedenen Konfigurationen."""
    notifier = MagicMock()
    container_manager = MagicMock()
    reconnect_cb = AsyncMock()

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_manager,
        trigger_reconnect_callback=reconnect_cb,
    )
    assert listener.is_active is True

    # Inaktiv wenn enable_commands=False
    mock_config.telegram.enable_commands = False
    assert listener.is_active is False

    # Inaktiv bei DUMMY token
    mock_config.telegram.enable_commands = True
    mock_config.telegram.bot_token = "DUMMY_TOKEN"
    assert listener.is_active is False

    # Inaktiv bei leerem Chat-ID
    mock_config.telegram.bot_token = "VALID_TOKEN"
    mock_config.telegram.chat_id = ""
    assert listener.is_active is False


@pytest.mark.asyncio
async def test_start_polling_inactive_returns_immediately(
    mock_config: MagicMock,
) -> None:
    """Verifiziert, dass start_polling sofort beendet, wenn is_active False ist."""
    mock_config.telegram.enable_commands = False
    listener = TelegramCommandListener(
        config=mock_config,
        notifier=MagicMock(),
        container_manager=MagicMock(),
        trigger_reconnect_callback=AsyncMock(),
    )
    # Sollte sofort ohne Schleife zurückkehren
    await listener.start_polling()


@pytest.mark.asyncio
async def test_fetch_updates_success_and_offset(mock_config: MagicMock) -> None:
    """Verifiziert erfolgreiches Abrufen von Updates mit Offset-Tracking."""
    notifier = MagicMock()
    container_manager = MagicMock()
    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_manager,
        trigger_reconnect_callback=AsyncMock(),
        polling_timeout_seconds=5,
    )

    fake_updates = [
        {"update_id": 100, "message": {"text": "/help", "chat": {"id": 987654321}}}
    ]
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json.return_value = {"ok": True, "result": fake_updates}

    mock_get_context = MagicMock()
    mock_get_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_get_context.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_get_context)

    mock_client_session_context = MagicMock()
    mock_client_session_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_client_session_context.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_client_session_context):
        updates = await listener._fetch_updates()

    assert updates == fake_updates


@pytest.mark.asyncio
async def test_fetch_updates_error_handling(mock_config: MagicMock) -> None:
    """Verifiziert, dass Fehler bei getUpdates abgefangen werden."""
    listener = TelegramCommandListener(
        config=mock_config,
        notifier=MagicMock(),
        container_manager=MagicMock(),
        trigger_reconnect_callback=AsyncMock(),
    )

    mock_response = AsyncMock()
    mock_response.status = 502
    mock_response.text.return_value = "Bad Gateway"

    mock_get_context = MagicMock()
    mock_get_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_get_context.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_get_context)

    mock_client_session_context = MagicMock()
    mock_client_session_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_client_session_context.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_client_session_context):
        updates = await listener._fetch_updates()

    assert updates == []


@pytest.mark.asyncio
async def test_handle_authorized_help_command(mock_config: MagicMock) -> None:
    """Verifiziert /help Befehl für autorisierte Chat-ID."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=MagicMock(),
        trigger_reconnect_callback=AsyncMock(),
    )

    message = {"chat": {"id": 987654321}, "text": "/help"}
    await listener._handle_text_message(message)

    notifier.send_message.assert_awaited_once()
    assert "Bot-Befehle" in notifier.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_unauthorized_command_rejected(mock_config: MagicMock) -> None:
    """Verifiziert, dass unautorisierte Chat-IDs ignoriert werden."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=MagicMock(),
        trigger_reconnect_callback=AsyncMock(),
    )

    message = {"chat": {"id": 111222333}, "text": "/restart_ibkr"}
    await listener._handle_text_message(message)

    notifier.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_authorized_status_command(mock_config: MagicMock) -> None:
    """Verifiziert /status Befehl mit Custom-Status-Provider."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    status_cb = AsyncMock(return_value="Custom Status Info")

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=MagicMock(),
        trigger_reconnect_callback=AsyncMock(),
        status_provider_callback=status_cb,
    )

    message = {"chat": {"id": "987654321"}, "text": "/status"}
    await listener._handle_text_message(message)

    status_cb.assert_awaited_once()
    notifier.send_message.assert_awaited_once_with(
        "Custom Status Info", reply_markup=DEFAULT_BOT_KEYBOARD
    )


@pytest.mark.asyncio
async def test_handle_restart_flow_success(mock_config: MagicMock) -> None:
    """Verifiziert erfolgreichen Neustart-Flow via Textbefehl."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    container_manager = MagicMock()
    container_manager.restart_container = AsyncMock(
        return_value=(True, "Restarted successfully")
    )
    reconnect_cb = AsyncMock()

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_manager,
        trigger_reconnect_callback=reconnect_cb,
        restart_debounce_seconds=60.0,
    )

    message = {"chat": {"id": 987654321}, "text": "/restart_ibkr"}
    await listener._handle_text_message(message)

    # 1. Initiierungsnachricht gesendet
    # 2. container_manager aufgerufen
    container_manager.restart_container.assert_awaited_once_with(container_name="ibkr")
    # 3. Erfolgsnachricht gesendet
    assert notifier.send_message.await_count == 2
    # 4. Reconnect Callback aufgerufen
    reconnect_cb.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_restart_debounce(mock_config: MagicMock) -> None:
    """Verifiziert Debounce-Schutz gegen Mehrfach-Klicks."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    container_manager = MagicMock()
    container_manager.restart_container = AsyncMock(return_value=(True, "OK"))
    reconnect_cb = AsyncMock()

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_manager,
        trigger_reconnect_callback=reconnect_cb,
        restart_debounce_seconds=60.0,
    )

    message = {"chat": {"id": 987654321}, "text": "/restart_ibkr"}
    await listener._handle_text_message(message)
    assert container_manager.restart_container.await_count == 1

    # Zweiter Aufruf unmittelbar danach -> Debounce greift
    await listener._handle_text_message(message)
    assert container_manager.restart_container.await_count == 1
    last_sent = notifier.send_message.call_args[0][0]
    assert "Neustart bereits in Arbeit" in last_sent


@pytest.mark.asyncio
async def test_handle_callback_query_authorized(mock_config: MagicMock) -> None:
    """Verifiziert Behandlung von Inline-Button-Klicks."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    notifier.answer_callback_query = AsyncMock(return_value=True)
    container_manager = MagicMock()
    container_manager.restart_container = AsyncMock(return_value=(True, "OK"))
    reconnect_cb = AsyncMock()

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_manager,
        trigger_reconnect_callback=reconnect_cb,
    )

    callback_query = {
        "id": "query_123",
        "message": {"chat": {"id": 987654321}},
        "data": "restart_ibkr",
    }
    await listener._handle_callback_query(callback_query)

    notifier.answer_callback_query.assert_awaited_once_with(
        "query_123", text="IBKR-Neustart wird ausgeführt..."
    )
    container_manager.restart_container.assert_awaited_once_with(container_name="ibkr")
    reconnect_cb.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_callback_query_unauthorized(mock_config: MagicMock) -> None:
    """Verifiziert Ablehnung unautorisierter Inline-Button-Klicks."""
    notifier = MagicMock()
    notifier.answer_callback_query = AsyncMock(return_value=True)
    container_manager = MagicMock()

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_manager,
        trigger_reconnect_callback=AsyncMock(),
    )

    callback_query = {
        "id": "query_evil",
        "message": {"chat": {"id": 666}},
        "data": "restart_ibkr",
    }
    await listener._handle_callback_query(callback_query)

    notifier.answer_callback_query.assert_awaited_once_with(
        "query_evil", text="⛔ Nicht autorisierter Zugriff.", show_alert=True
    )
    container_manager.restart_container.assert_not_called()


@pytest.mark.asyncio
async def test_restart_flow_failure(mock_config: MagicMock) -> None:
    """Verifiziert Verhalten bei fehlgeschlagenem Container-Neustart."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    container_manager = MagicMock()
    container_manager.restart_container = AsyncMock(
        return_value=(False, "Docker daemon socket error")
    )
    reconnect_cb = AsyncMock()

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_manager,
        trigger_reconnect_callback=reconnect_cb,
    )

    message = {"chat": {"id": 987654321}, "text": "/restart_ibkr"}
    await listener._handle_text_message(message)

    reconnect_cb.assert_not_called()
    assert "Fehler beim Neustart" in notifier.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_stop_polling() -> None:
    """Verifiziert sauberes Stoppen der Polling-Schleife."""
    listener = TelegramCommandListener(
        config=MagicMock(),
        notifier=MagicMock(),
        container_manager=MagicMock(),
        trigger_reconnect_callback=AsyncMock(),
    )
    listener._is_running = True
    listener.stop()
    assert listener._is_running is False


@pytest.mark.asyncio
async def test_handle_button_text_commands(mock_config: MagicMock) -> None:
    """Verifiziert, dass Button-Texte von Reply-Keyboards korrekt erkannt werden."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    container_mgr = MagicMock()
    container_mgr.restart_container = AsyncMock(return_value=(True, "OK"))
    reconnect_cb = AsyncMock()

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_mgr,
        trigger_reconnect_callback=reconnect_cb,
    )

    # 1. Klick auf '📊 Status'
    await listener._handle_text_message(
        {"chat": {"id": 987654321}, "text": "📊 Status"}
    )
    assert notifier.send_message.await_count == 1
    assert "TradeManager Status" in notifier.send_message.call_args[0][0]

    # 2. Klick auf '🔄 IBKR Neustart'
    await listener._handle_text_message(
        {"chat": {"id": 987654321}, "text": "🔄 IBKR Neustart"}
    )
    container_mgr.restart_container.assert_awaited_once_with(container_name="ibkr")
    reconnect_cb.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_bot_commands_success_and_failure(
    mock_config: MagicMock,
) -> None:
    """Verifiziert Registrierung von Bot-Commands via setMyCommands."""
    listener = TelegramCommandListener(
        config=mock_config,
        notifier=MagicMock(),
        container_manager=MagicMock(),
        trigger_reconnect_callback=AsyncMock(),
    )

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="OK")

    mock_post_context = MagicMock()
    mock_post_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_context.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_post_context)

    mock_client_context = MagicMock()
    mock_client_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_client_context.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_client_context):
        success = await listener._register_bot_commands()
        assert success is True

        # Fehlerfall (z. B. HTTP 400)
        mock_response.status = 400
        failure = await listener._register_bot_commands()
        assert failure is False


@pytest.mark.asyncio
async def test_handle_reconnect_command(mock_config: MagicMock) -> None:
    """Verifiziert die Ausführung des /reconnect Textbefehls."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    container_manager = MagicMock()
    reconnect_cb = AsyncMock()

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=container_manager,
        trigger_reconnect_callback=reconnect_cb,
    )

    message = {"chat": {"id": 987654321}, "text": "/reconnect"}
    await listener._handle_text_message(message)

    notifier.send_message.assert_awaited_once()
    last_sent = notifier.send_message.call_args[0][0]
    assert "Wiederverbindung initiiert" in last_sent
    reconnect_cb.assert_awaited_once()
    container_manager.restart_container.assert_not_called()


@pytest.mark.asyncio
async def test_execute_reconnect_flow_handles_callback_exception(
    mock_config: MagicMock,
) -> None:
    """Verifiziert Fehlerbehandlung bei Exception im Reconnect-Callback."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    failing_cb = AsyncMock(side_effect=RuntimeError("Callback failure"))

    listener = TelegramCommandListener(
        config=mock_config,
        notifier=notifier,
        container_manager=MagicMock(),
        trigger_reconnect_callback=failing_cb,
    )

    # Sollte Exception abfangen und loggen, nicht abstürzen
    await listener._execute_reconnect_flow()
    notifier.send_message.assert_awaited_once()
    failing_cb.assert_awaited_once()
