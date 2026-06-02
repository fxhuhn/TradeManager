import asyncio
from collections.abc import Callable, Awaitable
import structlog
import aiosqlite
from app.core.config import Config
from app.services.notifier import TelegramNotifier

logger = structlog.get_logger()


async def handle_retriable_error(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    order_id: int,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Führt das automatisierte Retry-Management mit exponentiellem Backoff durch.
    Gleichfalls wird sichergestellt, dass keine Endlosschleife entsteht (max_retries).
    """
    logger.info("Starte Retry-Handler fuer Order", order_id=order_id)

    db = await db_factory()
    try:
        # 1. Order details aus DB laden
        async with db.execute(
            "SELECT trade_group_id, retry_count, bracket_role, symbol FROM orders WHERE order_id = ?",
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                logger.warning(
                    "Order fuer Retry-Verarbeitung nicht in DB gefunden",
                    order_id=order_id,
                )
                return

            trade_group_id = row["trade_group_id"]
            retry_count = row["retry_count"]
            bracket_role = row["bracket_role"]
            symbol = row["symbol"]

        max_retries = config.app.max_retries

        if retry_count < max_retries:
            new_retry_count = retry_count + 1
            # Exponentieller Backoff basierend auf Config (z.B. 5.0 * 2^x)
            backoff_delay = config.app.retry_backoff_base_s * (
                2 ** (new_retry_count - 1)
            )

            logger.info(
                "Plane Order-Wiederholung (Backoff)",
                order_id=order_id,
                retry_count=new_retry_count,
                delay_s=backoff_delay,
                trade_group_id=trade_group_id,
            )

            # DB-Status auf 'Created' zurücksetzen und retry_count inkrementieren
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE orders SET status = 'Created', retry_count = ? WHERE order_id = ?",
                    (new_retry_count, order_id),
                )
                await db.execute("COMMIT")
            except Exception as exception:
                await db.execute("ROLLBACK")
                logger.error(
                    "Fehler beim Zuruecksetzen des Order-Status in DB",
                    order_id=order_id,
                    error=str(exception),
                )
                raise exception

            # Warte asynchron (blockiert den Event-Loop nicht!)
            await asyncio.sleep(backoff_delay)

            # Trade-Gruppe wieder in Queue einreihen zur erneuten Übermittlung
            logger.info(
                "Re-queue trade_group_id nach Backoff", trade_group_id=trade_group_id
            )
            await queue.put(trade_group_id)

        else:
            # Maximale Retries erreicht -> Setze auf Error und sende Alarm
            logger.error(
                "Maximale Anzahl an Retries erreicht. Order markiert als fehlgeschlagen.",
                order_id=order_id,
            )

            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE orders SET status = 'Error' WHERE order_id = ?", (order_id,)
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

            await notifier.send_message(
                f"🚨 RETRY-LIMIT ERREICHT: Order {order_id} ({symbol} {bracket_role}) "
                f"schlug auch nach {max_retries} Versuchen fehl. Status auf 'Error' gesetzt."
            )

    except Exception as exception:
        logger.error(
            "Schwerer Fehler im Retry-Handler", order_id=order_id, error=str(exception)
        )
    finally:
        await db.close()
