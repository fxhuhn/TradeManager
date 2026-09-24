"""
Callback-Manager für TWS-API-Events.

Registriert Event-Handler für Order-Statusaktualisierungen, Ausführungsberichte,
Kommissionen, Fehlermeldungen und Verbindungsabbrüche der Trader Workstation (TWS).
Verarbeitet den asynchronen Timing-Ablauf von Teilausführungen (execDetailsEvent) und
Fills (orderStatusEvent).

Siehe Datenfluss- und Architekturzusammenhang in app.core.models.
"""

from __future__ import annotations

import asyncio
import datetime as datetime_module
import re
from collections.abc import Awaitable, Callable, Coroutine
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

import aiosqlite
import structlog
from ib_async import IB, BarData, CommissionReport, Fill, Trade

from app.core.config import Config
from app.core.db import transaction
from app.core.models import parse_positive_decimal
from app.services.notifier import TelegramNotifier, build_tree_message
from app.trading.error_codes import (
    ErrorClass,
    classify_error_code,
    is_pre_market_hold_notice,
    is_read_only_error,
    is_trade_pre_market_held,
)
from app.trading.order_builder import (
    is_past_loc_gtd_cutoff,
    make_stock_contract,
    symbols_match,
)

logger = structlog.get_logger()


class UnassignedExecutionDetails(TypedDict):
    """Container für extrahierte Attribute einer unzugeordneten Ausführung."""

    symbol: str
    sec_type: str
    exchange: str
    currency: str
    side: str
    qty: Decimal | None
    price: Decimal | None
    account_id: str
    order_id: int
    perm_id: int | None
    exec_id: str
    executed_at: object
    order_ref: str


def _resolve_contract_attributes(
    trade: object, fill: object
) -> tuple[str, str, str, str]:
    """Ermittelt Symbol, Wertpapiertyp, Börse und Währung aus Fill oder Trade."""
    contract = getattr(fill, "contract", None)
    if contract is None and trade is not None:
        contract = getattr(trade, "contract", None)

    if not contract:
        return "", "", "", ""

    symbol = getattr(contract, "symbol", "") or ""
    sec_type = getattr(contract, "secType", "") or ""
    primary_exchange = getattr(contract, "primaryExchange", "")
    exchange = primary_exchange or getattr(contract, "exchange", "") or ""
    currency = getattr(contract, "currency", "") or ""
    return symbol, sec_type, exchange, currency


def _resolve_execution_attributes(
    execution: object,
) -> tuple[
    Decimal | None,
    Decimal | None,
    int,
    int | None,
    str,
    object,
    str,
    str,
]:
    """Ermittelt Menge, Preis, Order-IDs, Ausführungszeit und Seite aus dem Execution-Objekt."""
    if not execution:
        return None, None, 0, None, "", None, "", ""

    qty_raw = getattr(execution, "shares", None)
    qty = Decimal(str(qty_raw)) if qty_raw is not None else None

    price_raw = getattr(execution, "price", None)
    price = Decimal(str(price_raw)) if price_raw is not None else None

    order_id = getattr(execution, "orderId", 0) or 0
    perm_id = getattr(execution, "permId", None)
    exec_id = getattr(execution, "execId", "") or ""
    executed_at = getattr(execution, "time", None)
    side = getattr(execution, "side", "") or ""
    account_id = getattr(execution, "acctNumber", "") or ""

    return qty, price, order_id, perm_id, exec_id, executed_at, side, account_id


def extract_unassigned_execution_details(
    trade: object, fill: object
) -> UnassignedExecutionDetails:
    """Extrahiert alle verfügbaren Vertrags- und Ausführungsdetails aus einem TWS Trade- & Fill-Objekt.

    Wird verwendet, um bei unzugeordneten/unbekannten Orders alle Attribute (Symbol, Stückzahl,
    Preis, Börse, Konto etc.) vollständig zu erfassen.
    """
    symbol, sec_type, exchange, currency = _resolve_contract_attributes(trade, fill)
    execution = getattr(fill, "execution", None) if fill else None
    order = getattr(trade, "order", None) if trade else None

    (
        qty,
        price,
        order_id,
        perm_id,
        exec_id,
        executed_at,
        execution_side,
        execution_account_id,
    ) = _resolve_execution_attributes(execution)

    side = execution_side or (getattr(order, "action", "") if order else "")
    account_id = execution_account_id or (
        getattr(order, "account", "") if order else ""
    )
    order_ref = getattr(order, "orderRef", "") if order else ""

    return UnassignedExecutionDetails(
        symbol=symbol,
        sec_type=sec_type,
        exchange=exchange,
        currency=currency,
        side=side,
        qty=qty,
        price=price,
        account_id=account_id,
        order_id=order_id,
        perm_id=perm_id,
        exec_id=exec_id,
        executed_at=executed_at,
        order_ref=order_ref,
    )


def handle_unassigned_execution(
    trade: object, fill: object
) -> UnassignedExecutionDetails:
    """Protokolliert eine Ausführung, die keiner bekannten Order in der lokalen DB zugewiesen werden kann.

    Schreibt eine ausführliche Warnung mit allen ausgelesenen Vertragsdaten in das Log.
    """
    details = extract_unassigned_execution_details(trade, fill)
    logger.warning(
        "Unassigned execution received (order not found in local DB)",
        symbol=details["symbol"],
        side=details["side"],
        qty=details["qty"],
        price=details["price"],
        account_id=details["account_id"],
        order_id=details["order_id"],
        perm_id=details["perm_id"],
        exec_id=details["exec_id"],
        sec_type=details["sec_type"],
        exchange=details["exchange"],
        currency=details["currency"],
        executed_at=str(details["executed_at"]) if details["executed_at"] else None,
        order_ref=details["order_ref"],
    )
    return details


def is_loc_anomaly_check_warranted(
    order_type: str | None,
    target_price: Decimal | None,
    bracket_role: str | None,
    is_entry_filled: bool,
    has_filled_sibling: bool,
) -> bool:
    """Prüft seiteneffektfrei, ob für eine stornierte Order eine LOC-Schlusskursprüfung gerechtfertigt ist.

    Verhindert False Positives (Fehlalarme) bei mehrbeinigen Order-Brackets (OCA-Gruppen):
    - Wenn die Order nicht vom Typ LOC ist oder keinen Zielpreis hat -> False.
    - Wenn ein Geschwister-Exit-Leg bereits ausgeführt wurde (OCA-Stornierung) -> False.
    - Wenn die Order ein Exit-Leg ist ('SL', 'TP', 'EXIT') und das ENTRY-Leg der Gruppe
      nicht ausgeführt wurde (keine Position vorhanden) -> False.
    - Andernfalls -> True.

    Args:
        order_type: Der Ordertyp (z. B. 'LOC', 'LMT').
        target_price: Der definierte Limit-/Zielpreis der Order.
        bracket_role: Die Rolle im Bracket (z. B. 'ENTRY', 'EXIT', 'TP', 'SL').
        is_entry_filled: Gibt an, ob das ENTRY-Leg der Gruppe gefüllt wurde.
        has_filled_sibling: Gibt an, ob ein anderes Exit-Leg derselben Gruppe gefüllt wurde.

    Returns:
        True, falls eine Anomalie-Prüfung für die LOC-Order durchgeführt werden soll, sonst False.
    """
    if not order_type or order_type.upper() != "LOC":
        return False
    if target_price is None:
        return False
    if has_filled_sibling:
        return False
    role_upper = bracket_role.upper() if bracket_role else ""
    if role_upper in ("SL", "TP", "EXIT") and not is_entry_filled:
        return False
    return True


