"""
Import- und Directory-Watcher-Dienste für Order-CSV-Dateien.

Überwacht ein konfiguriertes Verzeichnis nach CSV-Dateien mit Orders,
validiert deren Inhalt, führt Kapitalallokation und Downscaling durch,
speichert die Orders in der Datenbank und reiht sie in die Execution Queue ein.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
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
        "Starte CSV Directory Watcher Hintergrunddienst",
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
            logger.info("CSV Directory Watcher wurde abgebrochen.")
            raise
        except Exception as exception:
            logger.error("Fehler im CSV Directory Watcher Loop", error=str(exception))

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
    logger.info("Starte CSV-Import", file=str(csv_path))

    # 1. DoS Ressourcenschutz-Check
    if not await _check_csv_dos_limits(csv_path, config, notifier):
        return

    # 2. CSV laden und gruppieren
    grouped_legs = load_csv(csv_path)
    if not grouped_legs:
        logger.info("Keine Trade-Gruppen zum Importieren gefunden")
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
    logger.info("Neue Order-Datei erkannt", file=csv_file.name)

    database_connection = await db_factory()
    try:
        await run_csv_import(
            database_connection, interactive_brokers, csv_file, queue, notifier, config
        )

        backup_path = csv_file.with_name(csv_file.name + ".bak")
        csv_file.rename(backup_path)

        logger.info(
            "Order-Datei erfolgreich verarbeitet und umbenannt",
            file=csv_file.name,
            backup=backup_path.name,
        )
        await notifier.send_message(
            f"✅ DATEI IMPORTIERT: Die Datei `{csv_file.name}` wurde erfolgreich "
            f"eingelesen und nach `.bak` archiviert."
        )

    except Exception as exception:
        logger.error(
            "Fehler bei der Verarbeitung der Order-Datei",
            file=csv_file.name,
            error=str(exception),
        )

        error_path = csv_file.with_name(csv_file.name + ".err")
        try:
            csv_file.rename(error_path)
            logger.warning(
                "Fehlerhafte Order-Datei umbenannt",
                file=csv_file.name,
                error_file=error_path.name,
            )
            await notifier.send_message(
                f"🚨 IMPORT-FEHLER: Die Datei `{csv_file.name}` schlug fehl und wurde nach `.err` umbenannt. "
                f"Fehler: {str(exception)}"
            )
        except Exception as rename_exception:
            logger.critical(
                "Kritischer Fehler beim Umbenennen einer fehlerhaften Datei",
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
            "CSV-Datei ueberschreitet Sicherheitslimit. Import verweigert.",
            size=file_size_bytes,
            max_size=config.app.max_csv_size_bytes,
        )
        await notifier.send_message(
            f"❌ INTEGRITÄTS-FEHLER: CSV-Datei ({file_size_bytes} Bytes) überschreitet das "
            f"Sicherheitslimit von {config.app.max_csv_size_bytes} Bytes. Import abgelehnt."
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
            "Validierungsfehler in Gruppe. Überspringe Gruppe.",
            trade_group_id=trade_group_id,
            error=error_message,
        )
        await notifier.send_message(
            f"❌ VALIDIERUNGSFEHLER ({trade_group_id}): {error_message}. Gruppe wurde uebersprungen."
        )
        return

    entry_leg = next((leg for leg in raw_legs if leg.bracket_role == "ENTRY"), None)
    first_leg = raw_legs[0]
    account_id = entry_leg.account_id if entry_leg else first_leg.account_id

    target_quantity = first_leg.quantity
    if entry_leg:
        balance_metrics = await fetch_account_balance_metrics(interactive_brokers, account_id)

        strategy_limit_percentage = Decimal(str(config.account.default_limit_pct))
        strategy_name = entry_leg.strategy_name
        if strategy_name and strategy_name in config.strategy_limits:
            strategy_limit_percentage = Decimal(str(config.strategy_limits[strategy_name]))

        maximum_capital_allocation = determine_maximum_capital_allocation(
            net_liquidation_value=balance_metrics.net_liquidation_value,
            available_funds_value=balance_metrics.available_funds_value,
            total_cash_value=balance_metrics.total_cash_value,
            margin_multiplier_factor=Decimal(str(config.account.margin_multiplier_factor)),
            sizing_mode=config.account.sizing_mode,
            allocation_limit_percentage=strategy_limit_percentage,
        )

        logger.info(
            "Kapital-Sizing berechnet",
            trade_group_id=trade_group_id,
            strategy=strategy_name,
            limit_percentage=float(strategy_limit_percentage),
            sizing_mode=config.account.sizing_mode,
            max_allocation=float(maximum_capital_allocation),
            original_quantity=entry_leg.quantity,
        )

        if maximum_capital_allocation <= Decimal("0.0"):
            logger.warning(
                "Kein verfuegbares Allokations-Kapital vorhanden. Überspringe Gruppe.",
                trade_group_id=trade_group_id,
            )
            await notifier.send_message(
                f"⚠️ KAPITAL-FEHLER ({trade_group_id}): Kein verfuegbares Allokations-Kapital fuer Account {account_id}. "
                f"Gruppe uebersprungen."
            )
            return

        target_quantity = calculate_downscaled_quantity(
            target_quantity=entry_leg.quantity,
            target_price=entry_leg.target_price,
            max_allocation=maximum_capital_allocation,
        )

        if target_quantity <= 0:
            logger.warning(
                "Sizing ergab Qty <= 0. Überspringe Gruppe.",
                trade_group_id=trade_group_id,
                max_allocation=float(maximum_capital_allocation),
                target_price=float(entry_leg.target_price)
                if entry_leg.target_price
                else 0.0,
            )
            await notifier.send_message(
                f"⚠️ SIZING-FEHLER ({trade_group_id}): Erforderliches Kapital überschreitet Limit "
                f"({float(maximum_capital_allocation):.2f}). Reduzierte Menge = 0. Gruppe uebersprungen."
            )
            return

    import dataclasses

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
    entry_leg: getattr(validate_group, "__annotations__", dict).get("legs") or None,
    legs: list,
    target_quantity: int,
    notifier: TelegramNotifier,
) -> None:
    """Führt die atomaren INSERT/UPDATE (UPSERT) Operationen in der Datenbank aus."""
    await db.execute("BEGIN IMMEDIATE")
    try:
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
                            strategy_name = ?
                        WHERE order_id = ?
                        """,
                        (
                            entry_leg.symbol,
                            entry_leg.sec_type,
                            entry_leg.exchange,
                            entry_leg.action,
                            target_quantity,
                            entry_leg.order_type,
                            float(entry_leg.target_price)
                            if entry_leg.target_price is not None
                            else None,
                            entry_leg.tif,
                            entry_leg.strategy_name,
                            entry_order_id,
                        ),
                    )
            else:
                logger.info(
                    "ENTRY-Order ist bereits aktiv/abgeschlossen. Überspringe ENTRY-Schreiben/Update.",
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
                    float(entry_leg.target_price)
                    if entry_leg.target_price is not None
                    else None,
                    entry_leg.tif,
                    entry_leg.strategy_name,
                ),
            )
        else:
            logger.error(
                "Reine Exit-Order importiert, aber kein ENTRY-Auftrag in der DB gefunden",
                trade_group_id=trade_group_id,
            )
            await db.execute("ROLLBACK")
            await notifier.send_message(
                f"❌ IMPORT-FEHLER ({trade_group_id}): Exit-Order importiert, aber kein passender ENTRY-Auftrag in der DB gefunden."
            )
            return

        for leg in legs:
            if leg.bracket_role == "ENTRY":
                continue

            async with db.execute(
                "SELECT order_id, status FROM orders WHERE account_id = ? AND trade_group_id = ? AND bracket_role = ?",
                (account_id, trade_group_id, leg.bracket_role),
            ) as cursor:
                child_row = await cursor.fetchone()

            if child_row:
                child_order_id = child_row["order_id"]
                existing_status = child_row["status"]
                if existing_status in ("Created", "Error"):
                    await db.execute(
                        """
                        UPDATE orders SET
                            parent_id = ?, symbol = ?, sec_type = ?, exchange = ?, action = ?,
                            quantity = ?, order_type = ?, target_price = ?, tif = ?,
                            strategy_name = ?
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
                            float(leg.target_price)
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
                        float(leg.target_price)
                        if leg.target_price is not None
                        else None,
                        leg.tif,
                        leg.strategy_name,
                    ),
                )

        await db.execute("COMMIT")
        logger.info(
            "Trade-Gruppe erfolgreich in DB importiert/aktualisiert",
            trade_group_id=trade_group_id,
            qty=target_quantity,
        )

    except Exception as exception:
        await db.execute("ROLLBACK")
        logger.error(
            "Fehler beim DB-UPSERT der Gruppe",
            trade_group_id=trade_group_id,
            error=str(exception),
        )
        await notifier.send_message(
            f"❌ DB-FEHLER ({trade_group_id}): Import fehlgeschlagen. Error: {str(exception)}"
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
            if account_value.tag in ("NetLiquidation", "AvailableFunds", "TotalCashValue"):
                try:
                    cache_values[account_value.tag] = Decimal(str(account_value.value))
                except ValueError as exception:
                    logger.warning(
                        "Ungueltiger Kontowert im TWS-Cache gefunden",
                        tag=account_value.tag,
                        raw_value=account_value.value,
                        error=str(exception),
                    )

    if len(cache_values) == 3:
        logger.info(
            "Kontowerte erfolgreich aus Cache geladen",
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
        "Einige Kontowerte nicht im Cache. Rufe reqAccountSummary auf.",
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
                    "Ungueltiger Wert in Account-Summary-Callback gefunden",
                    tag=tag,
                    raw_value=value,
                    error=str(exception),
                )

    interactive_brokers.accountSummaryEvent.connect(on_account_summary)
    interactive_brokers.reqAccountSummary()

    try:
        await asyncio.wait_for(summary_event.wait(), timeout=10.0)
        logger.info(
            "Kontowerte via reqAccountSummary geladen",
            account=account_id,
            net_liquidation=float(retrieved_values.get("NetLiquidation", Decimal("0.0"))),
            available_funds=float(retrieved_values.get("AvailableFunds", Decimal("0.0"))),
            total_cash_value=float(retrieved_values.get("TotalCashValue", Decimal("0.0"))),
        )
    except TimeoutError:
        logger.warning(
            "Timeout beim Warten auf reqAccountSummary. Nutze unvollstaendige oder Standardwerte."
        )
    finally:
        interactive_brokers.accountSummaryEvent.disconnect(on_account_summary)
        interactive_brokers.cancelAccountSummary()

    return AccountBalanceMetrics(
        net_liquidation_value=retrieved_values.get("NetLiquidation", net_liquidation_value),
        available_funds_value=retrieved_values.get("AvailableFunds", available_funds_value),
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
