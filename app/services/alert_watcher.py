"""
Überwachungs- und Alert-Watcher-Hintergrunddienste.

Periodische Hintergrundprozesse zur Erkennung hängender Orders (Dead Orders),
hoher Ausführungs-Slippage und Abgleich offener TWS-Order-Zustände.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
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
    max_slippage_percentage: float = 0.01,
    archive_dir: Path | None = None,
) -> None:
    """
    Asynchroner Alert-Watcher-Hauptloop (Hintergrunddienst).

    Führt periodisch die Dead-Order-Überprüfung, Slippage-Kontrolle,
    Hanging-Order-Erkennung und Dateisystem-Scans nach .err-Archivdateien aus.
    """
    logger.info("Starting Alert Watcher background service", interval=interval_seconds)
    state = AlertState()
    resolved_archive_dir = (
        archive_dir if archive_dir is not None else Path("data/orders/archive")
    )

    while True:
        try:
            db = await db_factory()
            try:
                # 1. Dead Order Check
                await check_dead_orders(
                    db, notifier, state, dead_order_threshold_minutes
                )
                # 2. Hohe Slippage Check
                await check_high_slippage(db, notifier, state, max_slippage_percentage)
                # 3. Hängende Created-Orders Check
                await check_hanging_orders(db, notifier, state, threshold_minutes=10)
                # 4. Archivierte Fehlerdateien (.err) Check
                await check_archived_error_files(resolved_archive_dir, notifier, state)
            finally:
                await db.close()
        except Exception as exception:
            logger.error("Unexpected error in Alert Watcher loop", error=str(exception))

        await asyncio.sleep(interval_seconds)


async def order_status_sync_loop(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    interactive_brokers: IB,
    queue: asyncio.Queue[str],
    notifier: TelegramNotifier,
    trigger_settlement_callback: Callable[[str, str], Coroutine[Any, Any, None]],
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
    # Erweitere das Überwachungsfenster um 30 Minuten, damit MOC-Orders nach
    # Börsenschluss (16:00 Uhr) geprüft werden können.
    extended_close_today = market_close_today + timedelta(minutes=30)

    # 2. Keine Prüfung außerhalb der regulären US-Handelszeiten (inkl. MOC-Puffer)
    if not (market_open_today <= current_time_new_york <= extended_close_today):
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
            market_close_today=market_close_today,
            new_york_timezone=new_york_timezone,
            threshold_minutes=threshold_minutes,
        )


async def check_high_slippage(
    db: aiosqlite.Connection,
    notifier: TelegramNotifier,
    state: "AlertState",
    max_slippage_percentage: float = 0.01,
) -> None:
    """
    Prüft auf hohe Slippage (Abweichung des realisierten Einstiegspreises vom Target).
    Vergleicht den absoluten Wert von price_diff_slippage mit dem avg_entry_price * max_slippage_percentage.
    """
    query = """
        SELECT ts.trade_group_id, ts.price_diff_slippage, ts.avg_entry_price, o.symbol, o.target_price
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
                target_price_raw = row["target_price"]

                # Ignoriere Orders ohne echten Target-Preis (z. B. Target 0, MKT, MOC, MOO)
                if target_price_raw is None:
                    continue
                target_price = Decimal(str(target_price_raw))
                if target_price <= Decimal("0"):
                    continue

                # Prämisse: ABS(price_diff_slippage) > avg_entry_price * max_slippage_percentage
                if avg_entry_price > Decimal("0"):
                    slippage_limit = avg_entry_price * Decimal(
                        str(max_slippage_percentage)
                    )
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
        self.reported_error_files: set[str] = set()
        self.reported_hanging_order_ids: set[int] = set()

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

    def is_file_reported(self, file_name: str) -> bool:
        """Gibt an, ob die Fehlerdatei bereits gemeldet wurde."""
        return file_name in self.reported_error_files

    def mark_file_reported(self, file_name: str) -> None:
        """Markiert die Fehlerdatei als gemeldet."""
        self.reported_error_files.add(file_name)

    def is_hanging_order_reported(self, order_id: int) -> bool:
        """Gibt an, ob die hängende Order bereits gemeldet wurde."""
        return order_id in self.reported_hanging_order_ids

    def mark_hanging_order_reported(self, order_id: int) -> None:
        """Markiert die hängende Order als gemeldet."""
        self.reported_hanging_order_ids.add(order_id)


