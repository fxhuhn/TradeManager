import asyncio
import time

import aiohttp
import structlog

from app.core.config import Config

logger = structlog.get_logger()


class AsyncTelegramRateLimiter:
    """
    Stellt sicher, dass wir die Telegram Rate-Limits einhalten
    (maximal 1 Nachricht alle X Sekunden, um Spike-Limits zu umgehen).
    """

    def __init__(self, delay_seconds: float = 1.5) -> None:
        self.delay_seconds = delay_seconds
        self.last_sent = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        """Wartet falls nötig, um das Rate-Limit einzuhalten."""
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_sent
            if elapsed < self.delay_seconds:
                sleep_time = self.delay_seconds - elapsed
                await asyncio.sleep(sleep_time)
            self.last_sent = time.monotonic()


class TelegramNotifier:
    """Sende asynchrone Alert-Nachrichten an Telegram."""

    def __init__(self, config: Config) -> None:
        self.token = config.telegram.bot_token
        self.chat_id = config.telegram.chat_id
        self.request_timeout_seconds = config.telegram.request_timeout_s

        self.limiter = AsyncTelegramRateLimiter(
            delay_seconds=config.telegram.rate_limit_delay_s
        )

        self.is_active = bool(self.token and self.chat_id and "DUMMY" not in self.token)
        if not self.is_active:
            logger.warning("Telegram Notifier inaktiv (DUMMY oder leere Konfiguration)")

    async def send_message(self, text: str) -> bool:
        """
        Sendet eine Nachricht asynchron via Telegram.
        Nutzt aiohttp, damit der Event-Loop nicht blockiert wird.
        """
        if not self.is_active:
            logger.info("Telegram-Alert (MOCK):", msg=text)
            return True

        # Warte, um Rate-Limits einzuhalten
        await self.limiter.wait()

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        # Unterstriche fuer den Telegram-Markdown-Parser maskieren
        safe_text = text.replace("_", "\\_")

        payload = {
            "chat_id": self.chat_id,
            "text": f"🤖 *IBKR Trading System*\n\n{safe_text}",
            "parse_mode": "Markdown",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=self.request_timeout_seconds
                ) as response:
                    if response.status == 200:
                        logger.info("Telegram Alert gesendet", msg=text)
                        return True
                    else:
                        response_text = await response.text()
                        logger.error(
                            "Telegram-API lieferte Fehler",
                            status=response.status,
                            response=response_text,
                        )
                        return False
        except Exception as exception:
            logger.error("Fehler beim Senden des Telegram-Alerts", error=str(exception))
            return False
