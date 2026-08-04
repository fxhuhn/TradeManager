"""
Wiederherstellungsdienste (Recovery Phase) für das Trading-System.

Wird beim Systemstart oder nach Verbindungsabbrüchen ausgeführt, um den Zustand offener Orders
in der Datenbank mit den TWS-Orders abzugleichen (Reconciliation) und Offline-Fills zu verarbeiten.

Siehe Datenfluss- und Architekturzusammenhang in app.core.models.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import aiosqlite
import structlog
from ib_async import IB, Trade

from app.core.config import Config
from app.core.db import transaction
from app.core.models import OrderRow, order_row_from_db_row, parse_positive_decimal
from app.services.notifier import TelegramNotifier
from app.trading.order_builder import normalize_symbol

logger = structlog.get_logger()


async def run_recovery(
    database_connection: aiosqlite.Connection,
    interactive_brokers_session: IB,
    queue: asyncio.Queue[str],
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Coroutine[Any, Any, None]],
    config: Config,
) -> None:
    """
    Führt die Recovery-Phase beim Start der Anwendung durch.

    Gleicht ausstehende lokale Orders mit der TWS ab und veranlasst bei Bedarf
    ein Re-queue oder Settlement.
    """
    logger.debug("Starting recovery phase")

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
    logger.debug("Pending local orders loaded", count=len(local_orders))

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

    await reconcile_broker_positions(
        database_connection=database_connection,
        interactive_brokers_session=interactive_brokers_session,
        notifier=notifier,
    )

    logger.debug("Recovery phase completed")


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
    tws_active_orders: dict[int, Trade],
    tws_completed_orders: dict[int, Trade],
    interactive_brokers_session: IB,
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Coroutine[Any, Any, None]],
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
                tws_completed_orders=tws_completed_orders,
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
    tws_active: Trade | None,
    tws_completed: Trade | None,
    local_orders: list[OrderRow],
    tws_active_orders: dict[int, Trade],
    tws_completed_orders: dict[int, Trade],
    interactive_brokers_session: IB,
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Coroutine[Any, Any, None]],
) -> None:
    """Gleicht den Zustand einer lokalen Submitted/PreSubmitted Order mit TWS ab."""
    if tws_active:
        await _sync_active_order_status(database_connection, order, tws_active)
        return

    if tws_completed and tws_completed.orderStatus.status == "Filled":
        await _handle_filled_during_downtime(
            database_connection,
            order,
            tws_completed,
            interactive_brokers_session,
            notifier,
            trigger_settlement_callback,
        )
        return

    if order.bracket_role == "ENTRY":
        was_recovered = await _try_recover_indirect_entry_fill(
            database_connection,
            order,
            local_orders,
            tws_active_orders,
            tws_completed_orders,
            interactive_brokers_session,
            notifier,
        )
        if was_recovered:
            return

    await _cancel_ghost_order(database_connection, order, notifier)


async def _sync_active_order_status(
    database_connection: aiosqlite.Connection,
    order: OrderRow,
    tws_active: Trade,
) -> None:
    """Recovery Scenario 1: Updates perm_id and status for orders still active in TWS."""
    order_id = order.order_id
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


async def _handle_filled_during_downtime(
    database_connection: aiosqlite.Connection,
    order: OrderRow,
    tws_completed: Trade,
    interactive_brokers_session: IB,
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Coroutine[Any, Any, None]],
) -> None:
    """Recovery Scenario 2: Process orders filled in TWS while the application was down."""
    order_id = order.order_id
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

    avg_fill_price = (
        tws_completed.orderStatus.avgFillPrice
        if tws_completed and tws_completed.orderStatus
        else None
    )
    price_decimal = parse_positive_decimal(avg_fill_price) or parse_positive_decimal(
        order.target_price
    )
    limit_price_decimal = parse_positive_decimal(order.target_price)

    await notifier.send_order_filled(
        symbol=order.symbol,
        bracket_role=order.bracket_role,
        action=order.action,
        quantity=Decimal(order.quantity),
        execution_price=price_decimal,
        order_type=order.order_type,
        order_id=order_id,
        strategy_name=order.strategy_name or "",
        limit_price=limit_price_decimal,
    )

    asyncio.create_task(
        trigger_settlement_callback(order.trade_group_id, order.account_id)
    )


async def _try_recover_indirect_entry_fill(
    database_connection: aiosqlite.Connection,
    order: OrderRow,
    local_orders: list[OrderRow],
    tws_active_orders: dict[int, Trade],
    tws_completed_orders: dict[int, Trade],
    interactive_brokers_session: IB,
    notifier: TelegramNotifier,
) -> bool:
    """Recovery Scenario 2b: Recovers entry orders that filled indirectly (active child or portfolio position found)."""
    order_id = order.order_id
    has_active_child = any(
        local_order.parent_id == order_id and local_order.order_id in tws_active_orders
        for local_order in local_orders
    )
    has_position = _has_live_position(
        interactive_brokers_session, order.account_id, order.symbol
    )

    if not (has_active_child or has_position):
        return False

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

    await _save_missing_executions(
        database_connection, order, interactive_brokers_session
    )

    entry_trade = tws_completed_orders.get(order_id)
    avg_fill_price = (
        entry_trade.orderStatus.avgFillPrice
        if entry_trade and entry_trade.orderStatus
        else None
    )
    price_decimal = parse_positive_decimal(avg_fill_price) or parse_positive_decimal(
        order.target_price
    )
    limit_price_decimal = parse_positive_decimal(order.target_price)

    await notifier.send_order_filled(
        symbol=order.symbol,
        bracket_role=order.bracket_role,
        action=order.action,
        quantity=Decimal(order.quantity),
        execution_price=price_decimal,
        order_type=order.order_type,
        order_id=order_id,
        strategy_name=order.strategy_name or "",
        limit_price=limit_price_decimal,
    )
    return True


async def _cancel_ghost_order(
    database_connection: aiosqlite.Connection,
    order: OrderRow,
    notifier: TelegramNotifier,
) -> None:
    """Recovery Scenario 3: Cancels ghost orders (Submitted in DB, but nowhere in TWS)."""
    order_id = order.order_id
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
    tws_active: Trade | None,
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
    target_symbol = normalize_symbol(symbol)
    for position in interactive_brokers.positions():
        if (
            position.account == account_id
            and normalize_symbol(position.contract.symbol) == target_symbol
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


async def reconcile_broker_positions(
    database_connection: aiosqlite.Connection,
    interactive_brokers_session: IB,
    notifier: TelegramNotifier,
) -> None:
    """
    Gleicht Live-Positionen vom IBKR Broker mit der lokalen SQLite-Datenbank ab.

    Falls Positionen im Broker existieren, die nicht oder nur teilweise in der DB erfasst sind,
    werden synthetische Entry-Orders und Executions mit strategy_name = None (NULL) angelegt,
    damit die Bestände 100% synchron sind und spätere Settlement-Verkäufe vorbereitet sind.
    """
    positions = interactive_brokers_session.positions()
    if not positions:
        return

    # Netto-Ausführungen in der DB berechnen (SUM(BUY) - SUM(SELL))
    db_positions: dict[str, Decimal] = {}
    query = """
        SELECT o.symbol,
               SUM(CASE WHEN o.action = 'BUY' THEN CAST(e.qty AS REAL) ELSE -CAST(e.qty AS REAL) END) as net_qty
        FROM executions e
        JOIN orders o ON e.order_id = o.order_id
        GROUP BY o.symbol
    """
    async with database_connection.execute(query) as cursor:
        async for row in cursor:
            symbol = normalize_symbol(str(row[0]))
            net_qty = Decimal(str(row[1])) if row[1] is not None else Decimal("0.0")
            db_positions[symbol] = net_qty

    for pos in positions:
        broker_qty = Decimal(str(pos.position))
        if broker_qty <= Decimal("0.0"):
            continue

        symbol = normalize_symbol(pos.contract.symbol)
        account_id = pos.account
        avg_cost = Decimal(str(round(float(pos.avgCost), 4)))

        db_net_qty = db_positions.get(symbol, Decimal("0.0"))
        delta_qty = broker_qty - db_net_qty

        if delta_qty > Decimal("0.0"):
            logger.info(
                "Unassigned broker position discrepancy detected. Recovering to DB.",
                symbol=symbol,
                broker_qty=float(broker_qty),
                db_net_qty=float(db_net_qty),
                delta_qty=float(delta_qty),
                avg_cost=float(avg_cost),
            )

            # Nächste freie negative Sequenz-ID ermitteln
            temp_id = await _get_next_recovery_temp_id(database_connection)
            trade_group_id = f"UNASSIGNED_{symbol}_{account_id}"
            now_iso = datetime.now(UTC).isoformat()

            async with transaction(database_connection):
                await database_connection.execute(
                    """
                    INSERT INTO orders (
                        order_id, perm_id, parent_id, trade_group_id, account_id,
                        bracket_role, symbol, sec_type, exchange, action, quantity,
                        order_type, target_price, tif, strategy_name, status, transmitted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        temp_id,
                        None,
                        None,
                        trade_group_id,
                        account_id,
                        "ENTRY",
                        symbol,
                        "STK",
                        "SMART",
                        "BUY",
                        int(delta_qty),
                        "MKT",
                        str(avg_cost),
                        "GTC",
                        None,
                        "Filled",
                        now_iso,
                    ),
                )

                exec_id = f"RECOVERED_POS_{symbol}_{abs(temp_id)}"
                currency_str = (
                    pos.contract.currency
                    if hasattr(pos.contract, "currency") and pos.contract.currency
                    else "USD"
                )
                await database_connection.execute(
                    """
                    INSERT OR IGNORE INTO executions (exec_id, order_id, price, qty, commission, currency, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        exec_id,
                        temp_id,
                        str(avg_cost),
                        str(delta_qty),
                        "0.0",
                        currency_str,
                        now_iso,
                    ),
                )

            await notifier.send_unassigned_position_recovered(
                symbol=symbol,
                quantity=delta_qty,
                avg_cost=avg_cost,
                account_id=account_id,
            )


async def _get_next_recovery_temp_id(database_connection: aiosqlite.Connection) -> int:
    """Ermittelt die nächste freie negative Sequenz-ID für synthetische Recovery-Orders."""
    query = "SELECT MIN(order_id) FROM orders WHERE order_id < 0"
    async with database_connection.execute(query) as cursor:
        row = await cursor.fetchone()
        if row and row[0] is not None and row[0] < 0:
            return int(row[0]) - 1
        return -1