class TwsCallbacksManager:
    """
    Registriert und verwaltet alle asynchronen TWS-Callbacks (Events)
    für die Abwicklung von Order-Status-Updates, Fills, Provisionen und Fehlern.
    """

    def __init__(
        self,
        db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
        interactive_brokers: IB,
        notifier: TelegramNotifier,
        config: Config,
        trigger_settlement_callback: Callable[[str, str], Coroutine[Any, Any, None]],
        handle_retriable_error_callback: Callable[[int], Coroutine[Any, Any, None]],
        run_recovery_callback: Callable[[], Coroutine[Any, Any, None]],
        run_reconnect_callback: Callable[[], Coroutine[Any, Any, None]],
        update_account_metrics_callback: (
            Callable[[str], Coroutine[Any, Any, None]] | None
        ) = None,
    ) -> None:
        self.db_factory = db_factory
        self.interactive_brokers = interactive_brokers
        self.notifier = notifier
        self.config = config
        self.trigger_settlement_callback = trigger_settlement_callback
        self.handle_retriable_error_callback = handle_retriable_error_callback
        self.run_recovery_callback = run_recovery_callback
        self.run_reconnect_callback = run_reconnect_callback
        self.update_account_metrics_callback = update_account_metrics_callback
        self._order_locks: dict[int, asyncio.Lock] = {}
        self._broker_connected: bool = True
        self._notified_cancelled_order_ids: set[int] = set()
        self._orders_with_warning_399: set[int] = set()
        self._read_only_alerted: bool = False

    def register_all(self) -> None:
        """Verknüpft die Event-Methoden mit den ib_async Signalen."""
        self.interactive_brokers.connectedEvent.connect(self.on_connected)
        self.interactive_brokers.orderStatusEvent.connect(self.on_order_status)
        self.interactive_brokers.execDetailsEvent.connect(self.on_exec_details)
        self.interactive_brokers.commissionReportEvent.connect(
            self.on_commission_report
        )
        self.interactive_brokers.errorEvent.connect(self.on_error)
        self.interactive_brokers.disconnectedEvent.connect(self.on_disconnected)
        logger.info("All async TWS callbacks successfully registered")

    def on_connected(self) -> None:
        """Setzt den Broker-Verbindungsstatus bei erfolgreichem Socket-Aufbau auf aktiv."""
        self._broker_connected = True
        self._read_only_alerted = False
        logger.info(
            "TWS/Gateway connection established: broker status marked connected"
        )

    @property
    def is_broker_connected(self) -> bool:
        """Gibt an, ob die WAN-Verbindung zum Broker-Server aktiv ist."""
        return self._broker_connected

    def _get_order_lock(self, order_id: int) -> asyncio.Lock:
        """Gibt das Lock für eine spezifische Order ID zurück (erstellt es bei Bedarf)."""
        if order_id not in self._order_locks:
            self._order_locks[order_id] = asyncio.Lock()
        return self._order_locks[order_id]

    def _validate_status_transition(
        self,
        order_id: int,
        current_status: str,
        new_status: str,
    ) -> tuple[bool, bool]:
        """Prüft, ob ein Statusübergang zulässig ist.

        Gibt ein Tupel (is_valid, should_update_perm_id_only) zurück.
        """
        # Terminale Zustände dürfen nicht überschrieben werden
        if current_status in ("Filled", "Cancelled"):
            logger.debug(
                "Ignoring status update for order in terminal state",
                order_id=order_id,
                current_status=current_status,
                new_status=new_status,
            )
            return False, False

        # PendingCancel ist ein flüchtiger Übergangszustand vor der finalen Stornierungsbestätigung
        if new_status == "PendingCancel":
            logger.debug(
                "Order status PendingCancel acknowledged, keeping current status until confirmed",
                order_id=order_id,
                current_status=current_status,
            )
            return False, True

        # Ein Zustand darf nicht von PreSubmitted zurück auf Submitted fallen
        if current_status == "PreSubmitted" and new_status in (
            "Submitted",
            "PendingSubmit",
        ):
            logger.debug(
                "Ignoring status regression from PreSubmitted to Submitted",
                order_id=order_id,
                current_status=current_status,
                new_status=new_status,
            )
            return False, True

        # Ein Fehler-Status darf einen aktiven Zustand nicht überschreiben
        if new_status == "Error" and (
            current_status in ("PreSubmitted", "Submitted")
            or (
                order_id in self._orders_with_warning_399
                and current_status == "Created"
            )
        ):
            logger.info(
                "Ignoring error status update for active/pre-market order (likely warning/ValidationError)",
                order_id=order_id,
                current_status=current_status,
            )
            return False, True

        return True, False

    @staticmethod
    def _is_event_symbol_mismatch(
        event_symbol: str | None,
        db_symbol: str,
        event_sec_type: str | None,
        db_sec_type: str,
        order_id: int,
    ) -> bool:
        """Prüft auf Symbol-Mismatch zwischen TWS-Event und lokalem Datenbank-Record."""
        if event_symbol is not None and not symbols_match(event_symbol, db_symbol):
            logger.warning(
                "Ignoring order status update due to symbol mismatch (ID collision)",
                order_id=order_id,
                event_symbol=event_symbol,
                db_symbol=db_symbol,
                event_sec_type=event_sec_type,
                db_sec_type=db_sec_type,
            )
            return True
        return False

    async def _update_order_status_db(
        self,
        order_id: int,
        status: str,
        permanent_id: int,
        event_symbol: str | None = None,
        event_sec_type: str | None = None,
    ) -> bool:
        """Schreibt das Status-Update atomar in die Datenbank.

        Gibt True zurück, wenn die Statusaktualisierung erfolgreich durchgeführt wurde.
        Gibt False zurück bei Nichtexistenz, Symbol-Mismatch (ID-Kollision) oder bereits terminalem Zustand.
        """
        db = await self.db_factory()
        try:
            async with transaction(db):
                async with db.execute(
                    "SELECT status, symbol, sec_type FROM orders WHERE order_id = ?",
                    (order_id,),
                ) as cursor:
                    row = await cursor.fetchone()

                if not row:
                    logger.debug(
                        "Order not found in database for status update",
                        order_id=order_id,
                        status=status,
                    )
                    return False

                if self._is_event_symbol_mismatch(
                    event_symbol=event_symbol,
                    db_symbol=row["symbol"],
                    event_sec_type=event_sec_type,
                    db_sec_type=row["sec_type"],
                    order_id=order_id,
                ):
                    return False

                is_valid, update_perm_id_only = self._validate_status_transition(
                    order_id, row["status"], status
                )
                if not is_valid:
                    if update_perm_id_only and permanent_id:
                        await db.execute(
                            "UPDATE orders SET perm_id = ? WHERE order_id = ?",
                            (permanent_id, order_id),
                        )
                    return False

                await db.execute(
                    "UPDATE orders SET status = ?, perm_id = ? WHERE order_id = ?",
                    (status, permanent_id, order_id),
                )
                logger.debug(
                    "Order status updated in database", order_id=order_id, status=status
                )
                return True
        except Exception as exception:
            logger.error(
                "Error updating order status in database",
                order_id=order_id,
                error=str(exception),
            )
            return False
        finally:
            await db.close()

    @staticmethod
    def _is_eod_or_oca_reason(reason: str) -> bool:
        """Prüft, ob der Stornierungsgrund auf regulären Ablauf (EOD/GTD/OCA) hinweist."""
        if not reason:
            return False
        reason_lower = reason.lower()
        keywords = ("expired", "time in force", "gtd", "one-cancels-all", "oca")
        return any(keyword in reason_lower for keyword in keywords)

    async def _send_emergency_alert(self, title: str, details: str) -> None:
        """Sendet einen Notfall-Alarm an Telegram bei unbehandelten Ausnahmen in Callbacks."""
        try:
            html = build_tree_message(
                title=f"NOTFALL-ALARM: {title.upper()}",
                emoji="🚨",
                rows=[
                    ("Details", f"<i>{details.strip()}</i>"),
                    ("Zeit", datetime.now().strftime("%d.%m.%Y %H:%M:%S")),
                ],
            )
            await self.notifier.send_message(html)
        except Exception as alert_error:
            logger.critical(
                "Failed to send emergency alert to Telegram",
                title=title,
                alert_error=str(alert_error),
            )

    @staticmethod
    def _map_tws_status(status: str, is_pre_market: bool, order_id: int) -> str:
        """Mappt den TWS-Statusstring auf den internen System-Orderstatus."""
        if status in ("PreSubmitted", "Submitted"):
            return status
        if status == "PendingSubmit":
            return "Submitted"
        if status == "PendingCancel":
            return "PendingCancel"
        if status == "Filled":
            return "Filled"
        if status in ("Cancelled", "Inactive"):
            return "Cancelled"
        if status == "ValidationError" and is_pre_market:
            logger.info(
                "Mapping ValidationError to PreSubmitted due to pre-market hold notice (399)",
                order_id=order_id,
            )
            return "PreSubmitted"
        return "Error"

    @staticmethod
    def _extract_cancellation_reason(trade: Trade) -> str:
        """Extrahiert Grund für Stornierung oder Halten aus TWS-Log und OrderStatus."""
        if getattr(trade, "log", None) and isinstance(trade.log, list):
            for log_entry in reversed(trade.log):
                msg = getattr(log_entry, "message", None)
                if isinstance(msg, str) and msg.strip():
                    return msg.strip()
        why_held = getattr(trade.orderStatus, "whyHeld", None)
        if isinstance(why_held, str) and why_held.strip():
            return why_held.strip()
        return ""

    def on_order_status(self, trade: Trade) -> None:
        """
        Wird aufgerufen, wenn TWS eine Statusänderung einer Order meldet.

        Triggert bei Filled-Status von SL/TP/EXIT das Settlement.
        """
        try:
            order_id = trade.order.orderId
            status = trade.orderStatus.status
            permanent_id = trade.orderStatus.permId

            is_pre_market = (
                order_id in self._orders_with_warning_399
                or is_trade_pre_market_held(trade)
            )
            if is_pre_market:
                self._orders_with_warning_399.add(order_id)

            mapped_status = self._map_tws_status(status, is_pre_market, order_id)
            logger.info(
                "orderStatusEvent received",
                order_id=order_id,
                tws_status=status,
                mapped_status=mapped_status,
            )

            avg_fill_price = (
                trade.orderStatus.avgFillPrice if trade.orderStatus else None
            )
            event_symbol = trade.contract.symbol if trade.contract else None
            event_sec_type = trade.contract.secType if trade.contract else None
            cancel_reason = self._extract_cancellation_reason(trade)

            if cancel_reason:
                asyncio.create_task(
                    self._process_status_change(
                        order_id,
                        mapped_status,
                        permanent_id,
                        avg_fill_price=avg_fill_price,
                        event_symbol=event_symbol,
                        event_sec_type=event_sec_type,
                        reason=cancel_reason,
                    )
                )
            else:
                asyncio.create_task(
                    self._process_status_change(
                        order_id,
                        mapped_status,
                        permanent_id,
                        avg_fill_price=avg_fill_price,
                        event_symbol=event_symbol,
                        event_sec_type=event_sec_type,
                    )
                )

        except Exception as unhandled:
            logger.exception(
                "CRITICAL: Unhandled exception in on_order_status",
                error=str(unhandled),
            )
            asyncio.create_task(
                self._send_emergency_alert(
                    title="🚨 KRITISCHER SYSTEMFEHLER IN ON_ORDER_STATUS",
                    details=f"Ausnahme beim Verarbeiten von orderStatusEvent: {unhandled}",
                )
            )

    async def _handle_filled_status(
        self,
        order_id: int,
        avg_fill_price: float | None = None,
    ) -> None:
        """Verarbeitet gefüllte Orders, alarmiert via Telegram und triggert Settlement."""
        db = await self.db_factory()
        try:
            query = """
                SELECT symbol, sec_type, bracket_role, action, quantity, order_type, target_price, strategy_name, account_id, trade_group_id
                FROM orders
                WHERE order_id = ?
            """
            async with db.execute(query, (order_id,)) as cursor:
                order_row = await cursor.fetchone()

            if not order_row:
                return

            raw_target_price = order_row["target_price"]
            price_decimal = parse_positive_decimal(
                avg_fill_price
            ) or parse_positive_decimal(raw_target_price)
            limit_price_decimal = parse_positive_decimal(raw_target_price)
            sec_type_str = (
                str(order_row["sec_type"])
                if "sec_type" in order_row.keys() and order_row["sec_type"]
                else "STK"
            )

            await self.notifier.send_order_filled(
                symbol=order_row["symbol"],
                bracket_role=order_row["bracket_role"],
                action=order_row["action"],
                quantity=Decimal(str(order_row["quantity"])),
                execution_price=price_decimal,
                order_type=order_row["order_type"],
                order_id=order_id,
                strategy_name=order_row["strategy_name"],
                limit_price=limit_price_decimal,
                sec_type=sec_type_str,
            )

            bracket_role = order_row["bracket_role"]
            trade_group_id = order_row["trade_group_id"]
            account_id = order_row["account_id"]

            if self.update_account_metrics_callback and account_id:
                asyncio.create_task(self.update_account_metrics_callback(account_id))

            if bracket_role in ("SL", "TP", "EXIT"):
                logger.info(
                    "Exit order filled. Triggering settlement.",
                    order_id=order_id,
                    trade_group_id=trade_group_id,
                )
                asyncio.create_task(
                    self.trigger_settlement_callback(trade_group_id, account_id)
                )
        except Exception as exception:
            logger.error(
                "Error during exit check in status callback",
                error=str(exception),
            )
        finally:
            await db.close()

    async def _process_status_change(
        self,
        order_id: int,
        mapped_status: str,
        permanent_id: int,
        avg_fill_price: float | None = None,
        event_symbol: str | None = None,
        event_sec_type: str | None = None,
        reason: str = "",
    ) -> None:
        """Verarbeitet Statusänderung asynchron und triggert ggf. Settlement oder Alarme."""
        try:
            async with self._get_order_lock(order_id):
                updated = await self._update_order_status_db(
                    order_id,
                    mapped_status,
                    permanent_id,
                    event_symbol=event_symbol,
                    event_sec_type=event_sec_type,
                )

            if not updated:
                return

            if mapped_status == "Filled":
                await self._handle_filled_status(order_id, avg_fill_price)
            elif mapped_status == "Cancelled":
                await self._handle_cancelled_status(
                    order_id=order_id,
                    event_symbol=event_symbol,
                    reason=reason,
                )
            elif mapped_status == "Error":
                await self._handle_error_status(
                    order_id=order_id,
                    event_symbol=event_symbol,
                    reason=reason,
                )
        except Exception as unhandled:
            logger.exception(
                "CRITICAL: Unhandled exception in _process_status_change",
                order_id=order_id,
                error=str(unhandled),
            )
            await self._send_emergency_alert(
                title="🚨 KRITISCHER FEHLER IN STATUSVERARBEITUNG",
                details=f"Ausnahme bei _process_status_change für Order {order_id}: {unhandled}",
            )

    async def _fetch_cancellation_context(
        self, order_id: int, db: aiosqlite.Connection, update_cancelled: bool = False
    ) -> tuple[aiosqlite.Row | None, bool, bool]:
        """Lädt Order-Attribute, markiert ggf. als storniert und prüft Geschwister-Exit-Orders."""
        query = """
            SELECT symbol, bracket_role, action, quantity, order_type, target_price, trade_group_id
            FROM orders
            WHERE order_id = ?
        """
        async with db.execute(query, (order_id,)) as cursor:
            order_row = await cursor.fetchone()

        if update_cancelled:
            async with transaction(db):
                await db.execute(
                    "UPDATE orders SET status = 'Cancelled' WHERE order_id = ? AND status NOT IN ('Filled', 'Cancelled')",
                    (order_id,),
                )

        has_filled_sibling = await self._has_filled_sibling_in_group(order_id, db=db)
        has_siblings = (
            await self._has_sibling_exit_legs(order_id, db=db)
            if not has_filled_sibling
            else False
        )
        return order_row, has_filled_sibling, has_siblings

    async def _handle_cancelled_status(
        self,
        order_id: int,
        event_symbol: str | None = None,
        reason: str = "",
    ) -> None:
        """Behandelt Statusänderung auf Cancelled/Inactive mit Two-Factor EOD-Filterung."""
        if order_id in self._orders_with_warning_399 or is_pre_market_hold_notice(
            message=reason
        ):
            logger.info(
                "Order cancelled notification suppressed for pre-market hold (warning 399)",
                order_id=order_id,
                reason=reason,
            )
            return

        async with self._get_order_lock(order_id):
            if order_id in self._notified_cancelled_order_ids:
                return

            db = await self.db_factory()
            order_row = None
            has_filled_sibling = False
            has_siblings = False
            try:
                (
                    order_row,
                    has_filled_sibling,
                    has_siblings,
                ) = await self._fetch_cancellation_context(order_id, db)
            except Exception as exception:
                logger.error(
                    "Error querying order details for cancelled status",
                    order_id=order_id,
                    error=str(exception),
                )
            finally:
                await db.close()

            symbol = (
                order_row["symbol"]
                if order_row
                else (event_symbol if event_symbol else "Unbekannt")
            )
            bracket_role = order_row["bracket_role"] if order_row else "-"

            await self._evaluate_and_notify_cancellation(
                order_id=order_id,
                symbol=symbol,
                bracket_role=bracket_role,
                reason=reason,
                tws_code=0,
                has_filled_sibling=has_filled_sibling,
                has_siblings=has_siblings,
                log_prefix="Order status Cancelled/Inactive notification",
            )

    def _is_cancellation_eod_or_expired(self, symbol: str | None, reason: str) -> bool:
        """Prüft, ob ein Stornierungsgrund auf regulären Marktschluss oder GTD zurückzuführen ist."""
        is_near_close = self._is_near_or_after_market_close(symbol)
        is_gtd_expired = (
            is_past_loc_gtd_cutoff(symbol) if (symbol and is_near_close) else False
        )
        is_eod_oca = self._is_eod_or_oca_reason(reason)
        return is_near_close or is_gtd_expired or is_eod_oca

    async def _evaluate_and_notify_cancellation(
        self,
        order_id: int,
        symbol: str,
        bracket_role: str,
        reason: str,
        tws_code: int,
        has_filled_sibling: bool,
        has_siblings: bool,
        log_prefix: str,
    ) -> None:
        """Prüft EOD-, GTD- und OCA-Bedingungen und sendet bei echten Stornierungen einen Alarm."""
        if self._is_cancellation_eod_or_expired(symbol, reason):
            self._notified_cancelled_order_ids.add(order_id)
            logger.info(
                f"{log_prefix} suppressed (EOD or OCA reason)",
                order_id=order_id,
                symbol=symbol,
                reason=reason,
            )
            return

        if (
            not has_filled_sibling
            and has_siblings
            and bracket_role in ("SL", "TP", "EXIT")
        ):
            # Grace window: Kooperativer Yield für in-flight Sibling-Fills
            await asyncio.sleep(0.15)
            has_filled_sibling = await self._has_filled_sibling_in_group(order_id)

        self._notified_cancelled_order_ids.add(order_id)

        if has_filled_sibling:
            logger.info(
                "Order cancellation notification suppressed (OCA sibling already filled)",
                order_id=order_id,
                symbol=symbol,
                bracket_role=bracket_role,
            )
            return

        clean_reason = (
            reason if reason else "Order durch TWS/Börse storniert oder inaktiviert."
        )
        await self.notifier.send_order_failed(
            order_id=order_id,
            tws_code=tws_code,
            reason=clean_reason,
            symbol=symbol,
            bracket_role=bracket_role,
            is_fatal=False,
        )

    @staticmethod
    def _format_failed_order_reason(error_string: str) -> str:
        """Bereinigt und formatiert Fehlermeldungen inkl. 2FA/Token-Erkennung."""
        if not error_string:
            return "Order im Status Error / ValidationError gemeldet."
        clean_error_string = re.sub(
            r"[ \t]+", " ", re.sub(r"(?i)<br\s*/?>", " ", error_string)
        ).strip()
        reason_upper = clean_error_string.upper()
        if (
            "LOGIN TO CLIENT PORTAL" in reason_upper
            or "VERIFY USING THE TOKEN" in reason_upper
            or "VERIFICATION PROCESS" in reason_upper
            or ("TOKEN" in reason_upper and "VERIFY" in reason_upper)
        ):
            return (
                f"🔑 ANMELDUNG/VERIFIZIERUNG ERFORDERLICH: IBKR/CapTrader verlangt "
                f"Token-Bestätigung im Client Portal! Details: {clean_error_string}"
            )
        return clean_error_string

    async def _dispatch_failed_order_alert(
        self,
        order_id: int,
        tws_code: int,
        reason: str,
        event_symbol: str | None = None,
    ) -> None:
        """Lädt Orderdetails und sendet einen fatalen Fehleralarm an Telegram."""
        db = await self.db_factory()
        order_row = None
        try:
            query = "SELECT symbol, bracket_role FROM orders WHERE order_id = ?"
            async with db.execute(query, (order_id,)) as cursor:
                order_row = await cursor.fetchone()
        except Exception as exception:
            logger.error(
                "Error querying order details for failed order alert",
                order_id=order_id,
                error=str(exception),
            )
        finally:
            await db.close()

        symbol = (
            order_row["symbol"]
            if order_row
            else (event_symbol if event_symbol else "Unbekannt")
        )
        bracket_role = order_row["bracket_role"] if order_row else "-"
        formatted_reason = self._format_failed_order_reason(reason)

        await self.notifier.send_order_failed(
            order_id=order_id,
            tws_code=tws_code,
            reason=formatted_reason,
            symbol=symbol,
            bracket_role=bracket_role,
            is_fatal=True,
        )

    async def _handle_error_status(
        self,
        order_id: int,
        event_symbol: str | None = None,
        reason: str = "",
    ) -> None:
        """Behandelt Statusänderung auf Error mit Alarmierung."""
        if order_id in self._orders_with_warning_399 or is_pre_market_hold_notice(
            message=reason
        ):
            logger.info(
                "Order failed notification suppressed for pre-market hold (warning 399)",
                order_id=order_id,
                reason=reason,
            )
            return

        if order_id in self._notified_cancelled_order_ids:
            return
        self._notified_cancelled_order_ids.add(order_id)

        await self._dispatch_failed_order_alert(
            order_id=order_id,
            tws_code=0,
            reason=reason,
            event_symbol=event_symbol,
        )

    @staticmethod
    async def _check_orders_filled_sibling(
        db: aiosqlite.Connection, order_id: int
    ) -> bool:
        """Prüft in der orders-Tabelle, ob ein Geschwister-Exit-Leg den Status 'Filled' hat."""
        query = """
            SELECT COUNT(*) AS filled_count
            FROM orders AS sibling
            WHERE sibling.trade_group_id = (
                SELECT trade_group_id FROM orders
                WHERE order_id = ? AND bracket_role IN ('SL', 'TP', 'EXIT')
            )
            AND sibling.order_id != ?
            AND sibling.bracket_role IN ('SL', 'TP', 'EXIT')
            AND sibling.status = 'Filled'
        """
        async with db.execute(query, (order_id, order_id)) as cursor:
            row = await cursor.fetchone()
            return bool(row and row["filled_count"] > 0)

    @staticmethod
    async def _check_executions_filled_sibling(
        db: aiosqlite.Connection, order_id: int
    ) -> bool:
        """Prüft in der executions-Tabelle, ob für ein Geschwister-Exit-Leg bereits ein Fill verbucht wurde."""
        query = """
            SELECT COUNT(*) AS exec_count
            FROM executions AS e
            JOIN orders AS sibling ON e.order_id = sibling.order_id
            WHERE sibling.trade_group_id = (
                SELECT trade_group_id FROM orders
                WHERE order_id = ? AND bracket_role IN ('SL', 'TP', 'EXIT')
            )
            AND sibling.order_id != ?
            AND sibling.bracket_role IN ('SL', 'TP', 'EXIT')
        """
        try:
            async with db.execute(query, (order_id, order_id)) as cursor:
                exec_row = await cursor.fetchone()
                return bool(exec_row and exec_row["exec_count"] > 0)
        except Exception:
            return False

    @staticmethod
    async def _check_trade_group_entry_filled(
        db: aiosqlite.Connection, trade_group_id: str
    ) -> bool:
        """Prüft in orders und executions, ob das ENTRY-Leg der Gruppe ausgeführt wurde."""
        if not trade_group_id:
            return True

        query_orders = """
            SELECT status FROM orders
            WHERE trade_group_id = ? AND bracket_role = 'ENTRY'
            LIMIT 1
        """
        async with db.execute(query_orders, (trade_group_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return True
            if row["status"] == "Filled":
                return True

        query_executions = """
            SELECT COUNT(*) AS exec_count
            FROM executions AS e
            JOIN orders AS o ON e.order_id = o.order_id
            WHERE o.trade_group_id = ? AND o.bracket_role = 'ENTRY'
        """
        try:
            async with db.execute(query_executions, (trade_group_id,)) as cursor:
                exec_row = await cursor.fetchone()
                return bool(exec_row and exec_row["exec_count"] > 0)
        except Exception:
            return False

    async def _has_filled_sibling_in_group(
        self, order_id: int, db: aiosqlite.Connection | None = None
    ) -> bool:
        """Prüft, ob eine Geschwister-Exit-Order derselben trade_group_id bereits 'Filled' ist.

        Wenn eine OCA-Gruppe existiert und ein Leg gefüllt wurde, storniert IBKR
        automatisch die verbleibenden Legs. Diese Stornierungen sind regulär und
        erfordern keine Alarm-Benachrichtigung.
        """
        should_close = False
        if db is None:
            db = await self.db_factory()
            should_close = True

        try:
            if await self._check_orders_filled_sibling(db, order_id):
                return True
            return await self._check_executions_filled_sibling(db, order_id)
        except Exception as exception:
            logger.error(
                "Error checking for filled sibling in OCA group",
                order_id=order_id,
                error=str(exception),
            )
            return False
        finally:
            if should_close:
                await db.close()

    async def _has_sibling_exit_legs(
        self, order_id: int, db: aiosqlite.Connection | None = None
    ) -> bool:
        """Prüft, ob für die Order in derselben trade_group_id weitere Exit-Legs existieren."""
        should_close = False
        if db is None:
            db = await self.db_factory()
            should_close = True

        try:
            query = """
                SELECT COUNT(*) AS sibling_count
                FROM orders AS sibling
                WHERE sibling.trade_group_id = (
                    SELECT trade_group_id FROM orders
                    WHERE order_id = ? AND bracket_role IN ('SL', 'TP', 'EXIT')
                )
                AND sibling.order_id != ?
                AND sibling.bracket_role IN ('SL', 'TP', 'EXIT')
            """
            async with db.execute(query, (order_id, order_id)) as cursor:
                row = await cursor.fetchone()
                return bool(row and row["sibling_count"] > 0)
        except Exception as exception:
            logger.error(
                "Error checking for sibling exit legs in trade group",
                order_id=order_id,
                error=str(exception),
            )
            return False
        finally:
            if should_close:
                await db.close()

    def on_exec_details(self, trade: Trade, fill: Fill) -> None:
        """
        Wird bei jeder atomaren Teilausführung (Partial Fill) einer Order aufgerufen.

        Schreibt die Daten idempotent (INSERT OR IGNORE) in die executions-Tabelle.
        """
        exec_id = fill.execution.execId
        order_id = fill.execution.orderId
        price = Decimal(str(fill.execution.price))
        qty = Decimal(str(fill.execution.shares))
        currency = fill.contract.currency
        executed_at = fill.execution.time
        symbol = fill.contract.symbol
        side = fill.execution.side

        logger.info(
            "execDetailsEvent received (partial execution)",
            exec_id=exec_id,
            order_id=order_id,
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
        )

        asyncio.create_task(
            self._save_execution(
                exec_id,
                order_id,
                price,
                qty,
                currency,
                executed_at,
                symbol=symbol,
                trade=trade,
                fill=fill,
            )
        )

    @staticmethod
    def _handle_unmatched_execution(
        order_id: int,
        exec_id: str,
        symbol: str | None,
        db_symbol: str | None,
        trade: object,
        fill: object,
    ) -> None:
        """Protokolliert Ausführungen, die keiner bekannten Order oder passendem Symbol zugeordnet werden können."""
        if trade is not None and fill is not None:
            handle_unassigned_execution(trade, fill)
        else:
            logger.warning(
                "Unassigned execution received (order not found or symbol mismatch in local DB)",
                order_id=order_id,
                exec_id=exec_id,
                event_symbol=symbol,
                db_symbol=db_symbol,
            )

    async def _save_execution(
        self,
        exec_id: str,
        order_id: int,
        price: Decimal,
        qty: Decimal,
        currency: str,
        executed_at: object,
        symbol: str | None = None,
        trade: object = None,
        fill: object = None,
    ) -> None:
        """Speichert ein Ausführungsdetail in der executions-Tabelle."""
        db = await self.db_factory()
        try:
            async with db.execute(
                "SELECT symbol FROM orders WHERE order_id = ?", (order_id,)
            ) as cursor:
                order_row = await cursor.fetchone()

            if not order_row or (
                symbol is not None and not symbols_match(symbol, order_row["symbol"])
            ):
                self._handle_unmatched_execution(
                    order_id=order_id,
                    exec_id=exec_id,
                    symbol=symbol,
                    db_symbol=order_row["symbol"] if order_row else None,
                    trade=trade,
                    fill=fill,
                )
                return

            async with transaction(db):
                await db.execute(
                    """
                    INSERT OR IGNORE INTO executions (exec_id, order_id, price, qty, currency, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        exec_id,
                        order_id,
                        str(price),
                        str(qty),
                        currency,
                        executed_at,
                    ),
                )
            logger.debug(
                "Partial execution idempotently recorded in DB", exec_id=exec_id
            )
        except Exception as exception:
            logger.error(
                "Error saving partial execution",
                exec_id=exec_id,
                error=str(exception),
            )
        finally:
            await db.close()

    def on_commission_report(
        self, trade: Trade, fill: Fill, commission_report: CommissionReport
    ) -> None:
        """
        Empfängt Kommissionsabrechnungen (oft leicht verzögert nach der Ausführung).

        Aktualisiert die Spalten 'commission' und 'currency' in der executions-Tabelle.
        """
        exec_id = fill.execution.execId
        commission = Decimal(str(commission_report.commission))
        currency = commission_report.currency

        logger.info(
            "commissionReportEvent received",
            exec_id=exec_id,
            commission=commission,
            currency=currency,
        )

        asyncio.create_task(self._update_commission(exec_id, commission, currency))

    async def _try_update_commission_attempt(
        self,
        exec_id: str,
        commission: Decimal,
        currency: str,
        attempt: int,
        max_attempts: int,
        retry_delay_s: float,
    ) -> bool:
        """Führt einen einzelnen Versuch zur Aktualisierung der Kommission durch.

        Gibt True zurück, wenn die Aktualisierung erfolgreich war oder max_attempts erreicht wurden.
        """
        db = await self.db_factory()
        try:
            async with transaction(db):
                cursor = await db.execute(
                    "UPDATE executions SET commission = ?, currency = ? WHERE exec_id = ?",
                    (str(commission), currency, exec_id),
                )
                if cursor.rowcount > 0:
                    logger.debug(
                        "Commission for partial execution updated",
                        exec_id=exec_id,
                        attempt=attempt,
                    )
                    return True

            if attempt == max_attempts:
                logger.warning(
                    "Failed to update commission: execution row not found after maximum retries",
                    exec_id=exec_id,
                    max_attempts=max_attempts,
                )
                return True

            logger.debug(
                "Execution row not found yet for commission update. Retrying...",
                exec_id=exec_id,
                attempt=attempt,
                next_retry_in_s=retry_delay_s,
            )
            await asyncio.sleep(retry_delay_s)
            return False

        except Exception as exception:
            logger.error(
                "Error updating commission",
                exec_id=exec_id,
                attempt=attempt,
                error=str(exception),
            )
            if attempt == max_attempts:
                raise
            await asyncio.sleep(retry_delay_s)
            return False
        finally:
            await db.close()

    async def _update_commission(
        self, exec_id: str, commission: Decimal, currency: str
    ) -> None:
        """
        Aktualisiert die Kommission einer Ausführung in der executions-Tabelle.

        Nutzt eine Retry-Schleife, falls die Ausführung (execDetailsEvent)
        aufgrund asynchroner Latenzen noch nicht in der Datenbank existiert.
        """
        max_attempts = 5
        retry_delay_s = 0.05

        for attempt in range(1, max_attempts + 1):
            if await self._try_update_commission_attempt(
                exec_id=exec_id,
                commission=commission,
                currency=currency,
                attempt=attempt,
                max_attempts=max_attempts,
                retry_delay_s=retry_delay_s,
            ):
                return

    @staticmethod
    def _is_ignorable_system_info(request_id: int, error_code: int) -> bool:
        """Prüft, ob es sich um eine unkritische TWS-Systemnachricht handelt (z.B. Marktdatenfarm-Verbindung)."""
        return request_id == -1 and error_code in (2104, 2106, 2158, 2100)

    def on_error(
        self,
        request_id: int,
        error_code: int,
        error_string: str,
        contract: object = None,
    ) -> None:
        """Klassifiziert alle von TWS gemeldeten Error-Codes und reagiert strukturiert.

        Triggert Retries, Warnungen, Verbindungsaufbau oder fatale Fehleralarme.
        """
        try:
            if request_id > 0 and is_pre_market_hold_notice(error_code, error_string):
                self._orders_with_warning_399.add(request_id)
                logger.info(
                    "Pre-market hold notice registered for order (warning 399/2109)",
                    order_id=request_id,
                    code=error_code,
                    message=error_string,
                )

            if self._is_ignorable_system_info(request_id, error_code):
                logger.debug(
                    "TWS system info received", code=error_code, message=error_string
                )
                return

            error_class = classify_error_code(error_code, error_string)
            logger.warning(
                "TWS error message received",
                request_id=request_id,
                code=error_code,
                message=error_string,
                classification=error_class.name,
            )

            asyncio.create_task(
                self._process_error(request_id, error_code, error_string, error_class)
            )
        except Exception as unhandled:
            logger.exception(
                "CRITICAL: Unhandled exception in on_error callback",
                request_id=request_id,
                code=error_code,
                error=str(unhandled),
            )
            asyncio.create_task(
                self._send_emergency_alert(
                    title="🚨 KRITISCHER SYSTEMFEHLER IN ON_ERROR",
                    details=(
                        f"Fehler beim Verarbeiten von TWS-Error {error_code} "
                        f"(reqId={request_id}): {unhandled}\n\nTWS-Text: {error_string}"
                    ),
                )
            )

    async def _handle_connection_error(
        self,
        request_id: int,
        error_code: int,
        error_string: str,
        error_class: ErrorClass,
    ) -> bool:
        """Behandelt Verbindungsverlust- und Reconnect-Meldungen von TWS.

        Gibt True zurück, wenn der Fehler als Verbindungsereignis behandelt wurde.
        """
        if request_id == -1 and error_code in (1100, 2110):
            if self._broker_connected:
                self._broker_connected = False
                await self.notifier.send_broker_connection_status(
                    is_connected=False,
                    error_code=error_code,
                    details=error_string,
                )
            return True

        if error_class == ErrorClass.RECONNECT:
            logger.info("Reconnect signaled. Triggering recovery run.")
            if not self._broker_connected:
                self._broker_connected = True
                await self.notifier.send_broker_connection_status(
                    is_connected=True,
                    error_code=error_code,
                    details=error_string,
                )
            asyncio.create_task(self.run_recovery_callback())
            return True

        return False

    async def _handle_read_only_error(
        self, request_id: int, error_code: int, error_string: str
    ) -> None:
        """Behandelt Read-Only-API-Fehler durch Telegram-Alarm und Markierung der betroffenen Order."""
        if not self._read_only_alerted:
            self._read_only_alerted = True
            await self.notifier.send_read_only_alert(details=error_string)
        if request_id > 0:
            await self._fail_order_in_db(request_id, error_code, error_string)

    async def _dispatch_classified_order_error(
        self,
        request_id: int,
        error_code: int,
        error_string: str,
        error_class: ErrorClass,
    ) -> None:
        """Führt aktionsbasierte Fehlerbehandlung für RETRIABLE, CANCEL oder FATAL durch."""
        if error_class == ErrorClass.RETRIABLE:
            if request_id > 0:
                asyncio.create_task(self.handle_retriable_error_callback(request_id))
            return

        if error_class == ErrorClass.CANCEL:
            if request_id > 0:
                await self._cancel_order_in_db(request_id, error_code, error_string)
            else:
                logger.info(
                    "Broadcast cancel message ignored for system-level request_id",
                    request_id=request_id,
                    code=error_code,
                    message=error_string,
                )
            return

        if error_class == ErrorClass.FATAL:
            if request_id > 0:
                await self._fail_order_in_db(request_id, error_code, error_string)
            else:
                logger.warning(
                    "Broadcast fatal error received without associated order (request_id <= 0)",
                    request_id=request_id,
                    code=error_code,
                    message=error_string,
                )

    async def _process_error(
        self,
        request_id: int,
        error_code: int,
        error_string: str,
        error_class: ErrorClass,
    ) -> None:
        """Verarbeitet klassifizierten API-Fehler."""
        try:
            if is_read_only_error(error_code, error_string):
                await self._handle_read_only_error(request_id, error_code, error_string)
                return

            if error_class == ErrorClass.INFO:
                return

            if await self._handle_connection_error(
                request_id, error_code, error_string, error_class
            ):
                return

            await self._dispatch_classified_order_error(
                request_id, error_code, error_string, error_class
            )
        except Exception as unhandled:
            logger.exception(
                "CRITICAL: Unhandled exception in _process_error",
                request_id=request_id,
                code=error_code,
                error=str(unhandled),
            )
            await self._send_emergency_alert(
                title="🚨 KRITISCHER FEHLER BEI FEHLERVERARBEITUNG",
                details=(
                    f"Ausnahme bei _process_error für Order {request_id} "
                    f"(Code {error_code}): {unhandled}\n\nUrsprünglicher Text: {error_string}"
                ),
            )

    @staticmethod
    def _clean_error_string(error_string: str) -> str:
        """Bereinigt TWS-Fehlermeldungen von HTML-Tags und mehrfachen Leerzeichen."""
        return re.sub(
            r"[ \t]+", " ", re.sub(r"(?i)<br\s*/?>", " ", error_string)
        ).strip()

    def _trigger_loc_verification_if_needed(
        self,
        order_id: int,
        order_row: aiosqlite.Row | None,
        symbol: str,
        is_entry_filled: bool = True,
        has_filled_sibling: bool = False,
    ) -> None:
        """Triggert asynchrone LOC-Schlusskursprüfung nur, wenn die Order-Kriterien erfüllt sind."""
        if not order_row:
            return

        order_type = order_row["order_type"] if "order_type" in order_row.keys() else ""
        raw_target_price = (
            order_row["target_price"] if "target_price" in order_row.keys() else None
        )
        target_price = parse_positive_decimal(raw_target_price)
        bracket_role = (
            order_row["bracket_role"] if "bracket_role" in order_row.keys() else None
        )

        if not is_loc_anomaly_check_warranted(
            order_type=order_type,
            target_price=target_price,
            bracket_role=bracket_role,
            is_entry_filled=is_entry_filled,
            has_filled_sibling=has_filled_sibling,
        ):
            if order_type == "LOC":
                logger.info(
                    "LOC anomaly check skipped: Trade group entry not filled or sibling exit executed",
                    order_id=order_id,
                    symbol=symbol,
                    bracket_role=bracket_role,
                    is_entry_filled=is_entry_filled,
                    has_filled_sibling=has_filled_sibling,
                )
            return

        asyncio.create_task(
            self._check_loc_execution_price(
                order_id=order_id,
                symbol=symbol,
                action=order_row["action"],
                limit_price=Decimal(str(order_row["target_price"])),
                quantity=Decimal(str(order_row["quantity"])),
            )
        )

    async def _cancel_order_in_db(
        self, request_id: int, error_code: int, error_string: str
    ) -> None:
        """Kennzeichnet Order in DB als storniert und benachrichtigt via Telegram."""
        if request_id <= 0:
            return

        async with self._get_order_lock(request_id):
            if request_id in self._notified_cancelled_order_ids:
                return

            db = await self.db_factory()
            order_row = None
            has_filled_sibling = False
            has_siblings = False
            is_entry_filled = True
            try:
                (
                    order_row,
                    has_filled_sibling,
                    has_siblings,
                ) = await self._fetch_cancellation_context(
                    request_id, db, update_cancelled=True
                )
                if (
                    order_row
                    and "trade_group_id" in order_row.keys()
                    and order_row["trade_group_id"]
                ):
                    is_entry_filled = await self._check_trade_group_entry_filled(
                        db, order_row["trade_group_id"]
                    )
            except Exception as exception:
                logger.error(
                    "Error updating DB for cancelled order",
                    order_id=request_id,
                    error=str(exception),
                )
                return
            finally:
                await db.close()

            symbol = order_row["symbol"] if order_row else "Unbekannt"
            bracket_role = order_row["bracket_role"] if order_row else "-"
            clean_error_string = self._clean_error_string(error_string)

            await self._evaluate_and_notify_cancellation(
                order_id=request_id,
                symbol=symbol,
                bracket_role=bracket_role,
                reason=clean_error_string,
                tws_code=error_code,
                has_filled_sibling=has_filled_sibling,
                has_siblings=has_siblings,
                log_prefix="Order cancellation notification",
            )

        self._trigger_loc_verification_if_needed(
            request_id,
            order_row,
            symbol,
            is_entry_filled=is_entry_filled,
            has_filled_sibling=has_filled_sibling,
        )

    @staticmethod
    def _evaluate_loc_eligibility(
        action: str, close_price: Decimal, limit_price: Decimal
    ) -> bool:
        """Prüft, ob die Order basierend auf Schluss- und Limitpreis ausgeführt hätte werden müssen."""
        if action.upper() == "BUY" and close_price <= limit_price:
            return True
        if action.upper() == "SELL" and close_price >= limit_price:
            return True
        return False

    async def _fetch_loc_closing_bar(
        self, symbol: str, order_id: int
    ) -> BarData | None:
        """Fragt die täglichen historischen Bars ab und validiert das heutige Datum."""
        contract = make_stock_contract(symbol)

        # Kurz warten, bis IBKR-Server den Schlusskurs finalisiert haben
        await asyncio.sleep(5)

        bars = None
        for attempt in range(1, 4):
            try:
                bars = await self.interactive_brokers.reqHistoricalDataAsync(
                    contract=contract,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                    keepUpToDate=False,
                )
                if bars:
                    break
            except Exception as historical_data_error:
                logger.warning(
                    "Attempt to fetch historical close price failed",
                    symbol=symbol,
                    attempt=attempt,
                    error=str(historical_data_error),
                )
            await asyncio.sleep(5)

        if not bars:
            logger.warning(
                "Could not retrieve daily historical bars for LOC check",
                symbol=symbol,
                order_id=order_id,
            )
            return None

        last_bar = bars[-1]
        if not self._is_bar_from_today(last_bar.date, symbol):
            logger.warning(
                "Retrieved daily bar is not from today. Close price check skipped.",
                symbol=symbol,
                order_id=order_id,
                bar_date=str(last_bar.date),
            )
            return None

        return last_bar

    async def _check_loc_execution_price(
        self,
        order_id: int,
        symbol: str,
        action: str,
        limit_price: Decimal,
        quantity: Decimal,
    ) -> None:
        """Prüft nach Marktschluss, ob der Schlusskurs den Limitpreis einer stornierten LOC-Order erreicht hat."""
        if not self._is_near_or_after_market_close(symbol):
            logger.debug(
                "Skipping LOC close price check: cancellation occurred before market close",
                order_id=order_id,
                symbol=symbol,
            )
            return

        logger.info(
            "Starting LOC execution price check",
            order_id=order_id,
            symbol=symbol,
            action=action,
            limit_price=limit_price,
        )

        try:
            last_bar = await self._fetch_loc_closing_bar(symbol, order_id)
            if not last_bar:
                return

            close_price = Decimal(str(last_bar.close))
            logger.info(
                "LOC verification: retrieved close price",
                symbol=symbol,
                close_price=close_price,
                limit_price=limit_price,
            )

            was_eligible = self._evaluate_loc_eligibility(
                action, close_price, limit_price
            )
            await self._handle_loc_anomaly_result(
                was_eligible=was_eligible,
                order_id=order_id,
                symbol=symbol,
                action=action,
                limit_price=limit_price,
                close_price=close_price,
                quantity=quantity,
            )
        except Exception as exception:
            logger.error(
                "Error checking LOC execution price",
                order_id=order_id,
                symbol=symbol,
                error=str(exception),
            )

    async def _handle_loc_anomaly_result(
        self,
        was_eligible: bool,
        order_id: int,
        symbol: str,
        action: str,
        limit_price: Decimal,
        close_price: Decimal,
        quantity: Decimal,
    ) -> None:
        """Protokolliert und alarmiert bei Unstimmigkeiten zwischen Schluss- und Limitpreis."""
        if was_eligible:
            logger.error(
                "LOC order anomaly detected: Limit price reached but order cancelled",
                order_id=order_id,
                symbol=symbol,
                action=action,
                limit_price=limit_price,
                close_price=close_price,
            )
            await self.notifier.send_loc_execution_anomaly(
                order_id=order_id,
                symbol=symbol,
                action=action,
                limit_price=limit_price,
                close_price=close_price,
                quantity=quantity,
            )
        else:
            logger.debug(
                "LOC order cancellation justified: limit price not reached by close price",
                order_id=order_id,
                symbol=symbol,
                limit_price=limit_price,
                close_price=close_price,
            )

    @staticmethod
    def _get_localized_time(
        target_zone: ZoneInfo, current_time: datetime | None = None
    ) -> datetime:
        """Konvertiert oder ermittelt die aktuelle Zeit in der angegebenen Zeitzone."""
        if current_time and current_time.tzinfo:
            return current_time.astimezone(target_zone)
        if current_time:
            return current_time.replace(tzinfo=target_zone)
        return datetime.now(target_zone)

    @staticmethod
    def _is_near_or_after_market_close(
        symbol: str | None, current_time: datetime | None = None
    ) -> bool:
        """Überprüft, ob der aktuelle Zeitpunkt nahe oder nach dem regulären Marktschluss liegt."""
        if symbol and symbol.upper().endswith(".DE"):
            # Deutscher Markt (Xetra) schließt um 17:30 Uhr Berlin-Zeit (Cutoff 17:15)
            now_berlin = TwsCallbacksManager._get_localized_time(
                ZoneInfo("Europe/Berlin"), current_time
            )
            market_close = now_berlin.replace(
                hour=17, minute=15, second=0, microsecond=0
            )
            return now_berlin >= market_close

        # US-Markt (NASDAQ/NYSE) schließt um 16:00 Uhr New York-Zeit (Cutoff 15:45)
        now_ny = TwsCallbacksManager._get_localized_time(
            ZoneInfo("America/New_York"), current_time
        )
        market_close = now_ny.replace(hour=15, minute=45, second=0, microsecond=0)
        return now_ny >= market_close

    @staticmethod
    def _is_bar_from_today(bar_date: object, symbol: str) -> bool:
        """Überprüft, ob das Datum des Bars dem heutigen Handelstag entspricht."""
        symbol_upper = symbol.upper()
        tz = (
            ZoneInfo("Europe/Berlin")
            if symbol_upper.endswith(".DE")
            else ZoneInfo("America/New_York")
        )
        today = datetime.now(tz).date()

        if hasattr(bar_date, "date") and callable(bar_date.date):
            return bool(bar_date.date() == today)
        elif isinstance(bar_date, date):
            return bar_date == today
        elif isinstance(bar_date, str):
            try:
                # Format 'YYYYMMDD' oder 'YYYYMMDD  HH:MM:SS'
                parsed_date = datetime.strptime(bar_date[:8], "%Y%m%d").date()
                return parsed_date == today
            except (ValueError, TypeError):
                return False
        return False

    async def _fail_order_in_db(
        self, request_id: int, error_code: int, error_string: str
    ) -> None:
        """Kennzeichnet Order in DB als fehlerhaft und benachrichtigt via Telegram."""
        if request_id <= 0:
            return

        db = await self.db_factory()
        try:
            async with transaction(db):
                await db.execute(
                    "UPDATE orders SET status = 'Error' WHERE order_id = ?",
                    (request_id,),
                )
        except Exception as exception:
            logger.error(
                "Error updating DB for fatal order",
                order_id=request_id,
                error=str(exception),
            )
            return
        finally:
            await db.close()

        await self._dispatch_failed_order_alert(
            order_id=request_id,
            tws_code=error_code,
            reason=error_string,
        )

    @staticmethod
    def _is_planned_weekly_gateway_restart(
        current_time: datetime | None = None,
    ) -> bool:
        """Prüft, ob der Zeitpunkt dem wöchentlichen IBKR-Gateway-Neustart (Sonntag 12:00-12:05) entspricht."""
        time_to_check = (
            current_time if current_time is not None else datetime_module.datetime.now()
        )
        return (
            time_to_check.weekday() == 6
            and time_to_check.hour == 12
            and 0 <= time_to_check.minute < 5
        )

    def on_disconnected(self) -> None:
        """Loggt Verbindungsverlust zu TWS und alarmiert den Betreiber."""
        self._broker_connected = False
        is_planned = self._is_planned_weekly_gateway_restart()

        if is_planned:
            logger.info(
                "Planned weekly Gateway restart detected (Sunday 12:00). Suppressing fatal alerts."
            )
            asyncio.create_task(
                self.notifier.send_system_status(
                    title="GEPLANTER NEUSTART (Gateway wird neu gestartet)",
                    emoji="⏳",
                    system="IBKR Gateway",
                )
            )
        else:
            logger.error("Connection to Interactive Brokers TWS lost unexpectedly!")
            asyncio.create_task(
                self.notifier.send_system_status(
                    title="VERBINDUNGSABBRUCH",
                    emoji="🚨",
                    system="IBKR Gateway",
                )
            )
        asyncio.create_task(self.run_reconnect_callback())
