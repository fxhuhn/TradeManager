"""
Ausführungsworker (Execution Worker) für TWS-Auftragsplatzierungen.

Verarbeitet Trade-Gruppen asynchron aus einer Queue und sendet
die entsprechenden ENTRY und Child-Orders (SL, TP, EXIT) an die TWS.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Final

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
from app.core.models import OrderRow, order_row_from_db_row
from app.services.notifier import TelegramNotifier
from app.trading.order_builder import build_order, make_stock_contract

logger = structlog.get_logger()

# Globales Lock zur Sicherung der Atomarität von getReqId() + DB-Write
ORDER_ID_LOCK = asyncio.Lock()


async def execution_worker(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    interactive_brokers: IB,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Asynchroner Execution Worker (Hintergrunddienst).

    Konsumiert permanent Trade-Gruppen-IDs aus der Queue und stößt
    deren Platzierung an.
    """
    logger.info("Starte Execution Worker Hintergrunddienst")

    while True:
        try:
            trade_group_id = await queue.get()

            db = await db_factory()
            try:
                await process_trade_group(
                    db, interactive_brokers, trade_group_id, notifier, config
                )
            finally:
                await db.close()

            queue.task_done()

        except asyncio.CancelledError:
            logger.info("Execution Worker wurde abgebrochen.")
            raise
        except Exception as exception:
            logger.error("Fehler im Execution Worker Loop", error=str(exception))
            await asyncio.sleep(1.0)


async def process_trade_group(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    trade_group_id: str,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Verarbeitet eine einzelne Trade-Gruppe aus der Queue.

    Übermittelt die ENTRY-Order sowie die zugehörigen Child-Orders (SL, TP, EXIT)
    an die TWS.
    """
    logger.info("Verarbeite Trade-Gruppe aus Queue", trade_group_id=trade_group_id)

    orders = await _load_trade_group_orders(db, trade_group_id)
    if not orders:
        logger.warning(
            "Keine Orders fuer Trade-Gruppe in DB gefunden",
            trade_group_id=trade_group_id,
        )
        return

    entry_order = next(
        (order for order in orders if order.bracket_role == "ENTRY"), None
    )
    child_orders = [order for order in orders if order.bracket_role != "ENTRY"]

    if not entry_order:
        logger.error(
            "Keine ENTRY-Order in Gruppe vorhanden", trade_group_id=trade_group_id
        )
        return

    is_post_fill: Final[bool] = entry_order.status == "Filled"
    placed_orders: list[OrderRow] = []

    if entry_order.status == "Created":
        logger.info(
            "Normaler Einstieg: Verarbeite ENTRY-Order",
            trade_group_id=trade_group_id,
        )
        await _process_entry_order(
            db,
            interactive_brokers,
            entry_order,
            child_orders,
            notifier,
            config,
            placed_orders,
        )

    if entry_order.status == "Error":
        logger.warning(
            "ENTRY-Order fehlgeschlagen. Verarbeite keine Child-Orders.",
            trade_group_id=trade_group_id,
        )
        return

    await _process_child_orders(
        db,
        interactive_brokers,
        entry_order,
        child_orders,
        is_post_fill,
        notifier,
        config,
        placed_orders,
    )

    if placed_orders:
        order_dicts = [
            {
                "role": o.bracket_role,
                "action": o.action,
                "quantity": o.quantity,
                "price": o.target_price,
                "order_type": o.order_type,
            }
            for o in placed_orders
        ]

        # Sortiere: ENTRY zuerst, dann TP, dann SL
        order_dicts.sort(key=lambda x: {"ENTRY": 0, "TP": 1, "SL": 2}.get(x["role"], 3))

        await notifier.send_bracket_order_submitted(
            symbol=entry_order.symbol,
            trade_group_id=trade_group_id,
            strategy_name=entry_order.strategy_name,
            orders=order_dicts,
        )


async def _load_trade_group_orders(
    db: aiosqlite.Connection, trade_group_id: str
) -> list[OrderRow]:
    """Lädt alle Orders einer Trade-Gruppe aus der Datenbank."""
    orders: list[OrderRow] = []
    query = """
        SELECT order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
               symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name,
               status, retry_count, transmitted_at
        FROM orders
        WHERE trade_group_id = ?
    """
    async with db.execute(query, (trade_group_id,)) as cursor:
        async for row in cursor:
            orders.append(order_row_from_db_row(row))
    return orders


async def _process_entry_order(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    entry_order: OrderRow,
    child_orders: list[OrderRow],
    notifier: TelegramNotifier,
    config: Config,
    placed_orders: list[OrderRow],
) -> None:
    """Weist dem Entry eine TWS Order-ID zu, aktualisiert die DB und übermittelt an TWS."""
    async with ORDER_ID_LOCK:
        tws_order_id = await _get_next_non_colliding_order_id(db, interactive_brokers)
        await _assign_order_id_in_db(db, entry_order.order_id, tws_order_id)

    entry_order.order_id = tws_order_id
    entry_order.status = "Submitted"

    contract = make_stock_contract(entry_order.symbol)
    ib_entry_order = build_order(entry_order)

    has_unsent_children = any(child.status == "Created" for child in child_orders)
    ib_entry_order.transmit = not has_unsent_children

    logger.info(
        "Sende ENTRY-Order an TWS", order_id=tws_order_id, symbol=entry_order.symbol
    )
    success = await _place_and_verify_order(
        db,
        interactive_brokers,
        contract,
        ib_entry_order,
        entry_order,
        tws_order_id,
        notifier,
    )
    if not success:
        return
    placed_orders.append(entry_order)

    await asyncio.sleep(config.app.order_rate_limit_s)


async def _assign_order_id_in_db(
    db: aiosqlite.Connection, original_order_id: int, tws_order_id: int
) -> None:
    """Updates order status and order_id in database."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "UPDATE orders SET order_id = ?, status = 'Submitted', transmitted_at = datetime('now') WHERE order_id = ?",
            (tws_order_id, original_order_id),
        )
        await db.execute("COMMIT")
    except Exception as exception:
        await db.execute("ROLLBACK")
        logger.error(
            "Fehler beim Zuweisen der order_id in DB",
            original_id=original_order_id,
            tws_id=tws_order_id,
            error=str(exception),
        )
        raise exception


