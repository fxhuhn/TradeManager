"""
Callback-Manager für TWS-API-Events.

Registriert Event-Handler für Order-Statusaktualisierungen, Ausführungsberichte,
Kommissionen, Fehlermeldungen und Verbindungsabbrüche der Trader Workstation (TWS).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal

import aiosqlite
import structlog
from ib_async import IB, CommissionReport, Fill, Trade

from app.core.config import Config
from app.services.notifier import TelegramNotifier
from app.trading.error_codes import ErrorClass, classify_error_code

logger = structlog.get_logger()


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
        trigger_settlement_callback: Callable[[str, str], Awaitable[None]],
        handle_retriable_error_callback: Callable[[int], Awaitable[None]],
        run_recovery_callback: Callable[[], Awaitable[None]],
        run_reconnect_callback: Callable[[], Awaitable[None]],
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

    def register_all(self) -> None:
        """Verknüpft die Event-Methoden mit den ib_async Signalen."""
        self.interactive_brokers.orderStatusEvent.connect(self.on_order_status)
        self.interactive_brokers.execDetailsEvent.connect(self.on_exec_details)
        self.interactive_brokers.commissionReportEvent.connect(
            self.on_commission_report
        )
        self.interactive_brokers.errorEvent.connect(self.on_error)
        self.interactive_brokers.disconnectedEvent.connect(self.on_disconnected)
        logger.info("Alle asynchronen TWS-Callbacks erfolgreich registriert")

    def _get_order_lock(self, order_id: int) -> asyncio.Lock:
        """Gibt das Lock für eine spezifische Order ID zurück (erstellt es bei Bedarf)."""
        if order_id not in self._order_locks:
            self._order_locks[order_id] = asyncio.Lock()
        return self._order_locks[order_id]

    async def _update_order_status_db(
        self, order_id: int, status: str, perm_id: int
    ) -> None:
        """Schreibt das Status-Update atomar in die Datenbank."""
        db = await self.db_factory()
        await db.execute("BEGIN IMMEDIATE")
        try:
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
                        "Ignoriere Status-Update für Order in terminalem Zustand",
                        order_id=order_id,
                        current_status=current_status,
                        new_status=status,
                    )
                    await db.execute("COMMIT")
                    return

                # Ein Fehler-Status (z. B. durch ValidationError-Warnung) darf einen aktiven Zustand nicht überschreiben
                if status == "Error" and current_status in ("PreSubmitted", "Submitted"):
                    logger.info(
                        "Ignoriere Error-Status-Update für aktive Order (vermutlich Warnung/ValidationError)",
                        order_id=order_id,
                        current_status=current_status,
                    )
                    if perm_id:
                        await db.execute(
                            "UPDATE orders SET perm_id = ? WHERE order_id = ?",
                            (perm_id, order_id),
                        )
                    await db.execute("COMMIT")
                    return

            await db.execute(
                "UPDATE orders SET status = ?, perm_id = ? WHERE order_id = ?",
                (status, perm_id, order_id),
            )
            await db.execute("COMMIT")
            logger.debug(
                "Order-Zustand in DB aktualisiert", order_id=order_id, status=status
            )
        except Exception as exception:
            await db.execute("ROLLBACK")
            logger.error(
                "Fehler beim DB-Update des Order-Status",
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
        perm_id = trade.orderStatus.permId

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
            "orderStatusEvent empfangen",
            order_id=order_id,
            tws_status=status,
            mapped_status=mapped_status,
        )

        asyncio.create_task(
            self._process_status_change(order_id, mapped_status, perm_id)
        )

    async def _process_status_change(
        self, order_id: int, mapped_status: str, perm_id: int
    ) -> None:
        """Verarbeitet Statusänderung asynchron und triggert ggf. Settlement."""
        async with self._get_order_lock(order_id):
            await self._update_order_status_db(order_id, mapped_status, perm_id)

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
            target_price_decimal = (
                Decimal(str(raw_target_price))
                if raw_target_price is not None
                else None
            )
            await self.notifier.send_order_filled(
                symbol=order_row["symbol"],
                bracket_role=order_row["bracket_role"],
                action=order_row["action"],
                quantity=float(order_row["quantity"]),
                price=float(target_price_decimal) if target_price_decimal else 0.0,
                order_type=order_row["order_type"],
                order_id=order_id,
                strategy_name=order_row["strategy_name"],
            )

            bracket_role = order_row["bracket_role"]
            trade_group_id = order_row["trade_group_id"]
            account_id = order_row["account_id"]

            if bracket_role in ("SL", "TP", "EXIT"):
                logger.info(
                    "Exit-Order gefüllt! Settlement wird ausgelöst.",
                    order_id=order_id,
                    trade_group_id=trade_group_id,
                )
                asyncio.create_task(
                    self.trigger_settlement_callback(trade_group_id, account_id)
                )
        except Exception as exception:
            logger.error(
                "Fehler bei Exit-Pruefung im Status-Callback",
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

        logger.info(
            "execDetailsEvent empfangen (Teilausfuehrung)",
            exec_id=exec_id,
            order_id=order_id,
            price=price,
            qty=qty,
        )

        asyncio.create_task(
            self._save_execution(
                exec_id, order_id, price, qty, currency, executed_at
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
    ) -> None:
        """Speichert ein Ausführungsdetail in der executions-Tabelle."""
        db = await self.db_factory()
        await db.execute("BEGIN IMMEDIATE")
        try:
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
            await db.execute("COMMIT")
            logger.debug(
                "Teilausfuehrung idempotent in DB verbucht", exec_id=exec_id
            )
        except Exception as exception:
            await db.execute("ROLLBACK")
            logger.error(
                "Fehler beim Speichern der Teilausfuehrung",
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
            "commissionReportEvent empfangen",
            exec_id=exec_id,
            commission=commission,
            currency=currency,
        )

        asyncio.create_task(self._update_commission(exec_id, commission, currency))

    async def _update_commission(
        self, exec_id: str, commission: Decimal, currency: str
    ) -> None:
        """Aktualisiert die Kommission einer Ausführung in der executions-Tabelle."""
        db = await self.db_factory()
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "UPDATE executions SET commission = ?, currency = ? WHERE exec_id = ?",
                (str(commission), currency, exec_id),
            )
            await db.execute("COMMIT")
            logger.debug(
                "Kommission fuer Teilausfuehrung aktualisiert", exec_id=exec_id
            )
        except Exception as exception:
            await db.execute("ROLLBACK")
            logger.error(
                "Fehler beim Aktualisieren der Kommission",
                exec_id=exec_id,
                error=str(exception),
            )
        finally:
            await db.close()

    def on_error(self, request_id: int, error_code: int, error_string: str) -> None:
        """
        Klassifiziert alle von TWS gemeldeten Error-Codes und reagiert strukturiert.

        Triggert Retries, Warnungen, Verbindungsaufbau oder fatale Fehleralarme.
        """
        if request_id == -1 and error_code in (2104, 2106, 2158, 2100):
            logger.debug(
                "TWS System-Info empfangen", code=error_code, message=error_string
            )
            return

        error_class = classify_error_code(error_code)
        logger.warning(
            "TWS-Fehlermeldung empfangen",
            request_id=request_id,
            code=error_code,
            message=error_string,
            klassifizierung=error_class.name,
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

        if error_class == ErrorClass.RECONNECT:
            logger.info("Reconnect signalisiert. Trigger Recovery-Lauf.")
            asyncio.create_task(self.run_recovery_callback())
            return

        if error_class == ErrorClass.RETRIABLE:
            asyncio.create_task(
                self.handle_retriable_error_callback(request_id)
            )
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
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "UPDATE orders SET status = 'Cancelled' WHERE order_id = ?",
                (request_id,),
            )
            await db.execute("COMMIT")
        except Exception as exception:
            await db.execute("ROLLBACK")
            logger.error(
                "Fehler bei Cancel-Order DB-Update",
                order_id=request_id,
                error=str(exception),
            )
            return
        finally:
            await db.close()

        await self.notifier.send_order_failed(
            order_id=request_id,
            tws_code=error_code,
            reason=error_string,
            is_fatal=False,
        )

    async def _fail_order_in_db(
        self, request_id: int, error_code: int, error_string: str
    ) -> None:
        """Kennzeichnet Order in DB als fehlerhaft und benachrichtigt via Telegram."""
        db = await self.db_factory()
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "UPDATE orders SET status = 'Error' WHERE order_id = ?",
                (request_id,),
            )
            await db.execute("COMMIT")
        except Exception as exception:
            await db.execute("ROLLBACK")
            logger.error(
                "Fehler bei Fatal-Order DB-Update",
                order_id=request_id,
                error=str(exception),
            )
            return
        finally:
            await db.close()

        await self.notifier.send_order_failed(
            order_id=request_id,
            tws_code=error_code,
            reason=error_string,
            is_fatal=True,
        )

    def on_disconnected(self) -> None:
        """Loggt Verbindungsverlust zu TWS und alarmiert den Betreiber."""
        logger.error("Verbindung zur Interactive Brokers TWS wurde getrennt!")
        asyncio.create_task(
            self.notifier.send_system_status(
                title="VERBINDUNGSABBRUCH",
                emoji="🚨",
            )
        )
        asyncio.create_task(self.run_reconnect_callback())
