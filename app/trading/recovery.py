import asyncio
from collections.abc import Awaitable, Callable

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
from app.core.models import OrderRow
from app.services.notifier import TelegramNotifier

logger = structlog.get_logger()


async def fetch_completed_orders(ib: IB, timeout_seconds: float) -> None:
    """Ruft abgeschlossene Orders asynchron von TWS ab."""
    try:
        await asyncio.wait_for(
            ib.reqCompletedOrdersAsync(apiOnly=False), timeout=timeout_seconds
        )
    except TimeoutError:
        logger.warning("Timeout beim Abrufen der completed orders von TWS")


async def fetch_active_orders(ib: IB, timeout_seconds: float) -> None:
    """Ruft aktive Orders von TWS ab."""
    try:
        await asyncio.wait_for(ib.reqOpenOrdersAsync(), timeout=timeout_seconds)
    except TimeoutError:
        logger.warning("Timeout beim Warten auf active orders von TWS")


async def run_recovery(
    database_connection: aiosqlite.Connection,
    interactive_brokers_session: IB,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    trigger_settlement_cb: Callable[[str, str], Awaitable[None]],
    config: Config,
) -> None:
    """
    Führt die Recovery-Phase beim Start durch.
    Gleicht lokale Orders mit Status 'Submitted' und 'Created' mit der TWS ab.
    """
    logger.info("Starte Recovery-Phase")

    await fetch_active_orders(interactive_brokers_session, config.tws.request_timeout_s)
    await fetch_completed_orders(
        interactive_brokers_session, config.tws.completed_orders_timeout_s
    )

    tws_active_orders = {
        trade.order.orderId: trade for trade in interactive_brokers_session.openTrades()
    }
    tws_completed_orders = {
        trade.order.orderId: trade
        for trade in interactive_brokers_session.trades()
        if trade not in interactive_brokers_session.openTrades()
    }

    local_orders: list[OrderRow] = []
    async with database_connection.execute(
        """
        SELECT order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
               symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name,
               status, retry_count, transmitted_at
        FROM orders
        WHERE status IN ('Created', 'PreSubmitted', 'Submitted')
        """
    ) as cursor:
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

    logger.info("Offene lokale Orders geladen", count=len(local_orders))

    groups_to_requeue: set[str] = set()

    for order in local_orders:
        order_id = order.order_id
        tws_active = tws_active_orders.get(order_id)
        tws_completed = tws_completed_orders.get(order_id)

        if order.status in ("PreSubmitted", "Submitted"):
            if tws_active:
                perm_id = tws_active.order.permId
                tws_status = tws_active.orderStatus.status
                mapped_status = "PreSubmitted" if tws_status == "PreSubmitted" else "Submitted"
                
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
                    trigger_settlement_cb(order.trade_group_id, order.account_id)
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

        elif order.status == "Created":
            if tws_active:
                perm_id = tws_active.order.permId
                tws_status = tws_active.orderStatus.status
                mapped_status = "PreSubmitted" if tws_status == "PreSubmitted" else "Submitted"
                
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

    for trade_group_id in groups_to_requeue:
        logger.info(
            "Re-queue trade_group_id nach Recovery", trade_group_id=trade_group_id
        )
        await queue.put(trade_group_id)

    logger.info("Recovery-Phase abgeschlossen")
