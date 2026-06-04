# ruff: noqa: E402
import asyncio
import signal
import socket
import sys
from collections.abc import Awaitable, Callable
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
            _enable_socket_keepalive(ib)
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
    async def trigger_settlement_callback(trade_group_id: str, account_id: str) -> None:
        await trigger_settlement(db_factory, trade_group_id, account_id, notifier)

    async def handle_retriable_error_callback(order_id: int) -> None:
        await handle_retriable_error(db_factory, order_id, queue, notifier, config)

    async def run_recovery_callback() -> None:
        database_conn = await db_factory()
        try:
            await run_recovery(
                database_conn, ib, queue, notifier, trigger_settlement_callback, config
            )
        finally:
            await database_conn.close()

    is_reconnecting = False

    async def run_reconnect_callback() -> None:
        nonlocal is_reconnecting
        if is_reconnecting:
            logger.warning("Reconnection loop is already running. Skipping trigger.")
            return
        is_reconnecting = True
        try:
            await _execute_reconnect_loop(ib, config, notifier, run_recovery_callback)
        finally:
            is_reconnecting = False

    # 7. Callbacks registrieren
    callbacks_mgr = TwsCallbacksManager(
        db_factory=db_factory,
        ib=ib,
        notifier=notifier,
        config=config,
        trigger_settlement_callback=trigger_settlement_callback,
        handle_retriable_error_callback=handle_retriable_error_callback,
        run_recovery_callback=run_recovery_callback,
        run_reconnect_callback=run_reconnect_callback,
    )
    callbacks_mgr.register_all()

    # 8. Phase 2: Recovery-Phase ausführen (Zustandsabgleich)
    database_conn = await db_factory()
    try:
        await run_recovery(
            database_conn, ib, queue, notifier, trigger_settlement_callback, config
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
            trigger_settlement_callback=trigger_settlement_callback,
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


async def _execute_reconnect_loop(
    ib: IB,
    config: Config,
    notifier: TelegramNotifier,
    recovery_callback: Callable[[], Awaitable[None]],
) -> None:
    """Führt die Wiederverbindungsschleife mit steigenden Intervallen aus."""
    logger.info("Starte automatischen Wiederverbindungsaufbau...")
    delays = [30.0, 60.0, 120.0, 240.0]
    attempt = 1
    max_attempts = config.tws.reconnect_max_attempts

    while attempt <= max_attempts:
        current_delay = delays[min(attempt - 1, len(delays) - 1)]
        logger.info(
            "Warte vor Wiederverbindungsversuch",
            attempt=attempt,
            delay_seconds=current_delay,
        )
        await asyncio.sleep(current_delay)

        if ib.isConnected():
            logger.info("Bereits verbunden. Beende Reconnect-Schleife.")
            return

        success = await _attempt_single_reconnect(ib, config, attempt)
        if success:
            logger.info("Wiederverbindung erfolgreich hergestellt!")
            await notifier.send_message(
                "✅ WIEDERVERBUNDEN: Die Verbindung zur Interactive Brokers TWS wurde erfolgreich wiederhergestellt."
            )
            ib.reqAutoOpenOrders(True)
            logger.info("Trigger Recovery-Lauf nach Wiederverbindung...")
            await recovery_callback()
            return

        attempt += 1

    logger.critical(
        f"Wiederverbindung nach {max_attempts} Versuchen fehlgeschlagen. Anwendung bleibt getrennt."
    )
    await notifier.send_message(
        f"🚨 WIEDERVERBINDUNG FEHLGESCHLAGEN: Die Verbindung konnte nach {max_attempts} Versuchen nicht wiederhergestellt werden."
    )


async def _attempt_single_reconnect(ib: IB, config: Config, attempt: int) -> bool:
    """Führt einen einzelnen Verbindungsversuch zur TWS durch."""
    logger.info(
        "Versuche Wiederverbindung",
        attempt=attempt,
        host=config.tws.host,
        port=config.tws.port,
    )
    try:
        await asyncio.wait_for(
            ib.connectAsync(
                config.tws.host,
                config.tws.port,
                clientId=config.tws.client_id
            ),
            timeout=config.tws.connection_timeout_s
        )
        _enable_socket_keepalive(ib)
        return True
    except Exception as exception:
        logger.warning(
            "Wiederverbindung-Verbindungsversuch fehlgeschlagen",
            attempt=attempt,
            error=str(exception)
        )
        return False


def _enable_socket_keepalive(ib: IB) -> None:
    """Aktiviert TCP-Keep-Alive auf dem TWS-Verbindungs-Socket zur Vermeidung von Timeouts."""
    if not ib.isConnected():
        return

    try:
        # Socket über den asyncio Transport abrufen
        socket_object = ib.client.conn.transport.get_extra_info("socket")
        if socket_object:
            # 1. SO_KEEPALIVE aktivieren
            socket_object.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            # 2. Idle-Zeit einstellen (z. B. 60 Sekunden Inaktivität)
            # macOS verwendet TCP_KEEPALIVE, Linux/Windows verwendet TCP_KEEPIDLE
            tcp_keepidle = getattr(socket, "TCP_KEEPALIVE", None) or getattr(
                socket, "TCP_KEEPIDLE", None
            )
            if tcp_keepidle is not None:
                socket_object.setsockopt(socket.IPPROTO_TCP, tcp_keepidle, 60)

            # 3. Intervall einstellen (z. B. 10 Sekunden zwischen Probes)
            tcp_keepintvl = getattr(socket, "TCP_KEEPINTVL", None)
            if tcp_keepintvl is not None:
                socket_object.setsockopt(socket.IPPROTO_TCP, tcp_keepintvl, 10)

            # 4. Maximale Anzahl der Fehlversuche einstellen (z. B. 5 Probes)
            tcp_keepcnt = getattr(socket, "TCP_KEEPCNT", None)
            if tcp_keepcnt is not None:
                socket_object.setsockopt(socket.IPPROTO_TCP, tcp_keepcnt, 5)

            logger.info("TCP-Keep-Alive-Einstellungen erfolgreich auf Socket angewendet")
    except Exception as exception:
        logger.warning(
            "Fehler beim Aktivieren von TCP-Keep-Alive auf dem Socket",
            error=str(exception),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
