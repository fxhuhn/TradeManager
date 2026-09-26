"""
Telegram-Notifier-Dienst für System- und Orderbenachrichtigungen.

Formatiert und sendet asynchrone Erfolgs-, Warn- und Statusnachrichten an Telegram
unter Einhaltung der API-Rate-Limits.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, TypedDict

import aiohttp
import structlog

from app.core.config import Config

if TYPE_CHECKING:
    from app.services.flex_query.service import ReconciliationReport

logger = structlog.get_logger()

type TreeRow = tuple[str, Any] | str | None


class BracketOrderDict(TypedDict, total=False):
    """Dictionary representing a single leg order submitted in a bracket or OCA group."""

    role: str
    action: str
    quantity: int | Decimal | str
    price: Decimal | float | str | None
    order_type: str


@dataclass(frozen=True)
class DailySummaryReport:
    """Immutable domain representation for an end-of-day summary notification."""

    date_str: str
    total_orders: int
    filled_orders: int
    cancelled_orders: int
    net_pnl: Decimal
    commissions: Decimal
    file_status: str
    equity: Decimal | None = None
    cushion_pct: Decimal | None = None


def _format_tree_row(row: TreeRow) -> str | None:
    """Extracts and sanitizes a single row entry for tree formatting.

    Returns the formatted string representation, or None if the row is empty or invalid.
    """
    if row is None:
        return None

    if isinstance(row, tuple):
        label, value = row
        if value is None:
            return None
        string_value = str(value).strip()
        if not string_value:
            return None
        return f"<b>{label}:</b> {string_value}"

    if isinstance(row, str):
        cleaned_text = row.strip()
        if not cleaned_text:
            return None
        return re.sub(r"^(?:├─|└─|[•\-])\s*", "", cleaned_text)

    return None


def _build_tree_header(
    title: str,
    *,
    emoji: str | None = None,
    context: str | None = None,
    system: str | None = None,
) -> str:
    """Builds the standardized header line for a tree message."""
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

    return header_line


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
    header_line = _build_tree_header(title, emoji=emoji, context=context, system=system)

    valid_rows: list[str] = []
    for row in rows:
        formatted_row = _format_tree_row(row)
        if formatted_row is not None:
            valid_rows.append(formatted_row)

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


def _is_slippage_favorable(action: str, price_difference: Decimal) -> bool:
    """Determines whether price slippage was favorable to the trader."""
    is_buy = action.upper() == "BUY"
    return (is_buy and price_difference < 0) or (not is_buy and price_difference > 0)


def _format_slippage_line(
    limit_price: Decimal | None,
    execution_price: Decimal | None,
    action: str,
    sec_type: str = "STK",
) -> str:
    """Formats a slippage indicator line for Telegram if prices diverge.

    Evaluates slippage direction relative to the trade action:
    - BUY:  fill below limit = favorable (saved money) -> 📈 Slippage: X.XX (Y.YY% Vorteil)
    - SELL: fill above limit = favorable (received more) -> 📈 Slippage: X.XX (Y.YY% Vorteil)

    Returns an empty string when slippage cannot be determined or is zero, or when sec_type is FUT
    to prevent false alerts from underlying stock condition price scale mismatches.
    """
    if sec_type == "FUT":
        return ""

    if limit_price is None or limit_price <= Decimal("0.0") or execution_price is None:
        return ""

    # Guard against cross-asset scale mismatches
    if abs(execution_price - limit_price) / limit_price > Decimal("3.0"):
        return ""

    price_difference = execution_price - limit_price
    if price_difference == 0:
        return ""

    percentage = (price_difference / limit_price) * 100
    is_favorable = _is_slippage_favorable(action, price_difference)

    direction_emoji = "📈" if is_favorable else "📉"
    label = "Vorteil" if is_favorable else "Nachteil"

    absolute_difference = abs(price_difference)
    absolute_percentage = abs(percentage)

    return (
        f"{direction_emoji} <b>Slippage:</b> "
        f"<code>{absolute_difference:.2f}</code> (<code>{absolute_percentage:.2f}% {label}</code>)"
    )


def _format_order_filled_rows(
    bracket_role: str,
    action: str,
    quantity: Decimal,
    execution_price: Decimal | None,
    order_type: str,
    order_id: int,
    strategy_name: str,
    limit_price: Decimal | None = None,
    sec_type: str = "STK",
) -> list[TreeRow]:
    """Pure helper calculating structured rows for a filled order Telegram message."""
    total_value = (
        quantity * execution_price if execution_price is not None else Decimal("0.0")
    )
    price_string = f"{execution_price:.2f}" if execution_price is not None else "MKT"

    slippage_line = _format_slippage_line(
        limit_price, execution_price, action, sec_type=sec_type
    )

    rows: list[TreeRow] = [
        ("Typ", f"<code>{bracket_role}</code> ({action})"),
    ]
    if limit_price is not None and execution_price is not None:
        price_label = (
            "Stop"
            if bracket_role == "SL" or order_type.upper() in ("STP", "TRAIL")
            else "Limit"
        )
        rows.append(
            (
                price_label,
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
    return rows


def _format_daily_summary_rows(report: DailySummaryReport) -> list[TreeRow]:
    """Pure helper constructing structured rows for a daily summary report."""
    pnl_emoji = "🟢" if report.net_pnl >= 0 else "🔴"
    rows: list[TreeRow] = [
        ("CSV-Status", f"<code>{report.file_status}</code>"),
        (
            "Orders",
            f"Gesamt: {report.total_orders} • Gefüllt: {report.filled_orders} • Storniert: {report.cancelled_orders}",
        ),
    ]
    if report.equity is not None:
        cushion_string = (
            f" • Cushion: {report.cushion_pct:.1f}%"
            if report.cushion_pct is not None
            else ""
        )
        rows.append(("Equity", f"<code>$ {report.equity:,.2f}</code>{cushion_string}"))

    rows.append(("Kommissionen", f"<code>$ {report.commissions:.2f}</code>"))
    rows.append(
        ("Realisierter Net PnL", f"{pnl_emoji} <code>$ {report.net_pnl:,.2f}</code>")
    )
    return rows


def _format_flex_reconciliation_rows(report: ReconciliationReport) -> list[TreeRow]:
    """Pure helper constructing structured rows for a Flex Query reconciliation report."""
    rows: list[TreeRow] = [
        ("Account", f"<code>{report.account_id}</code>"),
        (
            "Zeitraum",
            f"<code>{report.from_date}</code> bis <code>{report.to_date}</code>",
        ),
        (
            "Verarbeitet",
            f"Gesamt: {report.total_parsed} • Neu: {report.inserted_count} • Ignoriert: {report.skipped_duplicate_count}",
        ),
        (
            "Trade-Allokation",
            f"{report.allocated_to_trades_count} Buchungen (<code>$ {report.total_trade_adjustments_base:,.2f}</code>)",
        ),
        (
            "Account-Ebene",
            f"{report.account_level_count} Buchungen (<code>$ {report.total_account_expenses_base:,.2f}</code>)",
        ),
    ]
    return rows


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
                return await self._dispatch_post_message(
                    session, url, payload, text, reply_markup, request_timeout
                )
        except Exception as exception:
            logger.error("Error sending Telegram alert", error=str(exception))
            return False

    async def _dispatch_post_message(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: dict[str, Any],
        raw_text: str,
        reply_markup: dict[str, Any] | None,
        request_timeout: aiohttp.ClientTimeout,
    ) -> bool:
        """Dispatches HTTP POST to Telegram and handles HTML parse error fallback."""
        async with session.post(url, json=payload, timeout=request_timeout) as response:
            if response.status == 200:
                logger.info("Telegram Alert sent", length=len(raw_text))
                return True

            response_text = await response.text()
            if response.status == 400 and self._is_html_parse_error(response_text):
                logger.warning(
                    "Telegram HTML parse error, retrying with plain text fallback",
                    status=response.status,
                    response=response_text,
                )
                return await self._send_plain_text_fallback(
                    session, url, raw_text, reply_markup, request_timeout
                )

            logger.error(
                "Telegram API returned error",
                status=response.status,
                response=response_text,
            )
            return False

    @staticmethod
    def _is_html_parse_error(response_text: str) -> bool:
        """Checks if Telegram response indicates an unparseable HTML entity."""
        normalized_response = response_text.lower()
        return (
            "can't parse entities" in normalized_response
            or "unsupported start tag" in normalized_response
        )

    async def _send_plain_text_fallback(
        self,
        session: aiohttp.ClientSession,
        url: str,
        text: str,
        reply_markup: dict[str, Any] | None,
        request_timeout: aiohttp.ClientTimeout,
    ) -> bool:
        """Retries sending message as plain text when Telegram rejects HTML markup."""
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

            retry_text = await retry_response.text()
            logger.error(
                "Telegram plain text fallback failed",
                status=retry_response.status,
                response=retry_text,
            )
            return False

    async def send_interactive_reconnect_alert(
        self,
        title: str,
        container_name: str = "ibkr",
        button_text: str = "🔄 IBKR Gateway neu starten",
        callback_data: str = "restart_ibkr",
    ) -> bool:
        """Sendet einen Reconnect-Alert mit einem interaktiven Inline-Keyboard-Button."""
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

    async def send_read_only_alert(
        self,
        details: str = "",
        container_name: str = "ibkr",
        button_text: str = "🔄 IBKR Gateway neu starten",
        callback_data: str = "restart_ibkr",
    ) -> bool:
        """Sendet eine Alarmmeldung, wenn sich das IBKR Gateway im Read-Only Modus befindet."""
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        clean_details = (
            _clean_html_text(details)
            if details
            else "Kein Schreibzugriff für API-Client im Gateway konfiguriert."
        )
        message = build_tree_message(
            title="IBKR GATEWAY IM READ-ONLY MODUS",
            system="IBKR",
            emoji="🚨",
            context=container_name,
            rows=[
                ("Status", "API Schreibzugriff verweigert (Read-Only)"),
                ("Zeit", now_str),
                ("Details", f"<i>{clean_details}</i>"),
                (
                    "Aktion",
                    "Im Gateway Schreibzugriff bestätigen oder Gateway neu starten:",
                ),
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
        del error_code, details  # Intentionally omitted in compact status alert layout
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
        sec_type: str = "STK",
    ) -> bool:
        """Sendet eine Erfolgsmeldung für eine gefüllte Order inkl. Slippage-Anzeige."""
        rows = _format_order_filled_rows(
            bracket_role=bracket_role,
            action=action,
            quantity=quantity,
            execution_price=execution_price,
            order_type=order_type,
            order_id=order_id,
            strategy_name=strategy_name,
            limit_price=limit_price,
            sec_type=sec_type,
        )
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
        orders: Sequence[BracketOrderDict | dict[str, Any]],
    ) -> bool:
        """Sendet eine Zusammenfassung einer Trade-Gruppe (Bracket/OCA).

        orders erwartet dicts mit keys: role, action, quantity, price, order_type
        """
        del trade_group_id
        if not orders:
            return False

        title = "ORDER GESENDET" if len(orders) == 1 else "BRACKET ORDER GESENDET"

        rows: list[TreeRow] = []
        for order in orders:
            raw_price = order.get("price")
            price_string = (
                f"{Decimal(str(raw_price)):.2f}"
                if raw_price is not None and str(raw_price).strip()
                else "MKT"
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
        report = DailySummaryReport(
            date_str=date_str,
            total_orders=total_orders,
            filled_orders=filled_orders,
            cancelled_orders=cancelled_orders,
            net_pnl=net_pnl,
            commissions=commissions,
            file_status=file_status,
            equity=equity,
            cushion_pct=cushion_pct,
        )
        rows = _format_daily_summary_rows(report)
        message = build_tree_message(
            title="TAGESABSCHLUSS-BERICHT",
            context=date_str,
            emoji="📊",
            rows=rows,
        )
        return await self.send_message(message)

    async def send_flex_reconciliation_summary(
        self,
        report: ReconciliationReport,
    ) -> bool:
        """Sendet eine Zusammenfassung des Flex-Query-Reconciliation-Laufs an Telegram."""
        rows = _format_flex_reconciliation_rows(report)
        message = build_tree_message(
            title="IBKR FLEX RECONCILIATION",
            context=f"{report.from_date} - {report.to_date}",
            emoji="📑",
            rows=rows,
        )
        return await self.send_message(message)
