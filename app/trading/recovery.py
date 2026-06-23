"""
Wiederherstellungsdienste (Recovery Phase) für das Trading-System.

Wird beim Systemstart ausgeführt, um den Zustand offener Orders in der Datenbank
mit der Trader Workstation (TWS) abzugleichen (Reconciliation) und Systemabstürze abzufedern.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
from app.core.db import transaction
from app.core.models import OrderRow, order_row_from_db_row
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
    logger.info("Starting recovery phase")

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

    local_orders = await _load_local_pending_orders(database_connection)
    logger.info("Pending local orders loaded", count=len(local_orders))

    groups_to_requeue = await _reconcile_orders(
        database_connection=database_connection,
        local_orders=local_orders,
        tws_active_orders=tws_active_orders,
        tws_completed_orders=tws_completed_orders,
        interactive_brokers_session=interactive_brokers_session,
        notifier=notifier,
        trigger_settlement_callback=trigger_settlement_callback,
    )

    for trade_group_id in groups_to_requeue:
        logger.info(
            "Re-queueing trade_group_id after recovery",
            trade_group_id=trade_group_id,
        )
        await queue.put(trade_group_id)

    logger.info("Recovery phase completed")


async def fetch_active_orders(interactive_brokers: IB, timeout_seconds: float) -> None:
    """Ruft offene Orders aktiv von TWS ab."""
    try:
        await asyncio.wait_for(
            interactive_brokers.reqOpenOrdersAsync(), timeout=timeout_seconds
        )
    except TimeoutError:
        logger.warning("Timeout waiting for active orders from TWS")


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
        logger.warning("Timeout retrieving completed orders from TWS")


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
            local_orders.append(order_row_from_db_row(row))
    return local_orders


async def _reconcile_orders(
    database_connection: aiosqlite.Connection,
    local_orders: list[OrderRow],
    tws_active_orders: dict[int, object],
    tws_completed_orders: dict[int, object],
    interactive_brokers_session: IB,
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Awaitable[None]],
) -> set[str]:
    """Gleicht die ausstehenden lokalen Orders ab und gibt neu einzureihende Trade-Gruppen zurück."""
    groups_to_requeue: set[str] = set()

    for order in local_orders:
        order_id = order.order_id

        # Negative IDs sind rein lokale, temporäre Datenbank-IDs.
        # Sie wurden nie erfolgreich an TWS übertragen (da sie sonst eine echte positive TWS-ID hätten).
        if order_id < 0:
            logger.info(
                "Recovery: Temporäre lokale Order-ID gefunden. Wird als nie gesendet behandelt.",
                order_id=order_id,
                trade_group_id=order.trade_group_id,
                status=order.status,
            )
            if order.status in ("Created", "PreSubmitted", "Submitted"):
                groups_to_requeue.add(order.trade_group_id)
            continue

        tws_active = tws_active_orders.get(order_id)
        tws_completed = tws_completed_orders.get(order_id)

        if order.status in ("PreSubmitted", "Submitted"):
            await _recover_submitted_order(
                database_connection=database_connection,
                order=order,
                tws_active=tws_active,
                tws_completed=tws_completed,
                local_orders=local_orders,
                tws_active_orders=tws_active_orders,
                interactive_brokers_session=interactive_brokers_session,
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
    local_orders: list[OrderRow],
    tws_active_orders: dict[int, object],
    interactive_brokers_session: IB,
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Awaitable[None]],
) -> None:
    """Gleicht den Zustand einer lokalen Submitted/PreSubmitted Order mit TWS ab."""
    order_id = order.order_id
    if tws_active:
        perm_id = tws_active.order.permId
        tws_status = tws_active.orderStatus.status
        mapped_status = "PreSubmitted" if tws_status == "PreSubmitted" else "Submitted"

        if order.perm_id == perm_id and order.status == mapped_status:
            return

        logger.info(
            "Recovery scenario 1: Order active in TWS. Updating perm_id and status.",
            order_id=order_id,
            perm_id=perm_id,
            mapped_status=mapped_status,
        )
        async with transaction(database_connection):
            await database_connection.execute(
                "UPDATE orders SET perm_id = ?, status = ? WHERE order_id = ?",
                (perm_id, mapped_status, order_id),
            )
        return

    if tws_completed and tws_completed.orderStatus.status == "Filled":
        logger.info(
            "Recovery scenario 2: Order filled in TWS during downtime. Triggering settlement.",
            order_id=order_id,
        )
        async with transaction(database_connection):
            await database_connection.execute(
                "UPDATE orders SET status = 'Filled' WHERE order_id = ?",
                (order_id,),
            )

        await _save_missing_executions(
            database_connection, order, interactive_brokers_session
        )

        await notifier.send_order_filled(
            symbol=order.symbol,
            bracket_role=order.bracket_role,
            action=order.action,
            quantity=Decimal(order.quantity),
            price=order.target_price,
            order_type=order.order_type,
            order_id=order_id,
            strategy_name=order.strategy_name or "",
        )

        asyncio.create_task(
            trigger_settlement_callback(order.trade_group_id, order.account_id)
        )
        return

    # Zusätzliche Prüfung auf indirekt gefüllte Entry-Orders (Szenario 2b)
    if order.bracket_role == "ENTRY":
        has_active_child = any(
            local_order.parent_id == order_id
            and local_order.order_id in tws_active_orders
            for local_order in local_orders
        )
        has_position = _has_live_position(
            interactive_brokers_session, order.account_id, order.symbol
        )

        if has_active_child or has_position:
            logger.info(
                "Recovery scenario 2b: Entry order filled (active position or child order found). Setting to Filled.",
                order_id=order_id,
                symbol=order.symbol,
            )
            async with transaction(database_connection):
                await database_connection.execute(
                    "UPDATE orders SET status = 'Filled' WHERE order_id = ?",
                    (order_id,),
                )

            # Fehlende Ausführungen sichern
            await _save_missing_executions(
                database_connection, order, interactive_brokers_session
            )

            await notifier.send_order_filled(
                symbol=order.symbol,
                bracket_role=order.bracket_role,
                action=order.action,
                quantity=Decimal(order.quantity),
                price=order.target_price,
                order_type=order.order_type,
                order_id=order_id,
                strategy_name=order.strategy_name or "",
            )
            return

    # Recovery Szenario 3: Ghost Order
    logger.warning(
        "Recovery scenario 3: Ghost Order detected (Submitted in DB, not in TWS). Cancelling.",
        order_id=order_id,
    )
    async with transaction(database_connection):
        await database_connection.execute(
            "UPDATE orders SET status = 'Cancelled' WHERE order_id = ?",
            (order_id,),
        )
    await notifier.send_message(
        f"⚠️ <b>GHOST ORDER RECOVERED</b> | <code>{order.symbol}</code> ({order.bracket_role})"
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
        mapped_status = "PreSubmitted" if tws_status == "PreSubmitted" else "Submitted"

        logger.info(
            "Recovery scenario 4: Mid-crash detected (Created in DB, active in TWS).",
            order_id=order_id,
            perm_id=perm_id,
            mapped_status=mapped_status,
        )
        async with transaction(database_connection):
            await database_connection.execute(
                "UPDATE orders SET status = ?, perm_id = ? WHERE order_id = ?",
                (mapped_status, perm_id, order_id),
            )

    else:
        logger.info(
            "Recovery scenario 5: Order never sent. Re-queueing trade group.",
            order_id=order_id,
            trade_group_id=order.trade_group_id,
        )
        groups_to_requeue.add(order.trade_group_id)


def _has_live_position(interactive_brokers: IB, account_id: str, symbol: str) -> bool:
    """Prüft, ob für das Symbol eine offene Position im Depot vorhanden ist."""
    for position in interactive_brokers.positions():
        if (
            position.account == account_id
            and position.contract.symbol == symbol.upper()
            and abs(position.position) > 0
        ):
            return True
    return False


async def _save_missing_executions(
    database_connection: aiosqlite.Connection,
    order: OrderRow,
    interactive_brokers: IB,
) -> None:
    """Sucht nach Fills der Order in TWS und speichert sie in der executions-Tabelle."""
    order_id = order.order_id
    found_fills = [
        fill
        for fill in interactive_brokers.fills()
        if fill.execution.orderId == order_id
    ]

    if found_fills:
        for fill in found_fills:
            exec_id = fill.execution.execId
            price = Decimal(str(fill.execution.price))
            qty = Decimal(str(fill.execution.shares))
            currency = fill.contract.currency
            executed_at = fill.execution.time
            commission = Decimal("0.0")
            if hasattr(fill, "commissionReport") and fill.commissionReport:
                commission = Decimal(str(fill.commissionReport.commission))

            try:
                async with transaction(database_connection):
                    await database_connection.execute(
                        """
                        INSERT OR IGNORE INTO executions (exec_id, order_id, price, qty, commission, currency, executed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            exec_id,
                            order_id,
                            str(price),
                            str(qty),
                            str(commission),
                            currency,
                            executed_at,
                        ),
                    )
            except Exception as exception:
                logger.error(
                    "Error saving late execution detail",
                    exec_id=exec_id,
                    error=str(exception),
                )
    else:
        # Fallback-Ausführung anlegen, um PnL-Berechnung im Settlement abzusichern
        logger.warning(
            "No TWS execution details found for reconstructed order. Using fallback.",
            order_id=order_id,
        )
        fallback_execution_id = f"RECOVERED_{order_id}"
        try:
            async with transaction(database_connection):
                await database_connection.execute(
                    """
                    INSERT OR IGNORE INTO executions (exec_id, order_id, price, qty, commission, currency, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        fallback_execution_id,
                        order_id,
                        str(order.target_price or Decimal("0.0")),
                        str(order.quantity),
                        "0.0",
                        "USD",
                    ),
                )
        except Exception as exception:
            logger.error(
                "Error saving fallback execution",
                order_id=order_id,
                error=str(exception),
            )
