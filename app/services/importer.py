"""
Import- und Directory-Watcher-Dienste für Order-CSV-Dateien.

Überwacht ein konfiguriertes Verzeichnis nach CSV-Dateien mit Orders,
validiert deren Inhalt, führt Kapitalallokation und Downscaling durch,
speichert die Orders in der Datenbank und reiht sie in die Execution Queue ein.
"""

import asyncio
import dataclasses
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
from app.core.db import transaction
from app.core.models import LegRow
from app.services.csv_reader import load_csv, validate_group
from app.services.notifier import TelegramNotifier

logger = structlog.get_logger()


@dataclass(frozen=True)
class AccountBalanceMetrics:
    """Kontowerte fuer die Sizing-Berechnung."""

    net_liquidation_value: Decimal
    available_funds_value: Decimal
    total_cash_value: Decimal


async def csv_directory_watcher(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    interactive_brokers: IB,
    directory_path: Path,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
    interval_seconds: int = 60,
) -> None:
    """
    Hintergrunddienst zur kontinuierlichen Überwachung eines Verzeichnisses.

    Sucht nach Dateien des Musters 'orders_YYYY_MM_DD.csv', validiert diese und
    importiert sie. Erfolgreiche CSV-Dateien werden in '.csv.bak' umbenannt.
    Fehlerhafte Dateien werden in '.csv.err' umbenannt.
    """
    logger.info(
        "Starting CSV Directory Watcher background service",
        directory=str(directory_path),
        interval=interval_seconds,
    )

    date_pattern = re.compile(r"^orders_\d{4}_\d{2}_\d{2}\.csv$")

    while True:
        try:
            if directory_path.exists() and directory_path.is_dir():
                for csv_file in sorted(directory_path.glob("orders_*.csv")):
                    if date_pattern.match(csv_file.name):
                        await _process_daily_csv_file(
                            db_factory,
                            interactive_brokers,
                            csv_file,
                            queue,
                            notifier,
                            config,
                        )
        except asyncio.CancelledError:
            logger.info("CSV Directory Watcher was cancelled.")
            raise
        except Exception as exception:
            logger.error("Error in CSV Directory Watcher loop", error=str(exception))

        await asyncio.sleep(interval_seconds)


