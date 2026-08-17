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
from collections.abc import Awaitable, Callable, Coroutine
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import structlog
from ib_async import IB, CommissionReport, Fill, Trade

from app.core.config import Config
from app.core.db import transaction
from app.core.models import parse_positive_decimal
from app.services.notifier import TelegramNotifier
from app.trading.error_codes import ErrorClass, classify_error_code

logger = structlog.get_logger()


def extract_unassigned_execution_details(
    trade: object, fill: object
) -> dict[str, object]:
    """
    Extrahiert alle verfügbaren Vertrags- und Ausführungsdetails aus einem TWS Trade- & Fill-Objekt.

    Wird verwendet, um bei unzugeordneten/unbekannten Orders alle Attribute (Symbol, Stückzahl,
    Preis, Börse, Konto etc.) vollständig zu erfassen.
    """
    contract = getattr(fill, "contract", None) or (
        getattr(trade, "contract", None) if trade else None
    )
    execution = getattr(fill, "execution", None) if fill else None
    order = getattr(trade, "order", None) if trade else None

    symbol = getattr(contract, "symbol", "") if contract else ""
    sec_type = getattr(contract, "secType", "") if contract else ""
    exchange = (
        getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", "")
        if contract or execution
        else ""
    )
    currency = getattr(contract, "currency", "") if contract else ""

    side = (
        getattr(execution, "side", "") or getattr(order, "action", "")
        if execution or order
        else ""
    )
    qty_raw = getattr(execution, "shares", None) if execution else None
    qty = Decimal(str(qty_raw)) if qty_raw is not None else None

    price_raw = getattr(execution, "price", None) if execution else None
    price = Decimal(str(price_raw)) if price_raw is not None else None

    account_id = (
        getattr(execution, "acctNumber", "") or getattr(order, "account", "")
        if execution or order
        else ""
    )
    order_id = getattr(execution, "orderId", 0) if execution else 0
    perm_id = getattr(execution, "permId", None) if execution else None
    exec_id = getattr(execution, "execId", "") if execution else ""
    executed_at = getattr(execution, "time", None) if execution else None
    order_ref = getattr(order, "orderRef", "") if order else ""

    return {
        "symbol": symbol,
        "sec_type": sec_type,
        "exchange": exchange,
        "currency": currency,
        "side": side,
        "qty": qty,
        "price": price,
        "account_id": account_id,
        "order_id": order_id,
        "perm_id": perm_id,
        "exec_id": exec_id,
        "executed_at": executed_at,
        "order_ref": order_ref,
    }


