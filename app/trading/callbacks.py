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


# We pass callbacks to avoid circular imports.
class TwsCallbacksManager:
    """
    Registriert und verwaltet alle asynchronen TWS-Callbacks (Events)
    für die Abwicklung von Order-Status-Updates, Fills, Provisionen und Fehlern.
    """

    def __init__(
        self,
        db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
        ib: IB,
        notifier: TelegramNotifier,
        config: Config,
        trigger_settlement_callback: Callable[[str, str], Awaitable[None]],
        handle_retriable_error_callback: Callable[[int], Awaitable[None]],
        run_recovery_callback: Callable[[], Awaitable[None]],
        run_reconnect_callback: Callable[[], Awaitable[None]],
    ):
        self.db_factory = db_factory
        self.ib = ib
        self.notifier = notifier
        self.config = config
        self.trigger_settlement_callback = trigger_settlement_callback
        self.handle_retriable_error_callback = handle_retriable_error_callback
        self.run_recovery_callback = run_recovery_callback
        self.run_reconnect_callback = run_reconnect_callback

    def register_all(self) -> None:
        """Verknüpft die Event-Methoden mit den ib_async Signalen."""
        self.ib.orderStatusEvent.connect(self.on_order_status)
        self.ib.execDetailsEvent.connect(self.on_exec_details)
        self.ib.commissionReportEvent.connect(self.on_commission_report)
        self.ib.errorEvent.connect(self.on_error)
        self.ib.disconnectedEvent.connect(self.on_disconnected)
        logger.info("Alle asynchronen TWS-Callbacks erfolgreich registriert")

    async def _update_order_status_db(
        self, order_id: int, status: str, perm_id: int
    ) -> None:
        """Schreibt das Status-Update atomar in die Datenbank."""
        db = await self.db_factory()
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "UPDATE orders SET status = ?, perm_id = ? WHERE order_id = ?",
                (status, perm_id, order_id),
            )
            await db.execute("COMMIT")
            logger.debug(
                "Order-Zustand in DB aktualisiert", order_id=order_id, status=status
            )
        except Exception as e:
            await db.execute("ROLLBACK")
            logger.error(
                "Fehler beim DB-Update des Order-Status",
                order_id=order_id,
                error=str(e),
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

        # Mapping von TWS-Status auf unser Schema
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

        # DB asynchron updaten, um den Callback nicht zu blockieren
        async def handle_status_change():
            await self._update_order_status_db(order_id, mapped_status, perm_id)

            if mapped_status == "Filled":
                # Prüfen, ob dies eine Exit-Order war (SL, TP, EXIT, MOC)
                db = await self.db_factory()
                try:
                    async with db.execute(
                        "SELECT trade_group_id, account_id, bracket_role FROM orders WHERE order_id = ?",
                        (order_id,),
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            trade_group_id = row["trade_group_id"]
                            account_id = row["account_id"]
                            bracket_role = row["bracket_role"]

                            if bracket_role in ("SL", "TP", "EXIT"):
                                logger.info(
                                    "Exit-Order gefüllt! Settlement wird ausgelöst.",
                                    order_id=order_id,
                                    trade_group_id=trade_group_id,
                                )
                                # Settlement als asynchrone Hintergrund-Task ausführen (behebt blockierende Heartbeats)
                                asyncio.create_task(
                                    self.trigger_settlement_callback(
                                        trade_group_id, account_id
                                    )
                                )
                except Exception as e:
                    logger.error(
                        "Fehler bei Exit-Pruefung im Status-Callback", error=str(e)
                    )
                finally:
                    await db.close()

        asyncio.create_task(handle_status_change())

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

        async def save_execution():
            db = await self.db_factory()
            # Prämisse: INSERT OR IGNORE für absolute Idempotenz bei Reconnects
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
                        float(price),
                        float(qty),
                        currency,
                        executed_at,
                    ),
                )
                await db.execute("COMMIT")
                logger.debug(
                    "Teilausfuehrung idempotent in DB verbucht", exec_id=exec_id
                )
            except Exception as e:
                await db.execute("ROLLBACK")
                logger.error(
                    "Fehler beim Speichern der Teilausfuehrung",
                    exec_id=exec_id,
                    error=str(e),
                )
            finally:
                await db.close()

        asyncio.create_task(save_execution())

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

        async def update_commission():
            db = await self.db_factory()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE executions SET commission = ?, currency = ? WHERE exec_id = ?",
                    (float(commission), currency, exec_id),
                )
                await db.execute("COMMIT")
                logger.debug(
                    "Kommission fuer Teilausfuehrung aktualisiert", exec_id=exec_id
                )
            except Exception as e:
                await db.execute("ROLLBACK")
                logger.error(
                    "Fehler beim Aktualisieren der Kommission",
                    exec_id=exec_id,
                    error=str(e),
                )
            finally:
                await db.close()

        asyncio.create_task(update_commission())

    def on_error(self, reqId: int, errorCode: int, errorString: str) -> None:
        """
        Klassifiziert alle von TWS gemeldeten Error-Codes und reagiert
        strukturiert (Loggen, Retries, Warnungen, Telegram-Alerts).
        """
        # reqId = -1 kennzeichnet allgemeine Systemmeldungen
        if reqId == -1 and errorCode in (2104, 2106, 2158, 2100):
            # Reine INFO-Codes ignorieren
            logger.debug("TWS System-Info empfangen", code=errorCode, msg=errorString)
            return

        error_class = classify_error_code(errorCode)
        logger.warning(
            "TWS-Fehlermeldung empfangen",
            reqId=reqId,
            code=errorCode,
            msg=errorString,
            klassifizierung=error_class.name,
        )

        async def handle_error():
            db = await self.db_factory()
            try:
                if error_class == ErrorClass.INFO:
                    pass

                elif error_class == ErrorClass.RECONNECT:
                    # TWS hat Verbindung verloren, trigger Recovery erneut
                    logger.info("Reconnect signalisiert. Trigger Recovery-Lauf.")
                    asyncio.create_task(self.run_recovery_callback())

                elif error_class == ErrorClass.RETRIABLE:
                    # Temporärer API-Fehler (z.B. Timeout, Rate-Limit). Starte Retry-Logik.
                    asyncio.create_task(self.handle_retriable_error_callback(reqId))

                elif error_class == ErrorClass.CANCEL:
                    # Manuelle oder automatische Stornierung
                    await db.execute("BEGIN IMMEDIATE")
                    try:
                        await db.execute(
                            "UPDATE orders SET status = 'Cancelled' WHERE order_id = ?",
                            (reqId,),
                        )
                        await db.execute("COMMIT")
                    except Exception:
                        await db.execute("ROLLBACK")
                        raise
                    await self.notifier.send_message(
                        f"🚫 ORDER CANCELED: Order {reqId} wurde storniert (TWS-Code {errorCode}): {errorString}"
                    )

                elif error_class == ErrorClass.FATAL:
                    # Schwerer Systemfehler. Setze Order auf Error und sende Telegram-Alarm.
                    await db.execute("BEGIN IMMEDIATE")
                    try:
                        await db.execute(
                            "UPDATE orders SET status = 'Error' WHERE order_id = ?",
                            (reqId,),
                        )
                        await db.execute("COMMIT")
                    except Exception:
                        await db.execute("ROLLBACK")
                        raise
                    await self.notifier.send_message(
                        f"🚨 SYSTEM-FEHLER (FATAL): Order {reqId} schlug fehl (TWS-Code {errorCode}): {errorString}"
                    )

            except Exception as e:
                logger.error(
                    "Fehler bei der API-Error-Verarbeitung", reqId=reqId, error=str(e)
                )
            finally:
                await db.close()

        asyncio.create_task(handle_error())

    def on_disconnected(self) -> None:
        """Loggt Verbindungsverlust zu TWS und alarmiert den Betreiber."""
        logger.error("Verbindung zur Interactive Brokers TWS wurde getrennt!")
        asyncio.create_task(
            self.notifier.send_message(
                "🚨 VERBINDUNGSABBRUCH: Die TCP-Verbindung zur Interactive Brokers TWS ist abgebrochen! Es wird versucht, die Verbindung wiederherzustellen."
            )
        )
        asyncio.create_task(self.run_reconnect_callback())
