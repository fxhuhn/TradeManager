"""
Telegram-Notifier-Dienst für System- und Orderbenachrichtigungen.

Formatiert und sendet asynchrone Erfolgs-, Warn- und Statusnachrichten an Telegram
unter Einhaltung der API-Rate-Limits.
"""

import asyncio
import re
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final

import aiohttp
import structlog

from app.core.config import Config

logger = structlog.get_logger()

type TreeRow = tuple[str, Any] | str | None


def build_tree_message(
    title: str,
    rows: Sequence[TreeRow],
    *,
    emoji: str | None = None,
    context: str | None = None,
    system: str | None = None,
) -> str:
    """Formatiert eine standardisierte Telegram-Baumnachricht mit Glyphen (├─, └─).

    Reine Funktion (Functional Core), die Layout-Struktur von Fachdaten trennt.
    Filtert None und leere Werte automatisch heraus und setzt deterministisch
    für alle Zeilen bis zur vorletzten '├─ ' und für die letzte '└─ '.

    Args:
        title: Titel oder Thema der Meldung.
        rows: Sequenz aus (Label, Wert)-Tupeln oder vorformatierten Zeilenstrings.
        emoji: Optionales führendes Emoji für den Header.
        context: Optionaler Kontext (z. B. Symbol oder Dateiname) nach '| <code>...</code>'.
        system: Optionales System-Präfix (z. B. 'TradeManager', 'IBKR Gateway').

    Returns:
        HTML-formatierter String für Telegram.
    """
    header_parts: list[str] = []
    if emoji:
        header_parts.append(emoji)

    if system:
        title_part = f"{system}: {title}" if title else system
    else:
        title_part = title

    header_parts.append(f"<b>{title_part}</b>")
    header_line = " ".join(header_parts)

    if context:
        header_line += f" | <code>{context}</code>"

    valid_rows: list[str] = []
    for row in rows:
        if row is None:
            continue
        if isinstance(row, tuple):
            label, value = row
            if value is None:
                continue
            str_value = str(value).strip()
            if not str_value:
                continue
            valid_rows.append(f"<b>{label}:</b> {str_value}")
        elif isinstance(row, str):
            clean_str = row.strip()
            if not clean_str:
                continue
            clean_str = re.sub(r"^(?:├─|└─|[•\-])\s*", "", clean_str)
            valid_rows.append(clean_str)

    if not valid_rows:
        return header_line

    lines = [header_line]
    total_rows = len(valid_rows)
    for index, content in enumerate(valid_rows):
        prefix = "└─ " if index == total_rows - 1 else "├─ "
        lines.append(f"{prefix}{content}")

    return "\n".join(lines)


DEFAULT_BOT_KEYBOARD: Final[dict[str, Any]] = {
    "keyboard": [[{"text": "📊 Status"}, {"text": "🔄 IBKR Neustart"}]],
    "resize_keyboard": True,
    "persistent": True,
}


def _strip_html(text: str) -> str:
    """Removes HTML tags from text for logging."""
    return re.sub(r"<[^>]+>", "", text)