def handle_unassigned_execution(trade: object, fill: object) -> dict[str, object]:
    """
    Protokolliert eine Ausführung, die keiner bekannten Order in der lokalen DB zugewiesen werden kann.

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
    ) -> None:
        self.db_factory = db_factory
        self.interactive_brokers = interactive_brokers
        self.notifier = notifier
        self.config = config
        self.trigger_settlement_callback = trigger_settlement_callback
        self.handle_retriable_error_callback = handle_retriable_error_callback
        self.run_recovery_callback = run_recovery_callback
        self.run_reconnect_callback = run_reconnect_callback
        self._order_locks: dict[int, asyncio.Lock] = {}
        self._broker_connected: bool = True

    def register_all(self) -> None:
        """Verknüpft die Event-Methoden mit den ib_async Signalen."""
        self.interactive_brokers.orderStatusEvent.connect(self.on_order_status)
        self.interactive_brokers.execDetailsEvent.connect(self.on_exec_details)
        self.interactive_brokers.commissionReportEvent.connect(
            self.on_commission_report
        )
        self.interactive_brokers.errorEvent.connect(self.on_error)
        self.interactive_brokers.disconnectedEvent.connect(self.on_disconnected)
        logger.info("All async TWS callbacks successfully registered")

    def _get_order_lock(self, order_id: int) -> asyncio.Lock:
        """Gibt das Lock für eine spezifische Order ID zurück (erstellt es bei Bedarf)."""
        if order_id not in self._order_locks:
            self._order_locks[order_id] = asyncio.Lock()
        return self._order_locks[order_id]

    async def _update_order_status_db(
        self, order_id: int, status: str, permanent_id: int
    ) -> None:
        """Schreibt das Status-Update atomar in die Datenbank."""
        db = await self.db_factory()
        try:
            async with transaction(db):
                # Aktuellen Status abfragen, um ungültige Zustandsübergänge zu verhindern
                async with db.execute(
                    "SELECT status FROM orders WHERE order_id = ?", (order_id,)
                ) as cursor:
                    row = await cursor.fetchone()

                if row:
                    current_status = row["status"]

                    # Terminale Zustände dürfen nicht überschrieben werden
                    if current_status in ("Filled", "Cancelled"):
                        logger.debug(
                            "Ignoring status update for order in terminal state",
                            order_id=order_id,
                            current_status=current_status,
                            new_status=status,
                        )
                        return

                    # Ein Fehler-Status darf einen aktiven Zustand nicht überschreiben
                    if status == "Error" and current_status in (
                        "PreSubmitted",
                        "Submitted",
                    ):
                        logger.info(
                            "Ignoring error status update for active order (likely warning/ValidationError)",
                            order_id=order_id,
                            current_status=current_status,
                        )
                        if permanent_id:
                            await db.execute(
                                "UPDATE orders SET perm_id = ? WHERE order_id = ?",
                                (permanent_id, order_id),
                            )
                        return

                await db.execute(
                    "UPDATE orders SET status = ?, perm_id = ? WHERE order_id = ?",
                    (status, permanent_id, order_id),
                )
                logger.debug(
                    "Order status updated in database", order_id=order_id, status=status
                )
        except Exception as exception:
            logger.error(
                "Error updating order status in database",
                order_id=order_id,
                error=str(exception),
            )
        finally:
            await db.close()

    def on_order_status(self, trade: Trade) -> None:
        """
        Wird aufgerufen, wenn TWS eine Statusänderung einer Order meldet.

        Triggert bei Filled-Status von SL/TP/EXIT das Settlement.
        """
        order_id = trade.order.orderId
        status = trade.orderStatus.status
        permanent_id = trade.orderStatus.permId

        mapped_status = status
        if status in ("PreSubmitted", "Submitted"):
            mapped_status = status
        elif status == "Filled":
            mapped_status = "Filled"
        elif status in ("Cancelled", "Inactive"):
            mapped_status = "Cancelled"
        else:
            mapped_status = "Error"

        logger.info(
            "orderStatusEvent received",
            order_id=order_id,
            tws_status=status,
            mapped_status=mapped_status,
        )

        avg_fill_price = trade.orderStatus.avgFillPrice if trade.orderStatus else None

        asyncio.create_task(
            self._process_status_change(
                order_id, mapped_status, permanent_id, avg_fill_price
            )
        )

    async def _process_status_change(
        self,
        order_id: int,
        mapped_status: str,
        permanent_id: int,
        avg_fill_price: float | None = None,
    ) -> None:
        """Verarbeitet Statusänderung asynchron und triggert ggf. Settlement."""
        async with self._get_order_lock(order_id):
            await self._update_order_status_db(order_id, mapped_status, permanent_id)

        if mapped_status != "Filled":
            return

        db = await self.db_factory()
        try:
            # Details für die Benachrichtigung und das Settlement abfragen
            query = """
                SELECT symbol, bracket_role, action, quantity, order_type, target_price, strategy_name, account_id, trade_group_id
                FROM orders
                WHERE order_id = ?
            """
            async with db.execute(query, (order_id,)) as cursor:
                order_row = await cursor.fetchone()

            if not order_row:
                return

            raw_target_price = order_row["target_price"]

            # Tatsächlichen Kurs bevorzugen (avg_fill_price), falls vorhanden und positiv, sonst target_price aus DB
            price_decimal = parse_positive_decimal(
                avg_fill_price
            ) or parse_positive_decimal(raw_target_price)

            # Limit-Preis aus der DB für die Slippage-Anzeige aufbereiten
            limit_price_decimal = parse_positive_decimal(raw_target_price)

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
            )

            bracket_role = order_row["bracket_role"]
            trade_group_id = order_row["trade_group_id"]
            account_id = order_row["account_id"]

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
                trade=trade,
                fill=fill,
            )
        )

    async def _save_execution(
        self,
        exec_id: str,
        order_id: int,
        price: Decimal,
        qty: Decimal,
        currency: str,
        executed_at: object,
        trade: object = None,
        fill: object = None,
    ) -> None:
        """Speichert ein Ausführungsdetail in der executions-Tabelle."""
        db = await self.db_factory()
        try:
            # Überprüfen, ob die Order in unserer DB existiert (verhindert FK-Fehler bei TWS-manuellen Orders)
            async with db.execute(
                "SELECT 1 FROM orders WHERE order_id = ?", (order_id,)
            ) as cursor:
                exists = await cursor.fetchone()

            if not exists:
                if trade is not None and fill is not None:
                    handle_unassigned_execution(trade, fill)
                else:
                    logger.warning(
                        "Unassigned execution received (order not found in local DB)",
                        order_id=order_id,
                        exec_id=exec_id,
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
                        return

                if attempt == max_attempts:
                    logger.warning(
                        "Failed to update commission: execution row not found after maximum retries",
                        exec_id=exec_id,
                        max_attempts=max_attempts,
                    )
                    return

                logger.debug(
                    "Execution row not found yet for commission update. Retrying...",
                    exec_id=exec_id,
                    attempt=attempt,
                    next_retry_in_s=retry_delay_s,
                )
                await asyncio.sleep(retry_delay_s)

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
            finally:
                await db.close()

    def on_error(self, request_id: int, error_code: int, error_string: str) -> None:
        """
        Klassifiziert alle von TWS gemeldeten Error-Codes und reagiert strukturiert.

        Triggert Retries, Warnungen, Verbindungsaufbau oder fatale Fehleralarme.
        """
        if request_id == -1 and error_code in (2104, 2106, 2158, 2100):
            logger.debug(
                "TWS system info received", code=error_code, message=error_string
            )
            return

        error_class = classify_error_code(error_code)
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

    async def _process_error(
        self,
        request_id: int,
        error_code: int,
        error_string: str,
        error_class: ErrorClass,
    ) -> None:
        """Verarbeitet klassifizierten API-Fehler."""
        if error_class == ErrorClass.INFO:
            return

        # Systemweite Broker-Konnektivitätsfehler (request_id == -1)
        if request_id == -1 and error_code in (1100, 2110):
            if self._broker_connected:
                self._broker_connected = False
                await self.notifier.send_broker_connection_status(
                    is_connected=False,
                    error_code=error_code,
                    details=error_string,
                )
            return

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
            return

        if error_class == ErrorClass.RETRIABLE:
            if request_id > 0:
                asyncio.create_task(self.handle_retriable_error_callback(request_id))
            return

        if error_class == ErrorClass.CANCEL:
            await self._cancel_order_in_db(request_id, error_code, error_string)
            return

        if error_class == ErrorClass.FATAL:
            await self._fail_order_in_db(request_id, error_code, error_string)
            return

    async def _cancel_order_in_db(
        self, request_id: int, error_code: int, error_string: str
    ) -> None:
        """Kennzeichnet Order in DB als storniert und benachrichtigt via Telegram."""
        db = await self.db_factory()
        order_row = None
        try:
            # Details für die Benachrichtigung laden
            query = """
                SELECT symbol, bracket_role, action, quantity, order_type, target_price
                FROM orders
                WHERE order_id = ?
            """
            async with db.execute(query, (request_id,)) as cursor:
                order_row = await cursor.fetchone()

            async with transaction(db):
                await db.execute(
                    "UPDATE orders SET status = 'Cancelled' WHERE order_id = ?",
                    (request_id,),
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

        await self.notifier.send_order_failed(
            order_id=request_id,
            tws_code=error_code,
            reason=error_string,
            symbol=symbol,
            bracket_role=bracket_role,
            is_fatal=False,
        )

        # Überprüfung bei LOC-Orders nach Marktschluss anstoßen
        if (
            order_row
            and order_row["order_type"] == "LOC"
            and order_row["target_price"] is not None
        ):
            asyncio.create_task(
                self._check_loc_execution_price(
                    order_id=request_id,
                    symbol=symbol,
                    action=order_row["action"],
                    limit_price=Decimal(str(order_row["target_price"])),
                    quantity=Decimal(str(order_row["quantity"])),
                )
            )

    async def _check_loc_execution_price(
        self,
        order_id: int,
        symbol: str,
        action: str,
        limit_price: Decimal,
        quantity: Decimal,
    ) -> None:
        """
        Prüft nach dem Marktschluss, ob der Schlusskurs den Limitpreis einer stornierten
        LOC-Order erreicht hat und alarmiert bei Abweichungen.
        """
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
            from app.trading.order_builder import make_stock_contract

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
                return

            last_bar = bars[-1]
            if not self._is_bar_from_today(last_bar.date, symbol):
                logger.warning(
                    "Retrieved daily bar is not from today. Close price check skipped.",
                    symbol=symbol,
                    order_id=order_id,
                    bar_date=str(last_bar.date),
                )
                return

            close_price = Decimal(str(last_bar.close))
            logger.info(
                "LOC verification: retrieved close price",
                symbol=symbol,
                close_price=close_price,
                limit_price=limit_price,
            )

            # Abgleich der Ausführungsbedingungen
            was_eligible = False
            if action.upper() == "BUY" and close_price <= limit_price:
                was_eligible = True
            elif action.upper() == "SELL" and close_price >= limit_price:
                was_eligible = True

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

        except Exception as exception:
            logger.error(
                "Error checking LOC execution price",
                order_id=order_id,
                symbol=symbol,
                error=str(exception),
            )

    def _is_near_or_after_market_close(self, symbol: str) -> bool:
        """Überprüft, ob der aktuelle Zeitpunkt nahe oder nach dem regulären Marktschluss liegt."""
        symbol_upper = symbol.upper()
        if symbol_upper.endswith(".DE"):
            # Deutscher Markt (Xetra) schließt um 17:30 Uhr Berlin-Zeit
            berlin_tz = ZoneInfo("Europe/Berlin")
            now_berlin = datetime.now(berlin_tz)
            market_close = now_berlin.replace(
                hour=17, minute=25, second=0, microsecond=0
            )
            return now_berlin >= market_close
        else:
            # US-Markt (NASDAQ/NYSE) schließt um 16:00 Uhr New York-Zeit
            ny_tz = ZoneInfo("America/New_York")
            now_ny = datetime.now(ny_tz)
            market_close = now_ny.replace(hour=15, minute=55, second=0, microsecond=0)
            return now_ny >= market_close

    def _is_bar_from_today(self, bar_date: object, symbol: str) -> bool:
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
        db = await self.db_factory()
        order_row = None
        try:
            query = """
                SELECT symbol, bracket_role
                FROM orders
                WHERE order_id = ?
            """
            async with db.execute(query, (request_id,)) as cursor:
                order_row = await cursor.fetchone()

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

        symbol = order_row["symbol"] if order_row else "Unbekannt"
        bracket_role = order_row["bracket_role"] if order_row else "-"

        reason = error_string
        reason_upper = error_string.upper()
        if (
            "LOGIN TO CLIENT PORTAL" in reason_upper
            or "VERIFY USING THE TOKEN" in reason_upper
            or "VERIFICATION PROCESS" in reason_upper
            or ("TOKEN" in reason_upper and "VERIFY" in reason_upper)
        ):
            reason = (
                f"🔑 ANMELDUNG/VERIFIZIERUNG ERFORDERLICH: IBKR/CapTrader verlangt "
                f"Token-Bestätigung im Client Portal! Details: {error_string}"
            )

        await self.notifier.send_order_failed(
            order_id=request_id,
            tws_code=error_code,
            reason=reason,
            symbol=symbol,
            bracket_role=bracket_role,
            is_fatal=True,
        )

    def on_disconnected(self) -> None:
        """Loggt Verbindungsverlust zu TWS und alarmiert den Betreiber."""
        self._broker_connected = False
        import datetime as datetime_module

        now = datetime_module.datetime.now()
        is_planned = now.hour == 12 and 0 <= now.minute < 5

        if is_planned:
            logger.info(
                "Planned daily Gateway restart detected. Suppressing fatal alerts."
            )
            asyncio.create_task(
                self.notifier.send_system_status(
                    title="GEPLANTER NEUSTART (Gateway wird neu gestartet)",
                    emoji="⏳",
                )
            )
        else:
            logger.error("Connection to Interactive Brokers TWS lost unexpectedly!")
            asyncio.create_task(
                self.notifier.send_system_status(
                    title="VERBINDUNGSABBRUCH",
                    emoji="🚨",
                )
            )
        asyncio.create_task(self.run_reconnect_callback())
