"""
Ausführungsworker (Execution Worker) für TWS-Auftragsplatzierungen.

Verarbeitet Trade-Gruppen asynchron aus einer Queue und sendet
die entsprechenden ENTRY und Child-Orders (SL, TP, EXIT) an die TWS.
"""

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Final

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
from app.core.models import OrderRow
from app.services.notifier import TelegramNotifier
from app.trading.order_builder import build_order, make_stock_contract

logger = structlog.get_logger()

# Globales Lock zur Sicherung der Atomarität von getReqId() + DB-Write
ORDER_ID_LOCK = asyncio.Lock()


async def execution_worker(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    ib: IB,
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
                await process_trade_group(db, ib, trade_group_id, notifier, config)
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
    ib: IB,
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

    entry_order = next((order for order in orders if order.bracket_role == "ENTRY"), None)
    child_orders = [order for order in orders if order.bracket_role != "ENTRY"]

    if not entry_order:
        logger.error(
            "Keine ENTRY-Order in Gruppe vorhanden", trade_group_id=trade_group_id
        )
        return

    is_post_fill: Final[bool] = entry_order.status == "Filled"

    # 1. ENTRY-Order verarbeiten falls status='Created' (Normaler Bracket-Einstieg)
    if entry_order.status == "Created":
        logger.info(
            "Normaler Einstieg: Verarbeite ENTRY-Order", trade_group_id=trade_group_id
        )
        await _process_entry_order(db, ib, entry_order, child_orders, notifier, config)

    # 2. Child-Orders verarbeiten (SL, TP, EXIT)
    await _process_child_orders(db, ib, entry_order, child_orders, is_post_fill, notifier, config)


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
            orders.append(
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
    return orders


async def _process_entry_order(
    db: aiosqlite.Connection,
    ib: IB,
    entry_order: OrderRow,
    child_orders: list[OrderRow],
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """Weist dem Entry eine TWS Order-ID zu, aktualisiert die DB und übermittelt an TWS."""
    async with ORDER_ID_LOCK:
        tws_order_id = await _get_next_non_colliding_order_id(db, ib)

        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "UPDATE orders SET order_id = ?, status = 'Submitted', transmitted_at = datetime('now') WHERE order_id = ?",
                (tws_order_id, entry_order.order_id),
            )
            await db.execute("COMMIT")
        except Exception as exception:
            await db.execute("ROLLBACK")
            logger.error(
                "Fehler beim Zuweisen der ENTRY order_id",
                trade_group_id=entry_order.trade_group_id,
                error=str(exception),
            )
            raise exception

    entry_order.order_id = tws_order_id
    entry_order.status = "Submitted"

    contract = make_stock_contract(entry_order.symbol)
    ib_entry_order = build_order(entry_order)

    # Wenn noch ungesendete Kinder existieren, übertragen wir die Order noch nicht vollständig
    has_unsent_children = any(child.status == "Created" for child in child_orders)
    ib_entry_order.transmit = not has_unsent_children

    logger.info(
        "Sende ENTRY-Order an TWS", order_id=tws_order_id, symbol=entry_order.symbol
    )
    ib.placeOrder(contract, ib_entry_order)

    price_str = (
        f" @ {entry_order.target_price:.2f}"
        if entry_order.target_price and entry_order.target_price > 0
        else ""
    )
    await notifier.send_message(
        f"📤 ORDER GESENDET: {entry_order.symbol} | {entry_order.bracket_role} | "
        f"{entry_order.action} {entry_order.quantity}{price_str} ({entry_order.order_type}) | "
        f"ID: {tws_order_id} ({entry_order.strategy_name})"
    )

    await asyncio.sleep(config.app.order_rate_limit_s)


async def _process_child_orders(
    db: aiosqlite.Connection,
    ib: IB,
    entry_order: OrderRow,
    child_orders: list[OrderRow],
    is_post_fill: bool,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """Verarbeitet die verbleibenden untergeordneten Orders (SL, TP, EXIT)."""
    for iteration_index, child in enumerate(child_orders):
        if child.status != "Created":
            continue

        logger.info(
            "Verarbeite Child-Order",
            bracket_role=child.bracket_role,
            trade_group_id=child.trade_group_id,
        )

        if is_post_fill and child.bracket_role in ("SL", "TP", "EXIT"):
            should_continue = await _adjust_exit_order_quantity(db, ib, child, notifier)
            if not should_continue:
                continue

        async with ORDER_ID_LOCK:
            tws_order_id = await _get_next_non_colliding_order_id(db, ib)

            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE orders SET order_id = ?, status = 'Submitted', transmitted_at = datetime('now') WHERE order_id = ?",
                    (tws_order_id, child.order_id),
                )
                await db.execute("COMMIT")
            except Exception as exception:
                await db.execute("ROLLBACK")
                logger.error(
                    "Fehler beim Zuweisen der Child order_id",
                    trade_group_id=child.trade_group_id,
                    error=str(exception),
                )
                raise exception

        child.order_id = tws_order_id
        child.status = "Submitted"

        contract = make_stock_contract(child.symbol)
        ib_child_order = build_order(child)

        if not is_post_fill:
            ib_child_order.parentId = entry_order.order_id

        is_last = iteration_index == len(child_orders) - 1
        ib_child_order.transmit = is_last

        logger.info(
            "Sende Child-Order an TWS",
            order_id=tws_order_id,
            role=child.bracket_role,
        )
        ib.placeOrder(contract, ib_child_order)

        price_str = (
            f" @ {child.target_price:.2f}"
            if child.target_price and child.target_price > 0
            else ""
          )
        await notifier.send_message(
            f"📤 ORDER GESENDET: {child.symbol} | {child.bracket_role} | "
            f"{child.action} {child.quantity}{price_str} ({child.order_type}) | "
            f"ID: {tws_order_id} ({child.strategy_name})"
        )

        await asyncio.sleep(config.app.order_rate_limit_s)


async def _adjust_exit_order_quantity(
    db: aiosqlite.Connection,
    ib: IB,
    child: OrderRow,
    notifier: TelegramNotifier,
) -> bool:
    """Gleicht Depotbestand ab und passt die Order-Menge an oder storniert sie."""
    live_position = _get_live_position_quantity(ib, child.account_id, child.symbol)

    if child.action == "SELL":
        available_quantity = max(Decimal("0.0"), live_position)
    else:
        available_quantity = max(Decimal("0.0"), -live_position)

    if available_quantity <= Decimal("0.0"):
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

        await notifier.send_message(
            f"⚠️ EXIT ABGEBROCHEN ({child.trade_group_id}): Keine offene Position für {child.symbol} "
            f"vorhanden (Depotbestand: {float(live_position)}). Order wurde storniert."
        )
        return False

    intended_quantity = Decimal(str(child.quantity))
    if available_quantity < intended_quantity:
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

        await notifier.send_message(
            f"⚠️ EXIT MENGE ANGEPASST ({child.trade_group_id}): Stückzahl für {child.symbol} von "
            f"{float(intended_quantity)} auf {float(available_quantity)} reduziert, um dem realen Depotbestand zu entsprechen."
        )
    return True


def _get_live_position_quantity(ib: IB, account_id: str, symbol: str) -> Decimal:
    """Ermittelt den aktuellen Depotbestand für ein bestimmtes Symbol und Account."""
    for position in ib.positions():
        if (
            position.account == account_id
            and position.contract.symbol == symbol.upper()
        ):
            return Decimal(str(position.position))
    return Decimal("0.0")


async def _get_next_non_colliding_order_id(db: aiosqlite.Connection, ib: IB) -> int:
    """Ermittelt die nächste gültige Order-ID zur Abwehr von DB-ID-Kollisionen."""
    async with db.execute("SELECT MAX(order_id) FROM orders") as cursor:
        row = await cursor.fetchone()
        max_db_id = row[0] if (row and row[0] is not None) else 0

    if max_db_id > 0:
        current_sequence = getattr(ib.client, "_reqIdSeq", None)
        if isinstance(current_sequence, int):
            ib.client._reqIdSeq = max(current_sequence, max_db_id + 1)

    return ib.client.getReqId()