async def _process_child_orders(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    entry_order: OrderRow,
    child_orders: list[OrderRow],
    is_post_fill: bool,
    notifier: TelegramNotifier,
    config: Config,
    placed_orders: list[OrderRow],
) -> None:
    """Verarbeitet die verbleibenden untergeordneten Orders (SL, TP, EXIT)."""
    created_children = [child for child in child_orders if child.status == "Created"]
    for iteration_index, child in enumerate(created_children):
        is_last = iteration_index == len(created_children) - 1
        success = await _place_single_child_order(
            db,
            interactive_brokers,
            child,
            entry_order,
            is_post_fill,
            is_last,
            notifier,
            config,
        )
        if success:
            placed_orders.append(child)


async def _place_single_child_order(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    child: OrderRow,
    entry_order: OrderRow,
    is_post_fill: bool,
    is_last: bool,
    notifier: TelegramNotifier,
    config: Config,
) -> bool:
    """Bereitet eine einzelne untergeordnete Order vor und übermittelt sie an TWS."""
    logger.info(
        "Verarbeite Child-Order",
        bracket_role=child.bracket_role,
        trade_group_id=child.trade_group_id,
    )

    if is_post_fill and child.bracket_role in ("SL", "TP", "EXIT"):
        should_continue = await _adjust_exit_order_quantity(
            db, interactive_brokers, child, notifier
        )
        if not should_continue:
            return False

    async with ORDER_ID_LOCK:
        tws_order_id = await _get_next_non_colliding_order_id(db, interactive_brokers)
        await _assign_order_id_in_db(db, child.order_id, tws_order_id)

    child.order_id = tws_order_id
    child.status = "Submitted"

    contract = make_stock_contract(child.symbol)
    ib_child_order = build_order(child)

    if not is_post_fill:
        ib_child_order.parentId = entry_order.order_id

    # Post-Fill-Exits haben keinen parentId (kein Bracket). Ohne parentId löst
    # transmit=True der letzten Order NICHT die Übermittlung der vorherigen aus.
    # Jede Post-Fill-Order muss daher einzeln übermittelt werden (transmit=True).
    # Die gegenseitige Stornierung erfolgt ausschließlich über die OCA-Gruppe.
    if is_post_fill:
        ib_child_order.transmit = True
    else:
        ib_child_order.transmit = is_last

    logger.info(
        "Sende Child-Order an TWS",
        order_id=tws_order_id,
        role=child.bracket_role,
    )
    success = await _place_and_verify_order(
        db, interactive_brokers, contract, ib_child_order, child, tws_order_id, notifier
    )

    await asyncio.sleep(config.app.order_rate_limit_s)
    return success