async def check_archived_error_files(
    archive_dir: Path,
    notifier: TelegramNotifier,
    state: AlertState,
) -> None:
    """Scannt das Archiv-Verzeichnis nach neuen .err-Dateien und alarmiert sofort.

    Args:
        archive_dir: Pfad zum Archiv-Verzeichnis.
        notifier: Der Telegram-Notifier-Dienst.
        state: Der AlertState-Zustand zur Vermeidung doppelter Meldungen.
    """
    if not archive_dir.exists() or not archive_dir.is_dir():
        return

    try:
        err_files = sorted(archive_dir.glob("*.err"))
        for err_file in err_files:
            file_name = err_file.name
            if not state.is_file_reported(file_name):
                logger.warning(
                    "Archived error file discovered by watcher",
                    file_name=file_name,
                )
                if await notifier.send_archived_error_alert(file_name):
                    state.mark_file_reported(file_name)
    except Exception as exception:
        logger.error(
            "Error scanning archived error files",
            archive_dir=str(archive_dir),
            error=str(exception),
        )


async def check_hanging_orders(
    db: aiosqlite.Connection,
    notifier: TelegramNotifier,
    state: AlertState,
    threshold_minutes: int = 10,
    current_time: datetime | None = None,
) -> None:
    """Scannt nach Orders im Status 'Created', die ungewöhnlich lange nicht verarbeitet wurden.

    Args:
        db: Die offene SQLite-Datenbankverbindung.
        notifier: Der Telegram-Notifier-Dienst.
        state: Der AlertState-Zustand.
        threshold_minutes: Schwellenwert in Minuten, ab wann eine Order als hängend gilt.
        current_time: Optionaler Referenzzeitpunkt für Tests.
    """
    query = """
        SELECT order_id, trade_group_id, symbol
        FROM orders
        WHERE status = 'Created'
    """
    try:
        async with db.execute(query) as cursor:
            async for row in cursor:
                order_id = int(row["order_id"])
                symbol = str(row["symbol"])
                trade_group_id = str(row["trade_group_id"])

                if state.is_hanging_order_reported(order_id):
                    continue

                logger.warning(
                    "Hanging created order detected",
                    order_id=order_id,
                    trade_group_id=trade_group_id,
                    symbol=symbol,
                )
                message = (
                    f"⚠️ <b>HÄNGENDE ORDER (Status: Created)</b> | <code>{symbol}</code>\n"
                    f"├─ <b>Order-ID:</b> <code>{order_id}</code>\n"
                    f"├─ <b>Trade-Gruppe:</b> <code>{trade_group_id}</code>\n"
                    f"└─ <b>Hinweis:</b> Order verweilt länger als {threshold_minutes} Minuten in 'Created'."
                )
                if await notifier.send_message(message):
                    state.mark_hanging_order_reported(order_id)
    except Exception as exception:
        logger.error("Error during hanging orders check", error=str(exception))


async def _fetch_submitted_orders(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    """Ruft alle Orders mit dem Status 'Submitted' aus der Datenbank ab."""
    query = """
        SELECT order_id, trade_group_id, symbol, order_type, transmitted_at
        FROM orders
        WHERE status = 'Submitted' AND order_type IN ('MKT', 'MOC')
    """
    async with db.execute(query) as cursor:
        rows = await cursor.fetchall()
        return list(rows)


async def _process_single_potential_dead_order(
    order_row: aiosqlite.Row,
    notifier: TelegramNotifier,
    alert_state: AlertState,
    current_time_new_york: datetime,
    market_open_today: datetime,
    market_close_today: datetime,
    new_york_timezone: ZoneInfo,
    threshold_minutes: int,
) -> None:
    """Überprüft eine einzelne Order auf Überschreiten des Timeouts und alarmiert ggf."""
    order_id = order_row["order_id"]
    trade_group_id = order_row["trade_group_id"]
    symbol = order_row["symbol"]
    order_type = order_row["order_type"]
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

    if order_type == "MOC":
        # MOC-Orders werden erst ab Börsenschluss aktiv geschaltet.
        effective_activation_time = market_close_today
    elif transmitted_at_new_york < market_open_today:
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
