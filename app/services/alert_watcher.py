"""
Überwachungs- und Alert-Watcher-Hintergrunddienste.

Periodische Hintergrundprozesse zur Erkennung hängender Orders (Dead Orders),
hoher Ausführungs-Slippage und Abgleich offener TWS-Order-Zustände.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
from app.services.notifier import TelegramNotifier
from app.trading.recovery import run_recovery

logger = structlog.get_logger()


async def alert_watcher(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    notifier: TelegramNotifier,
    config: Config,
    interval_seconds: int = 60,
    dead_order_threshold_minutes: int = 15,
    max_slippage_pct: float = 0.01,
) -> None:
    """
    Asynchroner Alert-Watcher-Hauptloop (Hintergrunddienst).

    Führt periodisch die Dead-Order-Überprüfung und die Slippage-Kontrolle aus.
    """
    logger.info("Starting Alert Watcher background service", interval=interval_seconds)
    state = AlertState()

    while True:
        try:
            db = await db_factory()
            try:
                # 1. Dead Order Check
                await check_dead_orders(
                    db, notifier, state, dead_order_threshold_minutes
                )
                # 2. Hohe Slippage Check
                await check_high_slippage(db, notifier, state, max_slippage_pct)
            finally:
                await db.close()
        except Exception as exception:
            logger.error("Unexpected error in Alert Watcher loop", error=str(exception))

        await asyncio.sleep(interval_seconds)


async def order_status_sync_loop(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    interactive_brokers: IB,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Awaitable[None]],
    config: Config,
    interval_seconds: int = 300,
) -> None:
    """
    Asynchroner Hintergrunddienst für den periodischen Order-Zustandsabgleich.

    Gleicht den lokalen Order-Status periodisch mit der TWS ab (Active State Reconciliation).
    """
    logger.info(
        "Starting periodic order status reconciliation",
        interval=interval_seconds,
    )

    # Erste Wartezeit einhalten, da Recovery bereits beim Systemstart ausgeführt wurde
    await asyncio.sleep(interval_seconds)

    while True:
        try:
            db = await db_factory()
            try:
                await run_recovery(
                    database_connection=db,
                    interactive_brokers_session=interactive_brokers,
                    queue=queue,
                    notifier=notifier,
                    trigger_settlement_callback=trigger_settlement_callback,
                    config=config,
                )
            finally:
                await db.close()
        except Exception as exception:
            logger.error(
                "Unexpected error in Order Status Sync loop",
                error=str(exception),
            )

        await asyncio.sleep(interval_seconds)


async def check_dead_orders(
    db: aiosqlite.Connection,
    notifier: TelegramNotifier,
    state: "AlertState",
    threshold_minutes: int = 15,
    current_time: datetime | None = None,
) -> None:
    """
    Prüft, ob Orders im Status 'Submitted' hängen, deren Übermittlungszeitpunkt
    länger als `threshold_minutes` zurückliegt, unter Berücksichtigung der US-Handelszeiten.
    """
    new_york_timezone = ZoneInfo("America/New_York")
    if current_time is None:
        current_time_new_york = datetime.now(new_york_timezone)
    elif current_time.tzinfo is None:
        current_time_new_york = current_time.replace(tzinfo=UTC).astimezone(
            new_york_timezone
        )
    else:
        current_time_new_york = current_time.astimezone(new_york_timezone)

    # 1. Keine Prüfung am Wochenende
    if current_time_new_york.weekday() >= 5:
        return

    market_open_today = current_time_new_york.replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    market_close_today = current_time_new_york.replace(
        hour=16, minute=0, second=0, microsecond=0
    )

    # 2. Keine Prüfung außerhalb der regulären US-Handelszeiten
    if not (market_open_today <= current_time_new_york <= market_close_today):
        return

    try:
        rows = await _fetch_submitted_orders(db)
    except Exception as exception:
        logger.error("Error during dead order check", error=str(exception))
        return

    for row in rows:
        await _process_single_potential_dead_order(
            order_row=row,
            notifier=notifier,
            alert_state=state,
            current_time_new_york=current_time_new_york,
            market_open_today=market_open_today,
            new_york_timezone=new_york_timezone,
            threshold_minutes=threshold_minutes,
        )


async def check_high_slippage(
    db: aiosqlite.Connection,
    notifier: TelegramNotifier,
    state: "AlertState",
    max_slippage_pct: float = 0.01,
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
                price_diff_slippage = Decimal(str(row["price_diff_slippage"]))
                avg_entry_price = Decimal(str(row["avg_entry_price"]))
                symbol = row["symbol"]

                # Prämisse: ABS(price_diff_slippage) > avg_entry_price * max_slippage_pct
                if avg_entry_price > Decimal("0"):
                    slippage_limit = avg_entry_price * Decimal(str(max_slippage_pct))
                    if abs(price_diff_slippage) > slippage_limit:
                        if not state.is_group_reported(trade_group_id):
                            message_content = (
                                f"📉 <b>HIGH SLIPPAGE</b> | <code>{symbol}</code>"
                            )
                            logger.warning(
                                "High slippage detected",
                                trade_group_id=trade_group_id,
                                slippage=float(price_diff_slippage),
                            )

                            if await notifier.send_message(message_content):
                                state.mark_group_reported(trade_group_id)
    except Exception as exception:
        logger.error("Error during slippage check", error=str(exception))


class AlertState:
    """
    Hält den In-Memory-Status bereits gemeldeter Probleme.

    Dient dazu, redundante Telegram-Nachrichten zu unterbinden.
    """

    def __init__(self) -> None:
        self.reported_order_ids: set[int] = set()
        self.reported_trade_groups: set[str] = set()

    def is_order_reported(self, order_id: int) -> bool:
        """Gibt an, ob die Order bereits gemeldet wurde."""
        return order_id in self.reported_order_ids

    def mark_order_reported(self, order_id: int) -> None:
        """Markiert die Order als gemeldet."""
        self.reported_order_ids.add(order_id)

    def is_group_reported(self, trade_group_id: str) -> bool:
        """Gibt an, ob die Trade-Gruppe bereits gemeldet wurde."""
        return trade_group_id in self.reported_trade_groups

    def mark_group_reported(self, trade_group_id: str) -> None:
        """Markiert die Trade-Gruppe als gemeldet."""
        self.reported_trade_groups.add(trade_group_id)


async def _fetch_submitted_orders(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    """Ruft alle Orders mit dem Status 'Submitted' aus der Datenbank ab."""
    query = """
        SELECT order_id, trade_group_id, symbol, transmitted_at
        FROM orders
        WHERE status = 'Submitted' AND order_type IN ('MKT', 'MOC')
    """
    async with db.execute(query) as cursor:
        return await cursor.fetchall()


async def _process_single_potential_dead_order(
    order_row: aiosqlite.Row,
    notifier: TelegramNotifier,
    alert_state: AlertState,
    current_time_new_york: datetime,
    market_open_today: datetime,
    new_york_timezone: ZoneInfo,
    threshold_minutes: int,
) -> None:
    """Überprüft eine einzelne Order auf Überschreiten des Timeouts und alarmiert ggf."""
    order_id = order_row["order_id"]
    trade_group_id = order_row["trade_group_id"]
    symbol = order_row["symbol"]
    transmitted_at_string = order_row["transmitted_at"]

    if not transmitted_at_string:
        return

    try:
        transmitted_at_utc = datetime.strptime(
            transmitted_at_string, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=UTC)
    except ValueError as exception:
        logger.error(
            "Error parsing transmitted_at",
            order_id=order_id,
            value=transmitted_at_string,
            error=str(exception),
        )
        return

    transmitted_at_new_york = transmitted_at_utc.astimezone(new_york_timezone)

    if transmitted_at_new_york < market_open_today:
        effective_activation_time = market_open_today
    else:
        effective_activation_time = transmitted_at_new_york

    if current_time_new_york < effective_activation_time:
        return

    active_duration = current_time_new_york - effective_activation_time
    threshold_duration = timedelta(minutes=threshold_minutes)

    if active_duration <= threshold_duration:
        return

    if alert_state.is_order_reported(order_id):
        return

    message_content = f"⚠️ <b>DEAD ORDER</b> | <code>{symbol}</code>"
    logger.warning(
        "Dead order detected",
        order_id=order_id,
        trade_group_id=trade_group_id,
    )

    if await notifier.send_message(message_content):
        alert_state.mark_order_reported(order_id)