async def _place_and_verify_order(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    contract: object,
    ib_order: object,
    order_row: OrderRow,
    tws_order_id: int,
    notifier: TelegramNotifier,
) -> bool:
    """Sendet die Order und prüft auf Fehler (z. B. Read-Only Modus)."""
    trade = interactive_brokers.placeOrder(contract, ib_order)

    # Kurz warten, um sofortige Ablehnungen (z.B. ValidationError) zu erkennen
    for _ in range(20):
        if trade.orderStatus.status not in ("PendingSubmit", "PendingCancel"):
            break
        await asyncio.sleep(0.1)

    if trade.orderStatus.status in (
        "Inactive",
        "Cancelled",
        "ValidationError",
        "Error",
    ):
        log_errors = [
            entry
            for entry in trade.log
            if entry.errorCode != 0 or entry.status in ("ValidationError", "Error")
        ]
        is_only_warning_399: bool = len(log_errors) > 0 and all(
            entry.errorCode == 399 for entry in log_errors
        )

        if is_only_warning_399:
            logger.info(
                "Ignoriere Warning 399 (ValidationError/Außerbörslich) bei Orderplatzierung",
                order_id=tws_order_id,
                symbol=order_row.symbol,
            )
        else:
            error_msg = "Unbekannter Fehler"
            for entry in log_errors:
                if entry.errorCode != 399:
                    error_msg = entry.message
                    break

            logger.error(
                "Order-Uebertragung fehlgeschlagen",
                order_id=tws_order_id,
                status=trade.orderStatus.status,
                symbol=order_row.symbol,
                error=error_msg,
            )

            order_row.status = "Error"
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE orders SET status = 'Error' WHERE order_id = ?",
                    (tws_order_id,),
                )
                await db.execute("COMMIT")
            except Exception as exception:
                await db.execute("ROLLBACK")
                logger.error("Fehler beim DB-Update (Error)", error=str(exception))

            if "Read-Only mode" in error_msg or "321" in error_msg:
                await notifier.send_order_failed(
                    order_id=tws_order_id,
                    tws_code=321,
                    reason=f"API im READ-ONLY Modus. Details: {error_msg}",
                    symbol=order_row.symbol,
                    bracket_role=order_row.bracket_role,
                    is_fatal=True,
                )
            else:
                await notifier.send_order_failed(
                    order_id=tws_order_id,
                    tws_code=0,
                    reason=error_msg,
                    symbol=order_row.symbol,
                    bracket_role=order_row.bracket_role,
                    is_fatal=False,
                )
            return False

    return True


