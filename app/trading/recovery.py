"""
Wiederherstellungsdienste (Recovery Phase) für das Trading-System.

Wird beim Systemstart ausgeführt, um den Zustand offener Orders in der Datenbank
mit der Trader Workstation (TWS) abzugleichen (Reconciliation) und Systemabstürze abzufedern.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
from app.core.models import OrderRow
from app.services.notifier import TelegramNotifier

logger = structlog.get_logger()


async def run_recovery(
    database_connection: aiosqlite.Connection,
    interactive_brokers_session: IB,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Awaitable[None]],
    config: Config,
) -> None:
    """
    Führt die Recovery-Phase beim Start der Anwendung durch.

    Gleicht ausstehende lokale Orders mit der TWS ab und veranlasst bei Bedarf
    ein Re-queue oder Settlement.
    """
    logger.info("Starte Recovery-Phase")

    await fetch_active_orders(
        interactive_brokers_session, config.tws.request_timeout_s
    )
    await fetch_completed_orders(
        interactive_brokers_session, config.tws.completed_orders_timeout_s
    )

    tws_active_orders = {
        trade.order.orderId: trade
        for trade in interactive_brokers_session.openTrades()
    }
    tws_completed_orders = {
        trade.order.orderId: trade
        for trade in interactive_brokers_session.trades()
        if trade not in interactive_brokers_session.openTrades()
    }

    local_orders = await _load_local_pending_orders(database_connection)
    logger.info("Offene lokale Orders geladen", count=len(local_orders))

    groups_to_requeue = await _reconcile_orders(
        database_connection=database_connection,
        local_orders=local_orders,
        tws_active_orders=tws_active_orders,
        tws_completed_orders=tws_completed_orders,
        notifier=notifier,
        trigger_settlement_callback=trigger_settlement_callback,
    )

    for trade_group_id in groups_to_requeue:
        logger.info(
            "Re-queue trade_group_id nach Recovery",
            trade_group_id=trade_group_id,
        )
        await queue.put(trade_group_id)

    logger.info("Recovery-Phase abgeschlossen")


async def fetch_active_orders(
    interactive_brokers: IB, timeout_seconds: float
) -> None:
    """Ruft offene Orders aktiv von TWS ab."""
    try:
        await asyncio.wait_for(
            interactive_brokers.reqOpenOrdersAsync(), timeout=timeout_seconds
        )
    except TimeoutError:
        logger.warning("Timeout beim Warten auf active orders von TWS")


async def fetch_completed_orders(
    interactive_brokers: IB, timeout_seconds: float
) -> None:
    """Ruft abgeschlossene Orders asynchron von TWS ab."""
    try:
        await asyncio.wait_for(
            interactive_brokers.reqCompletedOrdersAsync(apiOnly=False),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning("Timeout beim Abrufen der completed orders von TWS")


async def _load_local_pending_orders(
    database_connection: aiosqlite.Connection,
) -> list[OrderRow]:
    """Lädt alle ausstehenden Orders (Created, PreSubmitted, Submitted) aus der DB."""
    local_orders: list[OrderRow] = []
    query = """
        SELECT order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
               symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name,
               status, retry_count, transmitted_at
        FROM orders
        WHERE status IN ('Created', 'PreSubmitted', 'Submitted')
    """
    async with database_connection.execute(query) as cursor:
        async for row in cursor:
            local_orders.append(
                OrderRow(
                    order_id=row["order_id"],
                    perm_id=row["perm_id"],
                    parent_id=row["parent_id"],
                    trade_group_id=row["trade_group_id"],
                    account_id=row["account_id"],
                    bracket_role=row["bracket_role"],
                    symbol=row["symbol"],
                    sec_type=row["sec_type"],
                    exchange=row["exchange"],
                    action=row["action"],
                    quantity=row["quantity"],
                    order_type=row["order_type"],
                    target_price=row["target_price"],
                    tif=row["tif"],
                    strategy_name=row["strategy_name"],
                    status=row["status"],
                    retry_count=row["retry_count"],
                    transmitted_at=row["transmitted_at"],
                )
            )
    return local_orders


async def _reconcile_orders(
    database_connection: aiosqlite.Connection,
    local_orders: list[OrderRow],
    tws_active_orders: dict[int, object],
    tws_completed_orders: dict[int, object],
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Awaitable[None]],
) -> set[str]:
    """Gleicht die ausstehenden lokalen Orders ab und gibt neu einzureihende Trade-Gruppen zurück."""
    groups_to_requeue: set[str] = set()

    for order in local_orders:
        order_id = order.order_id
        tws_active = tws_active_orders.get(order_id)
        tws_completed = tws_completed_orders.get(order_id)

        if order.status in ("PreSubmitted", "Submitted"):
            await _recover_submitted_order(
                database_connection=database_connection,
                order=order,
                tws_active=tws_active,
                tws_completed=tws_completed,
                notifier=notifier,
                trigger_settlement_callback=trigger_settlement_callback,
            )
        elif order.status == "Created":
            await _recover_created_order(
                database_connection=database_connection,
                order=order,
                tws_active=tws_active,
                groups_to_requeue=groups_to_requeue,
            )
    return groups_to_requeue


async def _recover_submitted_order(
    database_connection: aiosqlite.Connection,
    order: OrderRow,
    tws_active: object | None,
    tws_completed: object | None,
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Awaitable[None]],
) -> None:
    """Gleicht den Zustand einer lokalen Submitted/PreSubmitted Order mit TWS ab."""
    order_id = order.order_id
    if tws_active:
        perm_id = tws_active.order.permId
        tws_status = tws_active.orderStatus.status
        mapped_status = (
            "PreSubmitted" if tws_status == "PreSubmitted" else "Submitted"
        )

        if order.perm_id == perm_id and order.status == mapped_status:
            return

        logger.info(
            f"Recovery Szenario 1: Order aktiv in TWS. Aktualisiere perm_id und Status auf {mapped_status}.",
            order_id=order_id,
            perm_id=perm_id,
        )
        await database_connection.execute("BEGIN IMMEDIATE")
        try:
            await database_connection.execute(
                "UPDATE orders SET perm_id = ?, status = ? WHERE order_id = ?",
                (perm_id, mapped_status, order_id),
            )
            await database_connection.execute("COMMIT")
        except Exception:
            await database_connection.execute("ROLLBACK")
            raise

    elif tws_completed and tws_completed.orderStatus.status == "Filled":
        logger.info(
            "Recovery Szenario 2: Order in TWS gefüllt während Downtime. Stoße Settlement an.",
            order_id=order_id,
        )
        asyncio.create_task(
            trigger_settlement_callback(order.trade_group_id, order.account_id)
        )

    else:
        logger.warning(
            "Recovery Szenario 3: Ghost Order erkannt (Submitted in DB, nicht in TWS). Abbrechen.",
            order_id=order_id,
        )
        await database_connection.execute("BEGIN IMMEDIATE")
        try:
            await database_connection.execute(
                "UPDATE orders SET status = 'Cancelled' WHERE order_id = ?",
                (order_id,),
            )
            await database_connection.execute("COMMIT")
        except Exception:
            await database_connection.execute("ROLLBACK")
            raise
        await notifier.send_message(
            f"⚠️ GHOST ORDER RECOVERED: Order {order_id} ({order.symbol} {order.bracket_role}) "
            f"stand auf 'Submitted', existiert aber nicht in TWS. Status wurde auf 'Cancelled' gesetzt."
        )


async def _recover_created_order(
    database_connection: aiosqlite.Connection,
    order: OrderRow,
    tws_active: object | None,
    groups_to_requeue: set[str],
) -> None:
    """Gleicht den Zustand einer lokalen Created Order mit TWS ab."""
    order_id = order.order_id
    if tws_active:
        perm_id = tws_active.order.permId
        tws_status = tws_active.orderStatus.status
        mapped_status = (
            "PreSubmitted" if tws_status == "PreSubmitted" else "Submitted"
        )

        logger.info(
            f"Recovery Szenario 4: Mid-Crash erkannt (Created in DB, aktiv in TWS). Setze auf {mapped_status}.",
            order_id=order_id,
            perm_id=perm_id,
        )
        await database_connection.execute("BEGIN IMMEDIATE")
        try:
            await database_connection.execute(
                "UPDATE orders SET status = ?, perm_id = ? WHERE order_id = ?",
                (mapped_status, perm_id, order_id),
            )
            await database_connection.execute("COMMIT")
        except Exception:
            await database_connection.execute("ROLLBACK")
            raise

    else:
        logger.info(
            "Recovery Szenario 5: Order nie gesendet. Trade-Gruppe wird neu eingereiht.",
            order_id=order_id,
            trade_group_id=order.trade_group_id,
        )
        groups_to_requeue.add(order.trade_group_id)
