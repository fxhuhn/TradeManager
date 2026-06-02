# ruff: noqa: E402
import asyncio
import signal
import sys
from pathlib import Path

# Sicherstellen, dass das Root-Verzeichnis im PYTHONPATH ist, wenn das Skript direkt gestartet wird
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import structlog
from ib_async import IB

from app.core.config import Config, load_config
from app.core.db import get_db, run_migrations, verify_db_integrity
from app.core.logging_setup import configure_logging
from app.services.alert_watcher import alert_watcher, order_status_sync_loop
from app.services.importer import csv_directory_watcher
from app.services.notifier import TelegramNotifier
from app.trading.callbacks import TwsCallbacksManager
from app.trading.recovery import run_recovery
from app.trading.retry import handle_retriable_error
from app.trading.settlement import trigger_settlement
from app.trading.worker import execution_worker

# Logger konfigurieren vor jeglichem anderen Import
configure_logging()
logger = structlog.get_logger()


async def connect_to_tws(ib: IB, config: Config) -> bool:
    """
    Verbindung zu TWS mit exponentiellem Backoff aufbauen.
    Nutzt die zentral in der Konfiguration definierten Reconnect-Parameter.
    """
    attempt = 1
    delay = config.tws.reconnect_initial_delay_s
    max_attempts = config.tws.reconnect_max_attempts

    while attempt <= max_attempts:
        logger.info(
            "Verbindungsaufbau zu TWS gestartet",
            attempt=attempt,
            host=config.tws.host,
            port=config.tws.port,
            client_id=config.tws.client_id,
        )
        try:
            # Verbindung mit konfiguriertem Connection-Timeout absichern
            await asyncio.wait_for(
                ib.connectAsync(
                    config.tws.host, config.tws.port, clientId=config.tws.client_id
                ),
                timeout=config.tws.connection_timeout_s,
            )
            logger.info("Erfolgreich mit TWS-Plattform verbunden")
            return True
        except Exception as exception:
            logger.warning(
                "Verbindung fehlgeschlagen", attempt=attempt, error=str(exception)
            )
            if attempt == max_attempts:
                break

            logger.info("Warte vor erneutem Verbindungsversuch", delay_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, config.tws.reconnect_max_delay_s)
            attempt += 1

    logger.critical(
        f"Verbindung zu TWS nach {max_attempts} Versuchen unmoeglich. Beende Anwendung."
    )
    return False


