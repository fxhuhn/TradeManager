import asyncio
from collections.abc import Callable, Awaitable
from typing import Final
import structlog
from ib_async import IB
import aiosqlite
from app.core.config import Config
from app.core.models import OrderRow
from app.services.notifier import TelegramNotifier
from app.trading.order_builder import make_stock_contract, build_order

logger = structlog.get_logger()

# Globales Lock zur Sicherung der Atomarität von getReqId() + DB-Write
ORDER_ID_LOCK = asyncio.Lock()


async def process_trade_group(
    db: aiosqlite.Connection,
    ib: IB,
    trade_group_id: str,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Verarbeitet eine einzelne Trade-Gruppe aus der Queue.
    Sendet ENTRY und zugehörige Child-Orders (SL, TP, EXIT) an TWS.
    """
    logger.info("Verarbeite Trade-Gruppe aus Queue", trade_group_id=trade_group_id)

    # 1. Alle Orders der Gruppe aus DB laden
    orders: list[OrderRow] = []
    async with db.execute(
        """
        SELECT order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
               symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name,
               status, retry_count, transmitted_at
        FROM orders 
        WHERE trade_group_id = ?
        """,
        (trade_group_id,),
    ) as cursor:
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

    if not orders:
        logger.warning(
            "Keine Orders fuer Trade-Gruppe in DB gefunden",
            trade_group_id=trade_group_id,
        )
        return

    # Entry-Order und Child-Orders isolieren
    entry_order = next(
        (order for order in orders if order.bracket_role == "ENTRY"), None
    )
    child_orders = [order for order in orders if order.bracket_role != "ENTRY"]

    if not entry_order:
        logger.error(
            "Keine ENTRY-Order in Gruppe vorhanden", trade_group_id=trade_group_id
        )
        return

    # Scenario 3: Post-Fill Child (Entry ist bereits Filled, neue Exits werden geschickt)
    is_post_fill: Final[bool] = entry_order.status == "Filled"

    # 2. ENTRY-Order verarbeiten falls status='Created' (Normaler Bracket)
    if entry_order.status == "Created":
        logger.info(
            "Normaler Bracket: Verarbeite ENTRY-Order", trade_group_id=trade_group_id
        )

        # Atomares Reservieren und Schreiben der TWS-OrderId
        async with ORDER_ID_LOCK:
            tws_order_id = ib.client.getReqId()

            await db.execute("BEGIN IMMEDIATE")
            try:
                # Fremdschlüssel-Kaskadierung (ON UPDATE CASCADE) aktualisiert automatisch parent_id in Children
                await db.execute(
                    "UPDATE orders SET order_id = ?, status = 'Submitted', transmitted_at = datetime('now') WHERE order_id = ?",
                    (tws_order_id, entry_order.order_id),
                )
                await db.execute("COMMIT")
            except Exception as exception:
                await db.execute("ROLLBACK")
                logger.error(
                    "Fehler beim Zuweisen der ENTRY order_id",
                    trade_group_id=trade_group_id,
                    error=str(exception),
                )
                raise exception

        # Order-Objekt updaten
        entry_order.order_id = tws_order_id
        entry_order.status = "Submitted"

        # TWS-Order konstruieren
        contract = make_stock_contract(entry_order.symbol)
        ib_entry_order = build_order(entry_order)

        # transmit = False, wenn noch ungesendete Kinder existieren (hält Bracket zusammen)
        has_unsent_children = any(child.status == "Created" for child in child_orders)
        if has_unsent_children:
            ib_entry_order.transmit = False
            logger.debug(
                "ENTRY-Order mit transmit=False konfiguriert (wartet auf Children)",
                order_id=tws_order_id,
            )
        else:
            ib_entry_order.transmit = True

        # An TWS übergeben
        logger.info(
            "Sende ENTRY-Order an TWS", order_id=tws_order_id, symbol=entry_order.symbol
        )
        ib.placeOrder(contract, ib_entry_order)

        # Rate-Limit aus Konfiguration einhalten
        await asyncio.sleep(config.app.order_rate_limit_s)

    # 3. Child-Orders verarbeiten (SL, TP, EXIT)
    # Sortieren, damit das LETZTE Leg die Transmission freischaltet (transmit=True)
    for iteration_index, child in enumerate(child_orders):
        if child.status == "Created":
            logger.info(
                "Verarbeite Child-Order",
                bracket_role=child.bracket_role,
                trade_group_id=trade_group_id,
            )

            # Atomares Reservieren und Schreiben der TWS-OrderId
            async with ORDER_ID_LOCK:
                tws_order_id = ib.client.getReqId()

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
                        trade_group_id=trade_group_id,
                        error=str(exception),
                    )
                    raise exception

            child.order_id = tws_order_id
            child.status = "Submitted"

            contract = make_stock_contract(child.symbol)
            ib_child_order = build_order(child)

            # parentId verlinken (außer bei Post-Fill Child, da ist Parent schon Filled)
            if not is_post_fill:
                ib_child_order.parentId = entry_order.order_id
                logger.debug(
                    "Child verlinkt mit Parent",
                    child_id=tws_order_id,
                    parent_id=entry_order.order_id,
                )
            else:
                logger.info(
                    "Post-Fill Child: Sende Child ohne parentId", child_id=tws_order_id
                )

            # transmit = True für das letzte Child-Leg (schaltet TWS-Bracket frei)
            is_last = iteration_index == len(child_orders) - 1
            ib_child_order.transmit = is_last

            logger.info(
                "Sende Child-Order an TWS",
                order_id=tws_order_id,
                role=child.bracket_role,
            )
            ib.placeOrder(contract, ib_child_order)

            # Rate-Limit aus Konfiguration einhalten
            await asyncio.sleep(config.app.order_rate_limit_s)


async def execution_worker(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    ib: IB,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Der unendliche asynchrone Execution Worker Task.
    Konsumiert trade_group_ids aus der Queue und sendet sie an TWS.
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
            raise  # Prämisse: CancelledError immer weiterwerfen!
        except Exception as exception:
            logger.error("Fehler im Execution Worker Loop", error=str(exception))
            await asyncio.sleep(1.0)
