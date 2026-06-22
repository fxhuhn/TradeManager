"""
Fehlerbehandlung und automatische Wiederholungsversuche (Retries) für Orders.

Verarbeitet transiente Verbindungs- und API-Fehler mittels exponentiellem Backoff
bis zum Erreichen des konfigurierten Retry-Limits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import aiosqlite
import structlog

from app.core.config import Config
from app.core.db import transaction
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
    logger.info("Starting retry handler for order", order_id=order_id)

    db = await db_factory()
    try:
        order_retry_info = await _fetch_order_retry_info(db, order_id)
        if not order_retry_info:
            return

        trade_group_id, retry_count, bracket_role, symbol = order_retry_info
        max_retries = config.app.max_retries

        if retry_count < max_retries:
            await _process_retry_backoff(
                db,
                order_id,
                trade_group_id,
                retry_count,
                queue,
                config,
            )
        else:
            await _process_retry_limit_exceeded(
                db,
                order_id,
                symbol,
                bracket_role,
                max_retries,
                notifier,
            )

    except Exception as exception:
        logger.error(
            "Severe error in retry handler",
            order_id=order_id,
            error=str(exception),
        )
    finally:
        await db.close()


async def _fetch_order_retry_info(
    db: aiosqlite.Connection, order_id: int
) -> tuple[str, int, str, str] | None:
    """Lädt Order-Daten für die Retry-Verarbeitung aus der DB."""
    query = """
        SELECT trade_group_id, retry_count, bracket_role, symbol
        FROM orders
        WHERE order_id = ?
    """
    async with db.execute(query, (order_id,)) as cursor:
        row = await cursor.fetchone()
        if not row:
            logger.warning(
                "Order for retry processing not found in DB",
                order_id=order_id,
            )
            return None
        return (
            row["trade_group_id"],
            row["retry_count"],
            row["bracket_role"],
            row["symbol"],
        )


async def _process_retry_backoff(
    db: aiosqlite.Connection,
    order_id: int,
    trade_group_id: str,
    retry_count: int,
    queue: asyncio.Queue,
    config: Config,
) -> None:
    """Führt das DB-Update aus und wartet den exponentiellen Backoff ab."""
    new_retry_count = retry_count + 1
    backoff_delay = config.app.retry_backoff_base_s * (2 ** (new_retry_count - 1))

    logger.info(
        "Scheduling order retry (backoff)",
        order_id=order_id,
        retry_count=new_retry_count,
        delay_s=backoff_delay,
        trade_group_id=trade_group_id,
    )

    async with transaction(db):
        await db.execute(
            "UPDATE orders SET status = 'Created', retry_count = ? WHERE order_id = ?",
            (new_retry_count, order_id),
        )

    await asyncio.sleep(backoff_delay)

    logger.info(
        "Re-queueing trade_group_id after backoff", trade_group_id=trade_group_id
    )
    await queue.put(trade_group_id)


async def _process_retry_limit_exceeded(
    db: aiosqlite.Connection,
    order_id: int,
    symbol: str,
    bracket_role: str,
    max_retries: int,
    notifier: TelegramNotifier,
) -> None:
    """Markiert Order bei Überschreiten des Retry-Limits als fehlgeschlagen."""
    logger.error(
        "Max retries reached. Order marked as failed.",
        order_id=order_id,
    )

    async with transaction(db):
        await db.execute(
            "UPDATE orders SET status = 'Error' WHERE order_id = ?",
            (order_id,),
        )

    await notifier.send_message(
        f"🚨 <b>RETRY-LIMIT EXCEEDED</b> | <code>{symbol}</code> ({bracket_role})"
    )