def _clean_html_text(text: str) -> str:
    """Removes or converts unsupported HTML tags (like <br>) from text before formatting into Telegram HTML."""
    if not text:
        return ""
    # Convert <br>, <br/>, <br /> into space and collapse extra spaces
    cleaned = re.sub(r"(?i)<br\s*/?>", " ", text)
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def _format_slippage_line(
    limit_price: Decimal | None,
    execution_price: Decimal | None,
    action: str,
) -> str:
    """Formats a slippage indicator line for Telegram if prices diverge.

    Evaluates slippage direction relative to the trade action:
    - BUY:  fill below limit = favorable (saved money) -> 📈 Slippage: X.XX (Y.YY% Vorteil)
    - SELL: fill above limit = favorable (received more) -> 📈 Slippage: X.XX (Y.YY% Vorteil)

    Returns an empty string when slippage cannot be determined or is zero.
    """
    if limit_price is None or limit_price <= Decimal("0.0") or execution_price is None:
        return ""

    price_difference = execution_price - limit_price
    if price_difference == 0:
        return ""

    percentage = (price_difference / limit_price) * 100

    is_buy = action.upper() == "BUY"
    is_favorable = (is_buy and price_difference < 0) or (
        not is_buy and price_difference > 0
    )
    direction_emoji = "📈" if is_favorable else "📉"
    label = "Vorteil" if is_favorable else "Nachteil"

    abs_diff = abs(price_difference)
    abs_pct = abs(percentage)

    return (
        f"{direction_emoji} <b>Slippage:</b> "
        f"<code>{abs_diff:.2f}</code> (<code>{abs_pct:.2f}% {label}</code>)"
    )


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
            logger.warning("Telegram Notifier inactive (DUMMY or empty configuration)")

    async def send_message(
        self, text: str, reply_markup: dict[str, Any] | None = None
    ) -> bool:
        """Sendet eine Nachricht asynchron via Telegram.

        Nutzt aiohttp, damit der Event-Loop nicht blockiert wird.

        Args:
            text: Der zu sendende Text (HTML formatiert).
            reply_markup: Optionales Telegram-Reply-Markup (z. B. Inline-Keyboard).

        Returns:
            bool: True bei Erfolg, sonst False.
        """
        if not self.is_active:
            logger.info("Telegram Alert (MOCK):", message=_strip_html(text))
            return True

        # Warte, um Rate-Limits einzuhalten
        await self.limiter.wait()

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"

        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            request_timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=request_timeout
                ) as response:
                    if response.status == 200:
                        logger.info("Telegram Alert sent", length=len(text))
                        return True
                    else:
                        response_text = await response.text()
                        if response.status == 400 and (
                            "can't parse entities" in response_text.lower()
                            or "unsupported start tag" in response_text.lower()
                        ):
                            logger.warning(
                                "Telegram HTML parse error, retrying with plain text fallback",
                                status=response.status,
                                response=response_text,
                            )
                            plain_text = _strip_html(text)
                            plain_payload: dict[str, Any] = {
                                "chat_id": self.chat_id,
                                "text": plain_text,
                                "disable_web_page_preview": True,
                            }
                            if reply_markup is not None:
                                plain_payload["reply_markup"] = reply_markup

                            async with session.post(
                                url, json=plain_payload, timeout=request_timeout
                            ) as retry_response:
                                if retry_response.status == 200:
                                    logger.info(
                                        "Telegram Alert sent (plain text fallback)",
                                        length=len(plain_text),
                                    )
                                    return True
                                else:
                                    retry_text = await retry_response.text()
                                    logger.error(
                                        "Telegram plain text fallback failed",
                                        status=retry_response.status,
                                        response=retry_text,
                                    )
                                    return False

                        logger.error(
                            "Telegram API returned error",
                            status=response.status,
                            response=response_text,
                        )
                        return False
        except Exception as exception:
            logger.error("Error sending Telegram alert", error=str(exception))
            return False

    async def send_interactive_reconnect_alert(
        self,
        title: str,
        container_name: str = "ibkr",
        button_text: str = "🔄 IBKR Gateway neu starten",
        callback_data: str = "restart_ibkr",
    ) -> bool:
        """Sendet einen Reconnect-Alert mit einem interaktiven Inline-Keyboard-Button."""
        from datetime import datetime

        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        message = build_tree_message(
            title=title,
            system="IBKR",
            emoji="🚨",
            context=container_name,
            rows=[
                ("Status", "Container nicht erreichbar"),
                ("Zeit", now_str),
                ("Aktion", "Smartphone für 2FA bereitmachen und Button drücken:"),
            ],
        )
        reply_markup = {
            "inline_keyboard": [[{"text": button_text, "callback_data": callback_data}]]
        }
        return await self.send_message(message, reply_markup=reply_markup)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str,
        show_alert: bool = False,
    ) -> bool:
        """Beantwortet eine Telegram-Callback-Query (Klick auf Inline-Button)."""
        if not self.is_active:
            return True

        url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        try:
            request_timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=request_timeout
                ) as response:
                    if response.status == 200:
                        return True
                    return False
        except Exception as exception:
            logger.warning(
                "Failed to answer Telegram callback query", error=str(exception)
            )
            return False

    async def send_system_status(
        self,
        title: str,
        emoji: str = "🚀",
        reply_markup: dict[str, Any] | None = None,
        system: str = "TradeManager",
        details: str | None = None,
    ) -> bool:
        """Sendet eine System-Status-Nachricht (Start/Stop/Lifecycle)."""
        from datetime import datetime

        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        rows: list[TreeRow] = []
        if details:
            rows.append(("Details", f"<i>{_clean_html_text(details)}</i>"))
        rows.append(("Zeit", now_str))

        message = build_tree_message(
            title=title,
            system=system,
            emoji=emoji,
            rows=rows,
        )
        markup = reply_markup if reply_markup is not None else DEFAULT_BOT_KEYBOARD
        return await self.send_message(message, reply_markup=markup)

    async def send_broker_connection_status(
        self,
        is_connected: bool,
        error_code: int | None = None,
        details: str | None = None,
    ) -> bool:
        """Sendet Statusmeldung über Verbindungsverlust oder -wiederherstellung zum Broker-Backend im kompakten Format."""
        _ = (error_code, details)
        title = "WIEDERVERBUNDEN" if is_connected else "VERBINDUNGSABBRUCH"
        emoji = "✅" if is_connected else "🚨"
        return await self.send_system_status(
            title=title,
            emoji=emoji,
            system="IBKR Gateway",
        )

    async def send_order_filled(
        self,
        symbol: str,
        bracket_role: str,
        action: str,
        quantity: Decimal,
        execution_price: Decimal | None,
        order_type: str,
        order_id: int,
        strategy_name: str,
        limit_price: Decimal | None = None,
    ) -> bool:
        """Sendet eine Erfolgsmeldung für eine gefüllte Order inkl. Slippage-Anzeige."""
        total_value = (
            quantity * execution_price
            if execution_price is not None
            else Decimal("0.0")
        )
        price_string = (
            f"{execution_price:.2f}" if execution_price is not None else "MKT"
        )

        slippage_line = _format_slippage_line(limit_price, execution_price, action)

        rows: list[TreeRow] = [
            ("Typ", f"<code>{bracket_role}</code> ({action})"),
        ]
        if limit_price is not None and execution_price is not None:
            rows.append(
                (
                    "Limit",
                    f"<code>{limit_price:.2f}</code> → <b>Fill:</b> <code>{price_string}</code> ({order_type})",
                )
            )
        else:
            rows.append(
                (
                    "Menge",
                    f"<code>{quantity}</code> @ <code>{price_string}</code> ({order_type})",
                )
            )

        rows.append(("Wert", f"<code>$ {total_value:,.2f}</code>"))

        if slippage_line:
            rows.append(slippage_line)

        rows.append(("System", f"ID: <code>{order_id}</code> • <i>{strategy_name}</i>"))

        message = build_tree_message(
            title="ORDER GEFÜLLT",
            context=symbol,
            emoji="🟢",
            rows=rows,
        )
        return await self.send_message(message)

    async def send_order_failed(
        self,
        order_id: int,
        tws_code: int,
        reason: str,
        symbol: str = "Unbekannt",
        bracket_role: str = "-",
        is_fatal: bool = True,
    ) -> bool:
        """Sendet eine Fehler/Warnmeldung für eine fehlgeschlagene oder stornierte Order."""
        emoji = "🚨" if is_fatal else "🚫"
        title = "ORDER FEHLGESCHLAGEN" if is_fatal else "ORDER CANCELED"
        clean_reason = _clean_html_text(reason)

        message = build_tree_message(
            title=title,
            context=f"ID: {order_id}",
            emoji=emoji,
            rows=[
                ("Symbol/Typ", f"<code>{symbol}</code> ({bracket_role})"),
                ("TWS-Code", f"<code>{tws_code}</code>"),
                ("Grund", f"<i>{clean_reason}</i>"),
            ],
        )
        return await self.send_message(message)

    async def send_loc_execution_anomaly(
        self,
        order_id: int,
        symbol: str,
        action: str,
        limit_price: Decimal,
        close_price: Decimal,
        quantity: Decimal,
    ) -> bool:
        """Sendet eine Fehlermeldung für eine LOC-Order, die trotz erreichtem Limitpreis nicht ausgeführt wurde."""
        action_emoji = "🟢 BUY" if action.upper() == "BUY" else "🔴 SELL"
        message = build_tree_message(
            title="LOC ANOMALIE: NICHT AUSGEFÜHRT",
            context=symbol,
            emoji="⚠️",
            rows=[
                ("Order-ID", f"<code>{order_id}</code>"),
                ("Aktion", f"<code>{action_emoji}</code>"),
                ("Menge", f"<code>{quantity}</code>"),
                ("Limit-Preis", f"<code>$ {limit_price:.2f}</code>"),
                ("Schlusskurs", f"<code>$ {close_price:.2f}</code>"),
                (
                    "Status",
                    "Limitpreis wurde erreicht, aber Order wurde storniert/verfallen!",
                ),
            ],
        )
        return await self.send_message(message)

    async def send_importer_info(
        self,
        file_name: str,
        status: str,
        details: str,
        emoji: str = "📁",
        title: str = "DATEN IMPORT",
    ) -> bool:
        """Sendet eine Info-Meldung über importierte Daten oder Validierungsfehler."""
        clean_details = _clean_html_text(details)
        message = build_tree_message(
            title=title,
            context=file_name,
            emoji=emoji,
            rows=[
                ("Status", f"<code>{status}</code>"),
                ("Details", f"<i>{clean_details}</i>"),
            ],
        )
        return await self.send_message(message)

    async def send_bracket_order_submitted(
        self,
        symbol: str,
        trade_group_id: str,
        strategy_name: str,
        orders: list[dict[str, Any]],
    ) -> bool:
        """
        Sendet eine Zusammenfassung einer Trade-Gruppe (Bracket/OCA).
        orders erwartet dicts mit keys: role, action, quantity, price, order_type
        """
        if not orders:
            return False

        title = "ORDER GESENDET" if len(orders) == 1 else "BRACKET ORDER GESENDET"

        rows: list[TreeRow] = []
        for order in orders:
            price_string = (
                f"{Decimal(str(order['price'])):.2f}" if order.get("price") else "MKT"
            )
            rows.append(
                (
                    str(order["role"]),
                    f"<code>{order['action']} {order['quantity']}</code> @ <code>{price_string}</code> ({order['order_type']})",
                )
            )

        rows.append(("System", f"<i>{strategy_name}</i>"))

        message = build_tree_message(
            title=title,
            context=symbol,
            emoji="📤",
            rows=rows,
        )
        return await self.send_message(message)

    async def send_margin_limit_exceeded(
        self,
        symbol: str,
        account_id: str,
        init_margin_after: Decimal,
        limit_value: Decimal,
        cushion_percentage: Decimal,
    ) -> bool:
        """Sendet eine Meldung bei Überschreitung des Margin-Limits."""
        message = build_tree_message(
            title="MARGIN-LIMIT ÜBERSCHRITTEN",
            context=symbol,
            emoji="🚨",
            rows=[
                ("Konto", f"<code>{account_id}</code>"),
                ("Erforderliche Margin", f"<code>$ {init_margin_after:,.2f}</code>"),
                ("Limit", f"<code>$ {limit_value:,.2f}</code>"),
                ("Konto-Cushion", f"<code>{cushion_percentage:.1f}%</code>"),
                ("Status", "Order blockiert (nicht an TWS gesendet)."),
            ],
        )
        return await self.send_message(message)

    async def send_margin_utilization_warning(
        self,
        symbol: str,
        account_id: str,
        purchase_value: Decimal,
        total_cash: Decimal,
        margin_needed: Decimal,
    ) -> bool:
        """Sendet eine Meldung, wenn für einen Kauf Margin (Fremdkapital) genutzt wird."""
        message = build_tree_message(
            title="MARGIN-NUTZUNG ERFORDERLICH",
            context=symbol,
            emoji="ℹ️",
            rows=[
                ("Konto", f"<code>{account_id}</code>"),
                ("Kaufwert", f"<code>$ {purchase_value:,.2f}</code>"),
                ("Verfügbares Cash", f"<code>$ {total_cash:,.2f}</code>"),
                (
                    "Info",
                    f"Zusätzliche Margin von <code>$ {margin_needed:,.2f}</code> wird beansprucht.",
                ),
            ],
        )
        return await self.send_message(message)

    async def send_high_margin_usage_warning(
        self,
        symbol: str,
        account_id: str,
        usage_percentage: Decimal,
        init_margin_after: Decimal,
        net_liquidation: Decimal,
    ) -> bool:
        """Sendet eine Warnung bei einer Margin-Auslastung über 50%."""
        message = build_tree_message(
            title="HOHE MARGIN-AUSLASTUNG (>50%)",
            context=symbol,
            emoji="⚠️",
            rows=[
                ("Konto", f"<code>{account_id}</code>"),
                ("Margin-Auslastung", f"<code>{usage_percentage:.1f}%</code>"),
                ("Initial Margin (Neu)", f"<code>$ {init_margin_after:,.2f}</code>"),
                ("Netto-Liquidationswert", f"<code>$ {net_liquidation:,.2f}</code>"),
            ],
        )
        return await self.send_message(message)

    async def send_unassigned_position_recovered(
        self,
        symbol: str,
        quantity: Decimal,
        avg_cost: Decimal,
        account_id: str,
    ) -> bool:
        """Sendet eine Info-Meldung über eine automatisch in DB nacherfasste Unassigned-Position."""
        message = build_tree_message(
            title="UNASSIGNED POSITION RECOVERED",
            context=symbol,
            emoji="ℹ️",
            rows=[
                ("Konto", f"<code>{account_id}</code>"),
                ("Menge", f"<code>{quantity}</code>"),
                ("Durchschnittspreis", f"<code>$ {avg_cost:.2f}</code>"),
                ("Info", "Position ohne Strategie in der DB synchronisiert."),
            ],
        )
        return await self.send_message(message)

    async def send_archived_error_alert(
        self, file_name: str, details: str = ""
    ) -> bool:
        """Sendet einen Administrator-Alarm, wenn eine .err-Archivdatei erkannt wurde."""
        clean_details = _clean_html_text(details)
        message = build_tree_message(
            title="ARCHIVIERTE FEHLERDATEI ENTDECKT",
            emoji="🚨",
            rows=[
                ("Datei", f"<code>{file_name}</code>"),
                (
                    "Details",
                    f"<i>{clean_details or 'Datei wurde mit .err archiviert. Manuelle Prüfung erforderlich.'}</i>",
                ),
            ],
        )
        return await self.send_message(message)

    async def send_daily_summary(
        self,
        date_str: str,
        total_orders: int,
        filled_orders: int,
        cancelled_orders: int,
        net_pnl: Decimal,
        commissions: Decimal,
        file_status: str,
        equity: Decimal | None = None,
        cushion_pct: Decimal | None = None,
    ) -> bool:
        """Sendet einen strukturierten Tagesabschlussbericht (EOD-Summary)."""
        pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
        rows: list[TreeRow] = [
            ("CSV-Status", f"<code>{file_status}</code>"),
            (
                "Orders",
                f"Gesamt: {total_orders} • Gefüllt: {filled_orders} • Storniert: {cancelled_orders}",
            ),
        ]
        if equity is not None:
            cushion_str = (
                f" • Cushion: {cushion_pct:.1f}%" if cushion_pct is not None else ""
            )
            rows.append(("Equity", f"<code>$ {equity:,.2f}</code>{cushion_str}"))

        rows.append(("Kommissionen", f"<code>$ {commissions:.2f}</code>"))
        rows.append(
            ("Realisierter Net PnL", f"{pnl_emoji} <code>$ {net_pnl:,.2f}</code>")
        )

        message = build_tree_message(
            title="TAGESABSCHLUSS-BERICHT",
            context=date_str,
            emoji="📊",
            rows=rows,
        )
        return await self.send_message(message)