async def main() -> None:
    """Zentraler Orchestrierungs- und Lebenszyklus-Einstiegspunkt des Systems."""
    logger.info("Starte IBKR Equities Trading System (Release 1)")

    # 1. Konfiguration laden
    root_dir = Path(__file__).resolve().parent.parent
    try:
        config = load_config(root_dir)
        # 1b. Logger mit Pfad und Rotationsanzahl aus Config re-konfigurieren
        configure_logging(
            log_file_path=root_dir / config.app.log_file_path,
            backup_count=config.app.log_rotation_backup_count,
        )
        logger.info("Konfiguration erfolgreich geladen und Logging re-konfiguriert")
    except Exception as exception:
        logger.critical(
            "Schwerer Fehler beim Laden der Konfiguration", error=str(exception)
        )
        sys.exit(1)

    # 2. Telegram Notifier initialisieren
    notifier = TelegramNotifier(config)
    await notifier.send_message("🚀 Trading System startet...")

    # 3. Datenbank-Integrity Check
    db_path = root_dir / "data" / "trading.db"
    is_db_ok = await verify_db_integrity(db_path)
    if not is_db_ok:
        logger.critical(
            "DB-Integritaetspruefung fehlgeschlagen. Beende zur Sicherheit."
        )
        await notifier.send_message(
            "🚨 KRITISCH: DB-Integritaetspruefung fehlgeschlagen! Anwendung beendet."
        )
        sys.exit(1)

    # 4. Verbindung zu TWS aufbauen
    ib = IB()
    is_connected = await connect_to_tws(ib, config)
    if not is_connected:
        sys.exit(1)

    # DB-Verbindungs-Hilfsfunktion (Factory)
    async def db_factory() -> get_db:
        return await get_db(db_path)

    # 5. DB-Verbindung oeffnen und Migrationen ausfuehren
    database_connection = await db_factory()
    try:
        migrations_dir = root_dir / "migrations"
        await run_migrations(database_connection, migrations_dir)
    except Exception as exception:
        logger.critical(
            "Fehler beim Ausführen der DB-Migrationen", error=str(exception)
        )
        await ib.disconnect()
        await database_connection.close()
        sys.exit(1)
    finally:
        await database_connection.close()

    # 6. reqAutoOpenOrders aktivieren
    # Erfordert Client ID 0. Bindet manuell in TWS aufgegebene Orders
    ib.reqAutoOpenOrders(True)

    # Asynchrone Queue für Trade-Gruppen initialisieren
    queue: asyncio.Queue = asyncio.Queue()

    # Lokale Helfer-Funktionen zur Vermeidung zirkulärer Importe
    async def trigger_settlement_cb(trade_group_id: str, account_id: str) -> None:
        await trigger_settlement(db_factory, trade_group_id, account_id, notifier)

    async def handle_retriable_error_cb(order_id: int) -> None:
        await handle_retriable_error(db_factory, order_id, queue, notifier, config)

    async def run_recovery_cb() -> None:
        database_conn = await db_factory()
        try:
            await run_recovery(
                database_conn, ib, queue, notifier, trigger_settlement_cb, config
            )
        finally:
            await database_conn.close()

    # 7. Callbacks registrieren
    callbacks_mgr = TwsCallbacksManager(
        db_factory=db_factory,
        ib=ib,
        notifier=notifier,
        config=config,
        trigger_settlement_cb=trigger_settlement_cb,
        handle_retriable_error_cb=handle_retriable_error_cb,
        run_recovery_cb=run_recovery_cb,
    )
    callbacks_mgr.register_all()

    # 8. Phase 2: Recovery-Phase ausführen (Zustandsabgleich)
    database_conn = await db_factory()
    try:
        await run_recovery(
            database_conn, ib, queue, notifier, trigger_settlement_cb, config
        )
    finally:
        await database_conn.close()

    # 9. Phase 3: CSV-Import Hintergrunddienst (Dauerbetrieb) starten
    importer_task = asyncio.create_task(
        csv_directory_watcher(
            db_factory=db_factory,
            ib=ib,
            directory_path=root_dir / "data",
            queue=queue,
            notifier=notifier,
            config=config,
            interval_seconds=config.app.csv_watcher_interval_s,
        )
    )

    # 10. Dauerbetrieb Hintergrunddienste starten
    worker_task = asyncio.create_task(
        execution_worker(db_factory, ib, queue, notifier, config)
    )
    watcher_task = asyncio.create_task(
        alert_watcher(
            db_factory=db_factory,
            notifier=notifier,
            config=config,
            interval_seconds=config.app.alert_watcher_interval_s,
            dead_order_threshold_min=config.app.dead_order_threshold_min,
            max_slippage_pct=config.account.default_limit_pct,
        )
    )
    sync_task = asyncio.create_task(
        order_status_sync_loop(
            db_factory=db_factory,
            ib=ib,
            queue=queue,
            notifier=notifier,
            trigger_settlement_cb=trigger_settlement_cb,
            config=config,
            interval_seconds=config.app.order_sync_interval_s,
        )
    )

    # Graceful Shutdown Event registrieren
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def signal_handler() -> None:
        logger.info("Signal empfangen. Starte geordneten Shutdown...")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Fallback für Plattformen ohne Signal-Handler Support
            pass

    # Auf Beendigungssignal warten
    await shutdown_event.wait()

    # --- GRACEFUL SHUTDOWN SEQUENZ ---
    logger.info("Beginne Shutdown-Sequenz...")
    await notifier.send_message("⚠️ Trading System wird heruntergefahren...")

    # A1. Hintergrunddienste abbrechen
    logger.info("Breche Hintergrunddienste ab...")
    importer_task.cancel()
    worker_task.cancel()
    watcher_task.cancel()
    sync_task.cancel()

    try:
        await asyncio.gather(
            importer_task, worker_task, watcher_task, sync_task, return_exceptions=True
        )
    except Exception as exception:
        logger.debug("Fehler beim Abbrechen der Tasks", error=str(exception))

    # A2. Queue vollständig leeren (blockiert bis alle task_done() Aufrufe abgeschlossen sind)
    logger.info("Warte bis alle Queue-Tasks abgeschlossen sind (queue.join)...")
    try:
        await asyncio.wait_for(queue.join(), timeout=config.app.shutdown_join_timeout_s)
        logger.info("Queue erfolgreich geleert")
    except TimeoutError:
        logger.warning("Timeout beim Warten auf das Leeren der Queue. Fahre fort.")

    # A3. TWS Verbindung trennen
    if ib.isConnected():
        logger.info("Trenne API-Verbindung zur TWS...")
        ib.disconnect()
        logger.info("Verbindung getrennt")

    logger.info("Shutdown-Sequenz erfolgreich abgeschlossen. Auf Wiedersehen!")
    await notifier.send_message("🛑 Trading System geordnet heruntergefahren.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