async def run_csv_import(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    csv_path: Path,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Orchestriert den Import einer einzelnen CSV-Order-Datei.

    Führt Sicherheitsprüfungen durch, lädt die CSV-Daten, berechnet das
    Downscaling und speichert die validierten Trade-Gruppen in der Datenbank.
    """
    logger.info("Starting CSV import", file=str(csv_path))

    # 1. DoS Ressourcenschutz-Check
    if not await _check_csv_dos_limits(csv_path, config, notifier):
        return

    # 2. CSV laden und gruppieren
    grouped_legs = load_csv(csv_path)
    if not grouped_legs:
        logger.info("No trade groups found for import")
        return

    # 3. Gruppen einzeln validieren, sizing anpassen und in DB verbuchen
    for trade_group_id, raw_legs in grouped_legs.items():
        await _process_and_upsert_group(
            db=db,
            interactive_brokers=interactive_brokers,
            trade_group_id=trade_group_id,
            raw_legs=raw_legs,
            queue=queue,
            notifier=notifier,
            config=config,
        )


async def _process_daily_csv_file(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    interactive_brokers: IB,
    csv_file: Path,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """Verarbeitet eine einzelne gefundene CSV-Datei und benennt sie anschließend um."""
    logger.info("New order file detected", file=csv_file.name)

    database_connection = await db_factory()
    try:
        await run_csv_import(
            database_connection, interactive_brokers, csv_file, queue, notifier, config
        )

        archive_dir = csv_file.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        backup_path = archive_dir / (csv_file.name + ".bak")
        csv_file.rename(backup_path)

        logger.info(
            "Order file successfully processed and renamed",
            file=csv_file.name,
            backup=backup_path.name,
        )
        await notifier.send_importer_info(
            file_name=csv_file.name,
            status="Erfolgreich",
            details="Eingelesen und nach .bak archiviert.",
            emoji="📁",
            title="DATEI IMPORTIERT",
        )

    except Exception as exception:
        logger.error(
            "Error processing order file",
            file=csv_file.name,
            error=str(exception),
        )

        archive_dir = csv_file.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        error_path = archive_dir / (csv_file.name + ".err")
        try:
            csv_file.rename(error_path)
            logger.warning(
                "Failed order file renamed",
                file=csv_file.name,
                error_file=error_path.name,
            )
            await notifier.send_importer_info(
                file_name=csv_file.name,
                status="Fehler",
                details=f"Nach .err umbenannt. Fehler: {str(exception)}",
                emoji="🚨",
                title="IMPORT-FEHLER",
            )
        except Exception as rename_exception:
            logger.critical(
                "Critical error renaming failed file",
                file=csv_file.name,
                error=str(rename_exception),
            )
    finally:
        await database_connection.close()


async def _check_csv_dos_limits(
    csv_path: Path, config: Config, notifier: TelegramNotifier
) -> bool:
    """Prüft, ob die CSV-Datei die im System konfigurierte maximale Dateigröße überschreitet."""
    if not csv_path.exists():
        return True
    file_size_bytes = csv_path.stat().st_size
    if file_size_bytes > config.app.max_csv_size_bytes:
        logger.error(
            "CSV file exceeds security limit. Import rejected.",
            size=file_size_bytes,
            max_size=config.app.max_csv_size_bytes,
        )
        await notifier.send_importer_info(
            file_name=csv_path.name,
            status="Abgelehnt (DoS-Schutz)",
            details=f"Überschreitet Limit ({file_size_bytes} > {config.app.max_csv_size_bytes} Bytes)",
            emoji="❌",
            title="INTEGRITÄTS-FEHLER",
        )
        return False
    return True


async def _process_and_upsert_group(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    trade_group_id: str,
    raw_legs: list,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """Validiert eine einzelne Gruppe, berechnet das Sizing und speichert sie in der DB."""
    is_valid, error_message = validate_group(trade_group_id, raw_legs)
    if not is_valid:
        logger.error(
            "Validation error in group. Skipping group.",
            trade_group_id=trade_group_id,
            error=error_message,
        )
        await notifier.send_importer_info(
            file_name=trade_group_id,
            status="Übersprungen",
            details=error_message,
            emoji="❌",
            title="VALIDIERUNGSFEHLER",
        )
        return

    entry_leg = next((leg for leg in raw_legs if leg.bracket_role == "ENTRY"), None)
    first_leg = raw_legs[0]
    account_id = entry_leg.account_id if entry_leg else first_leg.account_id

    target_quantity = first_leg.quantity
    if entry_leg:
        balance_metrics = await fetch_account_balance_metrics(
            interactive_brokers, account_id
        )

        strategy_limit_percentage = Decimal(str(config.account.default_limit_pct))
        strategy_name = entry_leg.strategy_name
        if strategy_name and strategy_name in config.strategy_limits:
            strategy_limit_percentage = Decimal(
                str(config.strategy_limits[strategy_name])
            )

        maximum_capital_allocation = determine_maximum_capital_allocation(
            net_liquidation_value=balance_metrics.net_liquidation_value,
            available_funds_value=balance_metrics.available_funds_value,
            total_cash_value=balance_metrics.total_cash_value,
            margin_multiplier_factor=Decimal(
                str(config.account.margin_multiplier_factor)
            ),
            sizing_mode=config.account.sizing_mode,
            allocation_limit_percentage=strategy_limit_percentage,
        )

        logger.info(
            "Capital sizing calculated",
            trade_group_id=trade_group_id,
            strategy=strategy_name,
            limit_percentage=float(strategy_limit_percentage),
            sizing_mode=config.account.sizing_mode,
            max_allocation=float(maximum_capital_allocation),
            original_quantity=entry_leg.quantity,
        )

        if maximum_capital_allocation <= Decimal("0.0"):
            logger.warning(
                "No available allocation capital. Skipping group.",
                trade_group_id=trade_group_id,
            )
            await notifier.send_importer_info(
                file_name=trade_group_id,
                status="Übersprungen",
                details=f"Kein verfuegbares Allokations-Kapital fuer Account {account_id}.",
                emoji="⚠️",
                title="KAPITAL-FEHLER",
            )
            return

        target_quantity = calculate_downscaled_quantity(
            target_quantity=entry_leg.quantity,
            target_price=entry_leg.target_price,
            max_allocation=maximum_capital_allocation,
        )

        if target_quantity <= 0:
            logger.warning(
                "Sizing resulted in Qty <= 0. Skipping group.",
                trade_group_id=trade_group_id,
                max_allocation=float(maximum_capital_allocation),
                target_price=float(entry_leg.target_price)
                if entry_leg.target_price
                else 0.0,
            )
            await notifier.send_importer_info(
                file_name=trade_group_id,
                status="Reduziert auf 0",
                details=f"Erforderliches Kapital überschreitet Limit ({float(maximum_capital_allocation):.2f}).",
                emoji="⚠️",
                title="SIZING-FEHLER",
            )
            return

    legs = [dataclasses.replace(leg, quantity=target_quantity) for leg in raw_legs]

    await _upsert_trade_group_legs(
        db, trade_group_id, account_id, entry_leg, legs, target_quantity, notifier
    )

    await queue.put(trade_group_id)


def calculate_downscaled_quantity(
    target_quantity: int,
    target_price: Decimal | None,
    max_allocation: Decimal,
) -> int:
    """
    Berechnet die reduzierte Positionsgröße basierend auf dem verfügbaren Kapitallimit.

    Pure Function: Keine I/O-Zugriffe, rein deterministisch und isoliert testbar.
    """
    if target_price is None or target_price <= Decimal("0.0"):
        return target_quantity

    entry_cost = Decimal(str(target_quantity)) * target_price
    if entry_cost <= max_allocation:
        return target_quantity

    quantity_adjusted = int(max_allocation // target_price)
    return max(0, quantity_adjusted)


async def _upsert_trade_group_legs(
    db: aiosqlite.Connection,
    trade_group_id: str,
    account_id: str,
    entry_leg: LegRow | None,
    legs: list[LegRow],
    target_quantity: int,
    notifier: TelegramNotifier,
) -> None:
    """Führt die atomaren INSERT/UPDATE (UPSERT) Operationen in der Datenbank aus."""
    try:
        async with transaction(db):
            entry_order_id = None

            # Prüfen ob dieser Entry bereits in der DB existiert
            async with db.execute(
                "SELECT order_id, status FROM orders WHERE account_id = ? AND trade_group_id = ? AND bracket_role = 'ENTRY'",
                (account_id, trade_group_id),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                entry_order_id = row["order_id"]
                existing_status = row["status"]
                if existing_status in ("Created", "Error"):
                    if entry_leg:
                        await db.execute(
                            """
                            UPDATE orders SET
                                symbol = ?, sec_type = ?, exchange = ?, action = ?,
                                quantity = ?, order_type = ?, target_price = ?, tif = ?,
                                strategy_name = ?, status = 'Created', retry_count = 0
                            WHERE order_id = ?
                            """,
                            (
                                entry_leg.symbol,
                                entry_leg.sec_type,
                                entry_leg.exchange,
                                entry_leg.action,
                                target_quantity,
                                entry_leg.order_type,
                                str(entry_leg.target_price)
                                if entry_leg.target_price is not None
                                else None,
                                entry_leg.tif,
                                entry_leg.strategy_name,
                                entry_order_id,
                            ),
                        )
                else:
                    logger.info(
                        "ENTRY order is already active/completed. Skipping ENTRY write/update.",
                        order_id=entry_order_id,
                        status=existing_status,
                    )
            elif entry_leg:
                entry_order_id = await get_next_temp_id(db)
                await db.execute(
                    """
                    INSERT INTO orders (
                        order_id, parent_id, trade_group_id, account_id, bracket_role,
                        symbol, sec_type, exchange, action, quantity, order_type,
                        target_price, tif, strategy_name, status, retry_count
                    ) VALUES (
                        ?, NULL, ?, ?, 'ENTRY',
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, 'Created', 0
                    )
                    """,
                    (
                        entry_order_id,
                        trade_group_id,
                        account_id,
                        entry_leg.symbol,
                        entry_leg.sec_type,
                        entry_leg.exchange,
                        entry_leg.action,
                        target_quantity,
                        entry_leg.order_type,
                        str(entry_leg.target_price)
                        if entry_leg.target_price is not None
                        else None,
                        entry_leg.tif,
                        entry_leg.strategy_name,
                    ),
                )
            else:
                raise ValueError(
                    "Standalone exit order imported, but no ENTRY order found in DB"
                )

            for leg in legs:
                if leg.bracket_role == "ENTRY":
                    continue

                async with db.execute(
                    "SELECT order_id, status FROM orders WHERE account_id = ? AND trade_group_id = ? AND bracket_role = ? AND order_type = ?",
                    (account_id, trade_group_id, leg.bracket_role, leg.order_type),
                ) as cursor:
                    child_row = await cursor.fetchone()

                if child_row:
                    child_order_id = child_row["order_id"]
                    existing_status = child_row["status"]
                    if existing_status in ("Created", "Error", "Cancelled"):
                        await db.execute(
                            """
                            UPDATE orders SET
                                parent_id = ?, symbol = ?, sec_type = ?, exchange = ?, action = ?,
                                quantity = ?, order_type = ?, target_price = ?, tif = ?,
                                strategy_name = ?, status = 'Created', retry_count = 0
                            WHERE order_id = ?
                            """,
                            (
                                entry_order_id,
                                leg.symbol,
                                leg.sec_type,
                                leg.exchange,
                                leg.action,
                                target_quantity,
                                leg.order_type,
                                str(leg.target_price)
                                if leg.target_price is not None
                                else None,
                                leg.tif,
                                leg.strategy_name,
                                child_order_id,
                            ),
                        )
                else:
                    child_order_id = await get_next_temp_id(db)
                    await db.execute(
                        """
                        INSERT INTO orders (
                            order_id, parent_id, trade_group_id, account_id, bracket_role,
                            symbol, sec_type, exchange, action, quantity, order_type,
                            target_price, tif, strategy_name, status, retry_count
                        ) VALUES (
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, 'Created', 0
                        )
                        """,
                        (
                            child_order_id,
                            entry_order_id,
                            trade_group_id,
                            account_id,
                            leg.bracket_role,
                            leg.symbol,
                            leg.sec_type,
                            leg.exchange,
                            leg.action,
                            target_quantity,
                            leg.order_type,
                            str(leg.target_price)
                            if leg.target_price is not None
                            else None,
                            leg.tif,
                            leg.strategy_name,
                        ),
                    )

        logger.info(
            "Trade group successfully imported/updated in DB",
            trade_group_id=trade_group_id,
            qty=target_quantity,
        )

    except Exception as exception:
        logger.error(
            "Error during DB UPSERT of group",
            trade_group_id=trade_group_id,
            error=str(exception),
        )
        await notifier.send_importer_info(
            file_name=trade_group_id,
            status="Fehlgeschlagen",
            details=f"Error: {exception!s}",
            emoji="❌",
            title="DB-FEHLER",
        )
        raise exception


async def fetch_account_balance_metrics(
    interactive_brokers: IB, account_id: str
) -> AccountBalanceMetrics:
    """
    Fragt die wichtigsten Kontowerte (NetLiquidation, AvailableFunds, TotalCashValue)
    von Interactive Brokers ab. Versucht es zuerst aus dem Cache (accountValues()) und fällt bei Bedarf
    auf einen reqAccountSummary()-Aufruf zurück.
    """
    net_liquidation_value = Decimal("0.0")
    available_funds_value = Decimal("0.0")
    total_cash_value = Decimal("0.0")

    cache_values: dict[str, Decimal] = {}
    for account_value in interactive_brokers.accountValues():
        if not account_id or account_value.account == account_id:
            if account_value.tag in (
                "NetLiquidation",
                "AvailableFunds",
                "TotalCashValue",
            ):
                try:
                    cache_values[account_value.tag] = Decimal(str(account_value.value))
                except ValueError as exception:
                    logger.warning(
                        "Invalid account value found in TWS cache",
                        tag=account_value.tag,
                        raw_value=account_value.value,
                        error=str(exception),
                    )

    if len(cache_values) == 3:
        logger.info(
            "Account values successfully loaded from cache",
            account=account_id,
            net_liquidation=float(cache_values["NetLiquidation"]),
            available_funds=float(cache_values["AvailableFunds"]),
            total_cash_value=float(cache_values["TotalCashValue"]),
        )
        return AccountBalanceMetrics(
            net_liquidation_value=cache_values["NetLiquidation"],
            available_funds_value=cache_values["AvailableFunds"],
            total_cash_value=cache_values["TotalCashValue"],
        )

    logger.info(
        "Some account values not in cache. Calling reqAccountSummary.",
        account=account_id,
    )
    summary_event = asyncio.Event()
    retrieved_values: dict[str, Decimal] = {}

    def on_account_summary(
        request_id: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        if tag in ("NetLiquidation", "AvailableFunds", "TotalCashValue") and (
            not account_id or account == account_id
        ):
            try:
                retrieved_values[tag] = Decimal(str(value))
                if len(retrieved_values) == 3:
                    summary_event.set()
            except ValueError as exception:
                logger.warning(
                    "Invalid value found in Account Summary callback",
                    tag=tag,
                    raw_value=value,
                    error=str(exception),
                )

    interactive_brokers.accountSummaryEvent.connect(on_account_summary)
    interactive_brokers.reqAccountSummary()

    try:
        await asyncio.wait_for(summary_event.wait(), timeout=10.0)
        logger.info(
            "Account values loaded via reqAccountSummary",
            account=account_id,
            net_liquidation=float(
                retrieved_values.get("NetLiquidation", Decimal("0.0"))
            ),
            available_funds=float(
                retrieved_values.get("AvailableFunds", Decimal("0.0"))
            ),
            total_cash_value=float(
                retrieved_values.get("TotalCashValue", Decimal("0.0"))
            ),
        )
    except TimeoutError:
        logger.warning(
            "Timeout waiting for reqAccountSummary. Using incomplete or default values."
        )
    finally:
        interactive_brokers.accountSummaryEvent.disconnect(on_account_summary)
        interactive_brokers.cancelAccountSummary()

    return AccountBalanceMetrics(
        net_liquidation_value=retrieved_values.get(
            "NetLiquidation", net_liquidation_value
        ),
        available_funds_value=retrieved_values.get(
            "AvailableFunds", available_funds_value
        ),
        total_cash_value=retrieved_values.get("TotalCashValue", total_cash_value),
    )


def determine_maximum_capital_allocation(
    net_liquidation_value: Decimal,
    available_funds_value: Decimal,
    total_cash_value: Decimal,
    margin_multiplier_factor: Decimal,
    sizing_mode: str,
    allocation_limit_percentage: Decimal,
) -> Decimal:
    """
    Berechnet das maximale Kapitalallokationslimit fuer eine Order.

    Pure Function: Keine Seiteneffekte, rein deterministisch.
    """
    if sizing_mode == "total_cash":
        return total_cash_value

    # Bei Margin-Modus:
    # 1. Berechne theoretisches Limit basierend auf dem Gesamtkapital (Net Liquidation Value)
    margin_adjusted_limit = (
        net_liquidation_value * margin_multiplier_factor * allocation_limit_percentage
    )

    # 2. Begrenze es auf das tatsaechlich verfuegbare Kapital (AvailableFunds) unter Beruecksichtigung des Margin-Faktors,
    #    um eine Ablehnung durch TWS wegen mangelnder Deckung zu vermeiden.
    maximum_buying_power_limit = available_funds_value * margin_multiplier_factor

    return min(margin_adjusted_limit, maximum_buying_power_limit)


async def get_next_temp_id(db: aiosqlite.Connection) -> int:
    """Generiert eine neue negative temporäre ID zur Vermeidung von TWS-ID-Kollisionen."""
    async with db.execute("SELECT MIN(order_id) FROM orders") as cursor:
        row = await cursor.fetchone()
        value = row[0] if row and row[0] is not None else 0
        return min(value, 0) - 1