async def _adjust_exit_order_quantity(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    child: OrderRow,
    notifier: TelegramNotifier,
) -> bool:
    """Gleicht Depotbestand ab und passt die Order-Menge an oder storniert sie."""
    live_position = _get_live_position_quantity(
        interactive_brokers, child.account_id, child.symbol
    )

    available_quantity = (
        max(Decimal("0.0"), live_position)
        if child.action == "SELL"
        else max(Decimal("0.0"), -live_position)
    )

    if available_quantity <= Decimal("0.0"):
        await _cancel_empty_exit_order(db, child, live_position, notifier)
        return False

    intended_quantity = Decimal(str(child.quantity))
    if available_quantity < intended_quantity:
        await _reduce_exit_order_quantity(
            db, child, intended_quantity, available_quantity, notifier
        )

    return True


async def _cancel_empty_exit_order(
    db: aiosqlite.Connection,
    child: OrderRow,
    live_position: Decimal,
    notifier: TelegramNotifier,
) -> None:
    """Storniert eine Exit-Order bei fehlender Gegenposition im Depot."""
    logger.warning(
        "Keine offene Gegenposition im Depot gefunden. Exit-Order wird storniert.",
        trade_group_id=child.trade_group_id,
        symbol=child.symbol,
        bracket_role=child.bracket_role,
        live_position=float(live_position),
    )
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "UPDATE orders SET status = 'Cancelled' WHERE order_id = ?",
            (child.order_id,),
        )
        await db.execute("COMMIT")
    except Exception as exception:
        await db.execute("ROLLBACK")
        logger.error(
            "Fehler beim Stornieren der Child-Order in DB",
            order_id=child.order_id,
            error=str(exception),
        )

    await notifier.send_importer_info(
        title="EXIT ABGEBROCHEN",
        file_name=child.trade_group_id,
        status="Storniert",
        details=f"Keine offene Position für {child.symbol} vorhanden (Depotbestand: {float(live_position)}).",
        emoji="⚠️",
    )


async def _reduce_exit_order_quantity(
    db: aiosqlite.Connection,
    child: OrderRow,
    intended_quantity: Decimal,
    available_quantity: Decimal,
    notifier: TelegramNotifier,
) -> None:
    """Reduziert Exit-Menge auf verbleibenden Depotbestand."""
    logger.info(
        "Exit-Order Menge an realen Depotbestand angepasst",
        trade_group_id=child.trade_group_id,
        old_qty=float(intended_quantity),
        new_qty=float(available_quantity),
    )
    child.quantity = int(available_quantity)

    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "UPDATE orders SET quantity = ? WHERE order_id = ?",
            (child.quantity, child.order_id),
        )
        await db.execute("COMMIT")
    except Exception as exception:
        await db.execute("ROLLBACK")
        logger.error(
            "Fehler beim Aktualisieren der Child-Order Menge in DB",
            order_id=child.order_id,
            error=str(exception),
        )

    await notifier.send_importer_info(
        title="EXIT MENGE ANGEPASST",
        file_name=child.trade_group_id,
        status="Reduziert",
        details=f"Stückzahl für {child.symbol} von {float(intended_quantity)} auf {float(available_quantity)} reduziert.",
        emoji="⚠️",
    )


def _get_live_position_quantity(
    interactive_brokers: IB, account_id: str, symbol: str
) -> Decimal:
    """Ermittelt den aktuellen Depotbestand für ein bestimmtes Symbol und Account."""
    for position in interactive_brokers.positions():
        if (
            position.account == account_id
            and position.contract.symbol == symbol.upper()
        ):
            return Decimal(str(position.position))
    return Decimal("0.0")


async def _get_next_non_colliding_order_id(
    db: aiosqlite.Connection, interactive_brokers: IB
) -> int:
    """Ermittelt die nächste gültige Order-ID zur Abwehr von DB-ID-Kollisionen."""
    async with db.execute("SELECT MAX(order_id) FROM orders") as cursor:
        row = await cursor.fetchone()
        max_db_id = row[0] if (row and row[0] is not None) else 0

    if max_db_id > 0:
        current_sequence = getattr(interactive_brokers.client, "_reqIdSeq", None)
        if isinstance(current_sequence, int):
            interactive_brokers.client._reqIdSeq = max(current_sequence, max_db_id + 1)

    return interactive_brokers.client.getReqId()
