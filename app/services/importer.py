"""
Import- und Directory-Watcher-Dienste für Order-CSV-Dateien.

Überwacht ein konfiguriertes Verzeichnis nach CSV-Dateien mit Orders,
validiert deren Inhalt, führt Kapitalallokation und Downscaling durch,
speichert die Orders in der Datenbank und reiht sie in die Execution Queue ein.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import aiosqlite
import structlog
from ib_async import IB

from app.core.config import Config
from app.services.csv_reader import load_csv, validate_group
from app.services.notifier import TelegramNotifier

logger = structlog.get_logger()


async def csv_directory_watcher(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    ib: IB,
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
                            db_factory, ib, csv_file, queue, notifier, config
                        )
        except asyncio.CancelledError:
            logger.info("CSV Directory Watcher wurde abgebrochen.")
            raise
        except Exception as exception:
            logger.error("Fehler im CSV Directory Watcher Loop", error=str(exception))

        await asyncio.sleep(interval_seconds)


async def run_csv_import(
    db: aiosqlite.Connection,
    ib: IB,
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
            ib=ib,
            trade_group_id=trade_group_id,
            raw_legs=raw_legs,
            queue=queue,
            notifier=notifier,
            config=config,
        )


async def _process_daily_csv_file(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    ib: IB,
    csv_file: Path,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """Verarbeitet eine einzelne gefundene CSV-Datei und benennt sie anschließend um."""
    logger.info("Neue Order-Datei erkannt", file=csv_file.name)

    database_connection = await db_factory()
    try:
        await run_csv_import(database_connection, ib, csv_file, queue, notifier, config)

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
    ib: IB,
    trade_group_id: str,
    raw_legs: list,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """Validiert eine einzelne Gruppe, berechnet das Sizing und speichert sie in der DB."""
    is_valid, err_msg = validate_group(trade_group_id, raw_legs)
    if not is_valid:
        logger.error(
            "Validierungsfehler in Gruppe. Überspringe Gruppe.",
            trade_group_id=trade_group_id,
            error=err_msg,
        )
        await notifier.send_message(
            f"❌ VALIDIERUNGSFEHLER ({trade_group_id}): {err_msg}. Gruppe wurde uebersprungen."
        )
        return

    entry_leg = next((leg for leg in raw_legs if leg.bracket_role == "ENTRY"), None)
    first_leg = raw_legs[0]
    account_id = entry_leg.account_id if entry_leg else first_leg.account_id

    target_quantity = first_leg.quantity
    if entry_leg:
        total_cash_value = await fetch_total_cash_value(ib, account_id)
        if total_cash_value <= Decimal("0.0"):
            logger.warning(
                "Kein verfügbares Cash-Kapital vorhanden. Überspringe Gruppe.",
                trade_group_id=trade_group_id,
            )
            await notifier.send_message(
                f"⚠️ KAPITAL-FEHLER ({trade_group_id}): Kein verfuegbares Cash-Kapital fuer Account {account_id}. "
                f"Gruppe uebersprungen."
            )
            return

        target_quantity = calculate_downscaled_quantity(
            target_quantity=entry_leg.quantity,
            target_price=entry_leg.target_price,
            max_allocation=total_cash_value,
        )

        if target_quantity <= 0:
            logger.warning(
                "Sizing ergab Qty <= 0. Überspringe Gruppe.",
                trade_group_id=trade_group_id,
                max_allocation=float(total_cash_value),
                target_price=float(entry_leg.target_price) if entry_leg.target_price else 0.0,
            )
            await notifier.send_message(
                f"⚠️ SIZING-FEHLER ({trade_group_id}): Erforderliches Kapital überschreitet Limit "
                f"({float(total_cash_value):.2f}). Reduzierte Menge = 0. Gruppe uebersprungen."
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
                            float(entry_leg.target_price) if entry_leg.target_price is not None else None,
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
                    float(entry_leg.target_price) if entry_leg.target_price is not None else None,
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
                            float(leg.target_price) if leg.target_price is not None else None,
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
                        float(leg.target_price) if leg.target_price is not None else None,
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


async def fetch_total_cash_value(ib: IB, account_id: str) -> Decimal:
    """
    Fragt das gesamte Cash-Guthaben von TWS ab.

    Versucht es zuerst aus dem Cache (ib.accountValues()) und fällt bei Bedarf
    auf einen reqAccountSummary()-Aufruf zurück.
    """
    funds = Decimal("0.0")

    for account_value in ib.accountValues():
        if account_value.tag == "TotalCashValue" and (
            not account_id or account_value.account == account_id
        ):
            try:
                funds = Decimal(str(account_value.value))
                logger.info(
                    "TotalCashValue aus Cache geladen",
                    account=account_id,
                    funds=float(funds),
                )
                return funds
            except ValueError as exception:
                logger.warning(
                    "Ungueltiger Cash-Wert im TWS-Cache gefunden",
                    raw_value=account_value.value,
                    error=str(exception),
                )

    logger.info(
        "TotalCashValue nicht im Cache. Rufe reqAccountSummary auf.", account=account_id
    )
    summary_event = asyncio.Event()
    retrieved_funds: list[Decimal] = [Decimal("0.0")]

    def on_account_summary(
        request_id: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        if tag == "TotalCashValue" and (not account_id or account == account_id):
            try:
                retrieved_funds[0] = Decimal(str(value))
                summary_event.set()
            except ValueError as exception:
                logger.warning(
                    "Ungueltiger Cash-Wert in Account-Summary-Callback gefunden",
                    raw_value=value,
                    error=str(exception),
                )

    ib.accountSummaryEvent.connect(on_account_summary)
    ib.reqAccountSummary()

    try:
        await asyncio.wait_for(summary_event.wait(), timeout=10.0)
        funds = retrieved_funds[0]
        logger.info(
            "TotalCashValue via Fallback geladen",
            account=account_id,
            funds=float(funds),
        )
    except TimeoutError:
        logger.warning(
            "Timeout beim Warten auf reqAccountSummary. Setze standardmaessig 0.0"
        )
    finally:
        ib.accountSummaryEvent.disconnect(on_account_summary)
        ib.cancelAccountSummary()

    return funds


async def get_next_temp_id(db: aiosqlite.Connection) -> int:
    """Generiert eine neue negative temporäre ID zur Vermeidung von TWS-ID-Kollisionen."""
    async with db.execute("SELECT MIN(order_id) FROM orders") as cursor:
        row = await cursor.fetchone()
        value = row[0] if row and row[0] is not None else 0
        return min(value, 0) - 1
