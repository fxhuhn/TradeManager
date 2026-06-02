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


async def fetch_total_cash_value(ib: IB, account_id: str) -> Decimal:
    """
    Fragt das gesamte Cash-Guthaben von TWS ab.
    Versucht es zuerst aus dem Cache (ib.accountValues()),
    und faellt bei Bedarf auf einen reqAccountSummary()-Aufruf zurueck.
    """
    funds = Decimal("0.0")

    # 1. Versuch: Cache
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
            except ValueError:
                pass

    # 2. Versuch: Active Fallback via reqAccountSummary
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
            except ValueError:
                pass

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
    """
    Generiert eine neue negative temporäre ID, um Kollisionen mit TWS-OrderIDs (die positiv sind)
    zu vermeiden.
    """
    async with db.execute("SELECT MIN(order_id) FROM orders") as cursor:
        row = await cursor.fetchone()
        value = row[0] if row and row[0] is not None else 0
        return min(value, 0) - 1


async def run_csv_import(
    db: aiosqlite.Connection,
    ib: IB,
    csv_path: Path,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Führt Phase 3 aus: CSV einlesen, validieren, Kapital prüfen,
    symmetrisch downscalen, atomar in DB schreiben und in die Queue einreihen.
    Integrationsprüfungen gegen Ressourcen-Erschöpfung (DoS-Check).
    """
    logger.info("Starte CSV-Import", file=str(csv_path))

    # 1. DoS Ressourcenschutz-Check
    if csv_path.exists():
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
            return

    # 2. CSV laden und gruppieren
    grouped_legs = load_csv(csv_path)
    if not grouped_legs:
        logger.info("Keine Trade-Gruppen zum Importieren gefunden")
        return

    for trade_group_id, raw_legs in grouped_legs.items():
        # 3. Gruppe validieren
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
            continue

        entry_leg = next(leg for leg in raw_legs if leg.bracket_role == "ENTRY")
        account_id = entry_leg.account_id

        # 4. TotalCashValue abfragen (hochpräzise als Decimal)
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
            continue

        # 5. Risk-Limit umgehen: 100% des TotalCashValue ist die Allokationsgrenze
        max_allocation = total_cash_value

        # 6. Positionsgröße berechnen und downscalen
        target_quantity = entry_leg.quantity
        if entry_leg.target_price and entry_leg.target_price > Decimal("0.0"):
            entry_cost = Decimal(str(target_quantity)) * entry_leg.target_price
            if entry_cost > max_allocation:
                # Ganzzahl-Division auf Decimal zur mathematisch exakten Abrundung
                quantity_adjusted = int(max_allocation // entry_leg.target_price)
                if quantity_adjusted <= 0:
                    logger.warning(
                        "Sizing ergab Qty <= 0. Überspringe Gruppe.",
                        trade_group_id=trade_group_id,
                        max_allocation=float(max_allocation),
                        target_price=float(entry_leg.target_price),
                    )
                    await notifier.send_message(
                        f"⚠️ SIZING-FEHLER ({trade_group_id}): Erforderliches Kapital überschreitet Limit "
                        f"({float(max_allocation):.2f}). Reduzierte Menge = 0. Gruppe uebersprungen."
                    )
                    continue

                logger.info(
                    "Downscaling angewendet",
                    trade_group_id=trade_group_id,
                    old_qty=target_quantity,
                    new_qty=quantity_adjusted,
                    reason="Capital Limit ueberschritten",
                )
                target_quantity = quantity_adjusted

        # Die berechnete/angepasste Menge symmetrisch auf alle Legs anwenden
        # Da LegRow frozen=True ist, rekonstruieren wir die Dataclasses mit der neuen Menge:
        import dataclasses

        legs = [dataclasses.replace(leg, quantity=target_quantity) for leg in raw_legs]
        # Now legs is a list of reconstructed LegRows with updated quantities! That is extremely elegant!

        # 7. Atomarer UPSERT in die DB
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
                    logger.debug(
                        "ENTRY-Order aktualisiert",
                        order_id=entry_order_id,
                        trade_group_id=trade_group_id,
                    )
                else:
                    logger.info(
                        "ENTRY-Order ist bereits aktiv/abgeschlossen. Überspringe DB-Write.",
                        order_id=entry_order_id,
                        status=existing_status,
                    )
                    await db.execute("ROLLBACK")
                    continue
            else:
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
                logger.debug(
                    "ENTRY-Order neu angelegt",
                    order_id=entry_order_id,
                    trade_group_id=trade_group_id,
                )

            # 7b. Jetzt alle anderen Legs (SL, TP, EXIT) mit parent_id = entry_order_id upserten
            for leg in legs:
                if leg.bracket_role == "ENTRY":
                    continue

                async with db.execute(
                    "SELECT order_id, status FROM orders WHERE account_id = ? AND trade_group_id = ? AND bracket_role = ?",
                    (account_id, trade_group_id, leg.bracket_role),
                ) as cursor:
                    row = await cursor.fetchone()

                if row:
                    child_order_id = row["order_id"]
                    existing_status = row["status"]
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

            # 8. In asyncio.Queue einreihen
            await queue.put(trade_group_id)

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
    Hintergrunddienst zur kontinuierlichen Ueberwachung eines Verzeichnisses.

    Sucht nach Dateien des Musters 'orders_YYYY_MM_DD.csv', validiert diese und
    importiert sie. Erfolgreiche CSV-Dateien werden in '.csv.bak' umbenannt.
    Fehlerhafte Dateien werden in '.csv.err' umbenannt, um Endlosschleifen zu verhindern.
    """
    logger.info(
        "Starte CSV Directory Watcher Hintergrunddienst",
        directory=str(directory_path),
        interval=interval_seconds,
    )

    # Regex-Muster fuer dateibasierte Orders (z.B. orders_2026_06_01.csv)
    date_pattern = re.compile(r"^orders_\d{4}_\d{2}_\d{2}\.csv$")

    while True:
        try:
            if directory_path.exists() and directory_path.is_dir():
                # Finde alle orders_*.csv Dateien im Verzeichnis
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


async def _process_daily_csv_file(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    ib: IB,
    csv_file: Path,
    queue: asyncio.Queue,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Verarbeitet eine einzelne gefundene CSV-Datei und benennt sie anschliessend um.
    """
    logger.info("Neue Order-Datei erkannt", file=csv_file.name)

    database_connection = await db_factory()
    try:
        # Import durchführen
        await run_csv_import(database_connection, ib, csv_file, queue, notifier, config)

        # Erfolgsfall: Umbenennen nach .csv.bak
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

        # Fehlerfall: Umbenennen nach .csv.err zur Vermeidung von Endlosschleifen
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
