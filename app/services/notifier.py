"""
Telegram-Notifier-Dienst für Systembenachrichtigungen und Alerts.

Stellt einen asynchronen, rate-limitierten Client zur Übermittlung von
Order-Statusberichten, Handelsabschlüssen und Fehlermeldungen per Telegram bereit.
"""

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
            logger.info("Telegram-Alert (MOCK):", message=text)
            return True

        # Warte, um Rate-Limits einzuhalten
        await self.limiter.wait()

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            request_timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=request_timeout
                ) as response:
                    if response.status == 200:
                        logger.info("Telegram Alert gesendet", message=text)
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

    async def send_system_status(self, title: str, emoji: str = "🚀") -> bool:
        """Sendet eine System-Status-Nachricht (Start/Stop)."""
        from datetime import datetime

        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        message = (
            f"{emoji} <b>IBKR: {title}</b>\n"
            f"🕒 Time: {now_str}"
        )
        return await self.send_message(message)

    async def send_order_filled(
        self,
        symbol: str,
        bracket_role: str,
        action: str,
        quantity: float,
        price: float,
        order_type: str,
        order_id: int,
        strategy_name: str,
    ) -> bool:
        """Sendet eine Erfolgsmeldung für eine gefüllte Order."""
        total_val = float(quantity) * float(price) if price else 0.0
        price_str = f"{float(price):.2f}" if price else "MKT"

        message = (
            f"🟢 <b>ORDER GEFÜLLT</b> | <code>{symbol}</code>\n"
            f"├─ <b>Typ:</b> <code>{bracket_role}</code> ({action})\n"
            f"├─ <b>Menge:</b> <code>{quantity}</code> @ <code>{price_str}</code> ({order_type})\n"
            f"├─ <b>Wert:</b> <code>$ {total_val:,.2f}</code>\n"
            f"└─ <b>System:</b> ID: <code>{order_id}</code> • <i>{strategy_name}</i>"
        )
        return await self.send_message(message)

    async def send_order_failed(
        self,
        order_id: int,
        tws_code: int,
        reason: str,
        symbol: str = "Unbekannt",
        bracket_role: str = "-",
        is_fatal: bool = True
    ) -> bool:
        """Sendet eine Fehler/Warnmeldung für eine fehlgeschlagene oder stornierte Order."""
        emoji = "🚨" if is_fatal else "🚫"
        title = "ORDER FEHLGESCHLAGEN" if is_fatal else "ORDER CANCELED"

        message = (
            f"{emoji} <b>{title}</b> | <code>ID: {order_id}</code>\n"
            f"├─ <b>Symbol/Typ:</b> <code>{symbol}</code> ({bracket_role})\n"
            f"├─ <b>TWS-Code:</b> <code>{tws_code}</code>\n"
            f"└─ <b>Grund:</b> <i>{reason}</i>"
        )
        return await self.send_message(message)

    async def send_importer_info(self, file_name: str, status: str, details: str, emoji: str = "📁", title: str = "DATEN IMPORT") -> bool:
        """Sendet eine Info-Meldung über importierte Daten oder Validierungsfehler."""
        message = (
            f"{emoji} <b>{title}</b> | <code>{file_name}</code>\n"
            f"├─ <b>Status:</b> <code>{status}</code>\n"
            f"└─ <b>Details:</b> <i>{details}</i>"
        )
        return await self.send_message(message)

    async def send_bracket_order_submitted(
        self,
        symbol: str,
        trade_group_id: str,
        strategy_name: str,
        orders: list[dict]
    ) -> bool:
        """
        Sendet eine Zusammenfassung einer Trade-Gruppe (Bracket/OCA).
        orders erwartet dicts mit keys: role, action, quantity, price, order_type
        """
        if not orders:
            return False

        if len(orders) == 1:
            title = "ORDER GESENDET"
        else:
            title = "BRACKET ORDER GESENDET"

        lines = [f"📤 <b>{title}</b> | <code>{symbol}</code>"]

        for order in orders:
            price_str = f"{float(order['price']):.2f}" if order.get('price') else "MKT"
            lines.append(
                f"├─ <b>{order['role']}:</b> <code>{order['action']} {order['quantity']}</code> @ <code>{price_str}</code> ({order['order_type']})"
            )

        if trade_group_id:
            lines.append(f"└─ <b>System:</b> Group: <code>{trade_group_id}</code> • <i>{strategy_name}</i>")
        else:
            lines.append(f"└─ <b>System:</b> <i>{strategy_name}</i>")

        message = "\n".join(lines)
        return await self.send_message(message)
