"""Telegram Bot Command Listener Service für TradeManager.

Ermöglicht die sichere Fernsteuerung und manuelle Aktionen (z. B. Container-Neustart
bei Verbindungsverlust) direkt aus dem Telegram-Chat mittels Long-Polling.
"""

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any, Final

import aiohttp
import structlog

from app.core.config import Config
from app.services.container_manager import DockerContainerManager
from app.services.notifier import (
    DEFAULT_BOT_KEYBOARD,
    TelegramNotifier,
    build_tree_message,
)

logger = structlog.get_logger()

DEFAULT_POLLING_TIMEOUT_SECONDS: Final[int] = 20
DEFAULT_RESTART_DEBOUNCE_SECONDS: Final[float] = 60.0


class TelegramCommandListener:
    """Verarbeitet eingehende Telegram-Befehle und Button-Klicks via Long-Polling."""

    def __init__(
        self,
        config: Config,
        notifier: TelegramNotifier,
        container_manager: DockerContainerManager,
        trigger_reconnect_callback: Callable[[], Coroutine[Any, Any, None]],
        status_provider_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
        polling_timeout_seconds: int = DEFAULT_POLLING_TIMEOUT_SECONDS,
        restart_debounce_seconds: float = DEFAULT_RESTART_DEBOUNCE_SECONDS,
    ) -> None:
        """Initialisiert den TelegramCommandListener.

        Args:
            config: Zentrale Konfigurationsinstanz.
            notifier: Instanz des TelegramNotifiers für Rückmeldungen.
            container_manager: Instanz für Docker-Container-Aktionen.
            trigger_reconnect_callback: Asynchrone Callback-Funktion zur Reconnect-Auslösung.
            status_provider_callback: Optionale Callback-Funktion für Statusberichte.
            polling_timeout_seconds: Long-Polling-Timeout für getUpdates.
            restart_debounce_seconds: Mindestabstand zwischen zwei Container-Neustarts in Sekunden.
        """
        self._config = config
        self._notifier = notifier
        self._container_manager = container_manager
        self._trigger_reconnect_callback = trigger_reconnect_callback
        self._status_provider_callback = status_provider_callback
        self._polling_timeout_seconds = polling_timeout_seconds
        self._restart_debounce_seconds = restart_debounce_seconds
        self._last_restart_timestamp: float = 0.0
        self._last_update_id: int = 0
        self._is_running: bool = False

    @property
    def is_active(self) -> bool:
        """Prüft, ob der Listener aktiv betrieben werden kann."""
        return bool(
            self._config.telegram.enable_commands
            and self._config.telegram.bot_token
            and self._config.telegram.chat_id
            and "DUMMY" not in self._config.telegram.bot_token
        )

    async def start_polling(self) -> None:
        """Startet die asynchrone Long-Polling-Schleife."""
        if not self.is_active:
            logger.info("Telegram command listener inactive (disabled or dummy token)")
            return

        self._is_running = True
        logger.info(
            "Starting Telegram command listener polling loop",
            authorized_chat_id=self._config.telegram.chat_id,
        )

        await self._register_bot_commands()

        error_backoff_seconds = 2.0
        max_backoff_seconds = 30.0

        while self._is_running:
            try:
                updates = await self._fetch_updates()
                error_backoff_seconds = 2.0  # Reset bei Erfolg
                await self._dispatch_updates_batch(updates)
            except asyncio.CancelledError:
                logger.info("Telegram command listener polling loop cancelled.")
                self._is_running = False
                break
            except Exception as exception:
                logger.warning(
                    "Error in Telegram command polling loop",
                    error=str(exception),
                    backoff_seconds=error_backoff_seconds,
                )
                await asyncio.sleep(error_backoff_seconds)
                error_backoff_seconds = min(
                    error_backoff_seconds * 2.0, max_backoff_seconds
                )

    async def _dispatch_updates_batch(self, updates: list[dict[str, Any]]) -> None:
        """Leitet einen Batch empfangener Updates an die Einzelverarbeitung weiter."""
        for update in updates:
            await self._process_single_update(update)

    def stop(self) -> None:
        """Beendet die Polling-Schleife."""
        self._is_running = False

    async def _register_bot_commands(self) -> bool:
        """Registriert Bot-Befehle bei der Telegram Bot API (setMyCommands)."""
        url = f"https://api.telegram.org/bot{self._config.telegram.bot_token}/setMyCommands"
        payload = {
            "commands": [
                {
                    "command": "status",
                    "description": "📊 System- und Kontostatus anzeigen",
                },
                {
                    "command": "restart_ibkr",
                    "description": "🔄 IBKR Gateway Container neu starten",
                },
                {
                    "command": "help",
                    "description": "ℹ️ Hilfe und Befehle anzeigen",
                },
            ]
        }
        try:
            request_timeout = aiohttp.ClientTimeout(total=10.0)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=request_timeout
                ) as response:
                    if response.status == 200:
                        logger.info(
                            "Telegram bot commands successfully registered via setMyCommands"
                        )
                        return True
                    response_text = await response.text()
                    logger.warning(
                        "Failed to register Telegram bot commands",
                        status=response.status,
                        details=response_text,
                    )
                    return False
        except Exception as exception:
            logger.warning(
                "Error registering Telegram bot commands", error=str(exception)
            )
            return False

    async def _fetch_updates(self) -> list[dict[str, Any]]:
        """Ruft neue Updates von der Telegram Bot API ab."""
        url = (
            f"https://api.telegram.org/bot{self._config.telegram.bot_token}/getUpdates"
        )
        parameters: dict[str, Any] = {
            "timeout": self._polling_timeout_seconds,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._last_update_id > 0:
            parameters["offset"] = self._last_update_id + 1

        request_timeout = aiohttp.ClientTimeout(
            total=float(self._polling_timeout_seconds + 10)
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=parameters, timeout=request_timeout
            ) as response:
                if response.status != 200:
                    response_text = await response.text()
                    logger.warning(
                        "Telegram getUpdates returned non-200 status",
                        status=response.status,
                        details=response_text,
                    )
                    return []

                data: dict[str, Any] = await response.json()
                if not data.get("ok"):
                    logger.warning("Telegram getUpdates response not ok", response=data)
                    return []

                raw_results = data.get("result", [])
                if isinstance(raw_results, list):
                    return [item for item in raw_results if isinstance(item, dict)]
                return []

    async def _process_single_update(self, update: dict[str, Any]) -> None:
        """Verarbeitet ein einzelnes Telegram-Update (Nachricht oder Callback-Query)."""
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            self._last_update_id = max(self._last_update_id, update_id)

        # 1. Callback-Query (z. B. Klick auf Inline-Keyboard)
        if "callback_query" in update:
            await self._handle_callback_query(update["callback_query"])
            return

        # 2. Textnachricht
        if "message" in update:
            await self._handle_text_message(update["message"])

    def _is_chat_authorized(self, chat_id: Any) -> bool:
        """Prüft strikt, ob die übermittelte Chat-ID autorisiert ist."""
        configured_chat_id = str(self._config.telegram.chat_id).strip()
        incoming_chat_id = str(chat_id).strip()
        return bool(configured_chat_id and configured_chat_id == incoming_chat_id)

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        """Verarbeitet einen Inline-Button-Klick."""
        query_id = str(callback_query.get("id", ""))
        from_user = callback_query.get("from", {})
        message = callback_query.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id") or from_user.get("id")
        callback_data = str(callback_query.get("data", ""))

        if not self._is_chat_authorized(chat_id):
            logger.warning(
                "Unauthorized Telegram callback query rejected",
                chat_id=chat_id,
                callback_data=callback_data,
            )
            await self._notifier.answer_callback_query(
                query_id, text="⛔ Nicht autorisierter Zugriff.", show_alert=True
            )
            return

        logger.info(
            "Authorized Telegram callback query received",
            chat_id=chat_id,
            callback_data=callback_data,
        )

        if callback_data == "restart_ibkr":
            await self._notifier.answer_callback_query(
                query_id, text="IBKR-Neustart wird ausgeführt..."
            )
            await self._execute_ibkr_restart_flow()
        else:
            await self._notifier.answer_callback_query(
                query_id, text=f"Unbekannte Aktion: {callback_data}"
            )

    async def _handle_text_message(self, message: dict[str, Any]) -> None:
        """Verarbeitet eine eingehende Textnachricht."""
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        raw_text = message.get("text")

        if not isinstance(raw_text, str):
            return

        clean_text = raw_text.strip().lower()

        if not self._is_chat_authorized(chat_id):
            logger.warning(
                "Unauthorized Telegram command rejected",
                chat_id=chat_id,
                command=clean_text,
            )
            return

        logger.info(
            "Authorized Telegram command received",
            chat_id=chat_id,
            command=clean_text,
        )

        if clean_text in (
            "/restart_ibkr",
            "/restart",
            "restart",
            "🔄 ibkr neustart",
            "ibkr neustart",
            "🔄 ibkr neu starten",
        ):
            await self._execute_ibkr_restart_flow()
        elif clean_text in ("/status", "status", "📊 status"):
            await self._send_status_reply()
        elif clean_text in ("/help", "/start", "help", "hilfe", "start"):
            await self._send_help_reply()

    async def _execute_ibkr_restart_flow(self) -> None:
        """Führt den geschützten Container-Neustart und Reconnect-Flow aus."""
        now = time.monotonic()
        time_since_last_restart = now - self._last_restart_timestamp

        if (
            self._last_restart_timestamp > 0
            and time_since_last_restart < self._restart_debounce_seconds
        ):
            remaining_seconds = int(
                self._restart_debounce_seconds - time_since_last_restart
            )
            debounce_message = build_tree_message(
                title="Neustart bereits in Arbeit",
                emoji="⚠️",
                rows=[
                    ("Wartezeit", f"Noch {remaining_seconds}s"),
                    (
                        "Hinweis",
                        "Smartphone für den IBKR 2FA-Push bereithalten.",
                    ),
                ],
            )
            await self._notifier.send_message(
                debounce_message, reply_markup=DEFAULT_BOT_KEYBOARD
            )
            return

        self._last_restart_timestamp = now
        container_name = self._config.telegram.ibkr_container_name

        initiation_message = build_tree_message(
            title="IBKR-Neustart eingeleitet",
            emoji="⏳",
            context=container_name,
            rows=[
                ("Status", "Container wird neu gestartet..."),
                (
                    "Aktion",
                    "Bitte halte jetzt Dein Smartphone für den IBKR 2FA-Push bereit!",
                ),
            ],
        )
        await self._notifier.send_message(
            initiation_message, reply_markup=DEFAULT_BOT_KEYBOARD
        )

        success, detail_message = await self._container_manager.restart_container(
            container_name=container_name
        )

        if not success:
            failure_message = build_tree_message(
                title="Fehler beim Neustart",
                emoji="❌",
                context=container_name,
                rows=[
                    ("Details", detail_message),
                ],
            )
            await self._notifier.send_message(
                failure_message, reply_markup=DEFAULT_BOT_KEYBOARD
            )
            return

        success_message = build_tree_message(
            title="Container neu gestartet",
            emoji="🔄",
            context=container_name,
            rows=[
                ("Status", "IBC führt den Login durch"),
                ("Aktion", "2FA-Push auf Smartphone bestätigen!"),
                (
                    "TradeManager",
                    "Löst nun unmittelbar die Wiederverbindung aus.",
                ),
            ],
        )
        await self._notifier.send_message(
            success_message, reply_markup=DEFAULT_BOT_KEYBOARD
        )

        # Trigger sofortige Wiederverbindung
        try:
            await self._trigger_reconnect_callback()
        except Exception as exception:
            logger.error(
                "Error triggering reconnect callback after container restart",
                error=str(exception),
            )

    async def _send_status_reply(self) -> None:
        """Sendet einen aktuellen Systemstatusbericht."""
        if self._status_provider_callback is not None:
            try:
                status_text = await self._status_provider_callback()
                await self._notifier.send_message(
                    status_text, reply_markup=DEFAULT_BOT_KEYBOARD
                )
                return
            except Exception as exception:
                logger.error(
                    "Error executing status provider callback",
                    error=str(exception),
                )

        # Fallback Standard-Status
        is_socket_ok = self._container_manager.is_available()
        status_message = build_tree_message(
            title="TradeManager Status",
            emoji="📊",
            rows=[
                (
                    "Docker Socket",
                    "✅ Verfügbar" if is_socket_ok else "❌ Nicht gemountet",
                ),
                (
                    "Ziel-Container",
                    f"<code>{self._config.telegram.ibkr_container_name}</code>",
                ),
                ("Befehle aktiv", "✅"),
            ],
        )
        await self._notifier.send_message(
            status_message, reply_markup=DEFAULT_BOT_KEYBOARD
        )

    async def _send_help_reply(self) -> None:
        """Sendet Hilfetexte zu verfügbaren Befehlen inklusive Keyboard-Buttons."""
        help_message = build_tree_message(
            title="Bot-Befehle",
            system="TradeManager",
            emoji="🤖",
            rows=[
                (
                    "/status",
                    "Aktuellen Systemstatus abfragen (oder Button 📊 Status)",
                ),
                (
                    "/restart_ibkr",
                    "IBKR-Container neu starten & 2FA/Reconnect triggern (oder Button 🔄 IBKR Neustart)",
                ),
                ("/help", "Diese Hilfemeldung anzeigen"),
            ],
        )
        await self._notifier.send_message(
            help_message, reply_markup=DEFAULT_BOT_KEYBOARD
        )
