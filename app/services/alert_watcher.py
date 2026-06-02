import asyncio
from datetime import datetime, timedelta, timezone
from typing import Set
import structlog
import aiosqlite
from ib_async import IB
from app.core.config import Config
from app.services.notifier import TelegramNotifier
from app.trading.recovery import run_recovery

logger = structlog.get_logger()

class AlertState:
    """
    Hält den In-Memory-Status bereits gemeldeter Probleme,
    um redundante Telegram-Nachrichten zu verhindern.
    """
    def __init__(self):
        self.reported_order_ids: Set[int] = set()
        self.reported_trade_groups: Set[str] = set()

    def is_order_reported(self, order_id: int) -> bool:
        return order_id in self.reported_order_ids

    def mark_order_reported(self, order_id: int) -> None:
        self.reported_order_ids.add(order_id)

    def is_group_reported(self, trade_group_id: str) -> bool:
        return trade_group_id in self.reported_trade_groups

    def mark_group_reported(self, trade_group_id: str) -> None:
        self.reported_trade_groups.add(trade_group_id)


async def check_dead_orders(
    db: aiosqlite.Connection,
    notifier: TelegramNotifier,
    state: AlertState,
    threshold_minutes: int = 15
) -> None:
    """
    Prüft, ob Orders im Status 'Submitted' hängen, deren Übermittlungszeitpunkt
    länger als `threshold_minutes` zurückliegt.
    """
    # UTC-Zeit bestimmen
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    cutoff_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")

    query = """
        SELECT order_id, trade_group_id, symbol, transmitted_at 
        FROM orders 
        WHERE status = 'Submitted' AND transmitted_at < ?
    """
    try:
        async with db.execute(query, (cutoff_str,)) as cursor:
            async for row in cursor:
                order_id = row["order_id"]
                trade_group_id = row["trade_group_id"]
                symbol = row["symbol"]
                transmitted_at = row["transmitted_at"]

                if not state.is_order_reported(order_id):
                    msg = f"⚠️ DEAD ORDER: {trade_group_id} | {symbol} | seit {transmitted_at}"
                    logger.warning("Dead Order erkannt", order_id=order_id, trade_group_id=trade_group_id)
                    
                    if await notifier.send_message(msg):
                        state.mark_order_reported(order_id)
    except Exception as e:
        logger.error("Fehler beim Dead-Order-Check", error=str(e))


async def check_high_slippage(
    db: aiosqlite.Connection,
    notifier: TelegramNotifier,
    state: AlertState,
    max_slippage_pct: float = 0.01  # Default: 1% Slippage Limit
) -> None:
    """
    Prüft auf hohe Slippage (Abweichung des realisierten Einstiegspreises vom Target).
    Vergleicht den absoluten Wert von price_diff_slippage mit dem avg_entry_price * max_slippage_pct.
    """
    query = """
        SELECT ts.trade_group_id, ts.price_diff_slippage, ts.avg_entry_price, o.symbol
        FROM trades_settlement ts
        JOIN orders o ON ts.trade_group_id = o.trade_group_id AND o.bracket_role = 'ENTRY'
    """
    try:
        async with db.execute(query) as cursor:
            async for row in cursor:
                trade_group_id = row["trade_group_id"]
                price_diff_slippage = row["price_diff_slippage"]
                avg_entry_price = row["avg_entry_price"]
                symbol = row["symbol"]

                # Prämisse: ABS(price_diff_slippage) > avg_entry_price * max_slippage_pct
                if avg_entry_price > 0:
                    slippage_limit = avg_entry_price * max_slippage_pct
                    if abs(price_diff_slippage) > slippage_limit:
                        if not state.is_group_reported(trade_group_id):
                            msg = f"📉 SLIPPAGE: {symbol} | {price_diff_slippage:.4f} | Trade: {trade_group_id}"
                            logger.warning("Hohe Slippage erkannt", trade_group_id=trade_group_id, slippage=price_diff_slippage)
                            
                            if await notifier.send_message(msg):
                                state.mark_group_reported(trade_group_id)
    except Exception as e:
        logger.error("Fehler beim Slippage-Check", error=str(e))


async def alert_watcher(
    db_factory,  # Funktion zur Erstellung einer DB-Verbindung (oder offene Connection)
    notifier: TelegramNotifier,
    config: Config,
    interval_seconds: int = 60,
    dead_order_threshold_min: int = 15,
    max_slippage_pct: float = 0.01
) -> None:
    """
    Der asynchrone Alert-Watcher-Loop. Läuft permanent im Hintergrund
    und triggert die verschiedenen Überwachungsfunktionen.
    """
    logger.info("Starte Alert Watcher Hintergrunddienst", interval=interval_seconds)
    state = AlertState()

    while True:
        try:
            # Wir holen eine frische Verbindung für den Watcher-Durchlauf
            db = await db_factory()
            try:
                # 1. Dead Order Check
                await check_dead_orders(db, notifier, state, dead_order_threshold_min)
                # 2. Hohe Slippage Check
                await check_high_slippage(db, notifier, state, max_slippage_pct)
            finally:
                await db.close()
        except Exception as e:
            logger.error("Unerwarteter Fehler im Alert Watcher Loop", error=str(e))
        
        await asyncio.sleep(interval_seconds)


async def order_status_sync_loop(
    db_factory,
    ib: IB,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    trigger_settlement_cb,
    config: Config,
    interval_seconds: int = 300
) -> None:
    """
    Der asynchrone Hintergrunddienst für den periodischen Order-Zustandsabgleich (Active State Reconciliation).
    Läuft permanent im Hintergrund und gleicht lokale Orders mit der TWS ab.
    """
    logger.info("Starte periodischen Order-Zustandsabgleich (Reconciliation)", interval=interval_seconds)
    
    while True:
        try:
            db = await db_factory()
            try:
                await run_recovery(
                    database_connection=db,
                    interactive_brokers_session=ib,
                    queue=queue,
                    notifier=notifier,
                    trigger_settlement_cb=trigger_settlement_cb,
                    config=config
                )
            finally:
                await db.close()
        except Exception as e:
            logger.error("Unerwarteter Fehler im Order-Status-Sync Loop", error=str(e))
        
        await asyncio.sleep(interval_seconds)

