# ruff: noqa: E402
"""
Haupt-Einstiegspunkt des IBKR Equities Trading Systems.

Initialisiert die Systemkomponenten (Konfiguration, Logging, Telegram-Notifier,
Datenbank-Integrität und Migrationen), stellt die Verbindung zur Trader Workstation (TWS)
von Interactive Brokers her, startet die Hintergrunddienste und steuert den graceful shutdown.
"""

import asyncio
import signal
import socket
import sys
from pathlib import Path

import aiosqlite

# Sicherstellen, dass das Hauptverzeichnis im PYTHONPATH ist, wenn das Skript direkt gestartet wird
root_directory = str(Path(__file__).resolve().parent.parent)
if root_directory not in sys.path:
    sys.path.insert(0, root_directory)

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

# System- und Reconnection-Konstanten
RECONNECT_DELAYS_SECONDS: tuple[float, ...] = (30.0, 60.0, 120.0, 240.0)
MAINTENANCE_WINDOW_HOUR: int = 12
MAINTENANCE_WINDOW_DURATION_MINUTES: int = 5


class TradingSystemOrchestrator:
    """Orchestriert das Trading-System, verwaltet Ressourcen, Callbacks und Hintergrund-Tasks."""

    def __init__(
        self,
        root_directory_path: Path,
        database_path: Path,
        config: Config,
        notifier: TelegramNotifier,
        interactive_brokers: IB,
        queue: asyncio.Queue,
    ) -> None:
        """Initialisiert den Orchestrator mit allen benötigten Abhängigkeiten.

        Args:
            root_directory_path: Absolute Pfad-Instanz des Root-Verzeichnisses.
            database_path: Absolute Pfad-Instanz zur SQLite-Datenbank.
            config: Die geladene Konfigurations-Instanz.
            notifier: Der Telegram-Notifier-Dienst.
            interactive_brokers: Die Interactive Brokers API-Clientinstanz.
            queue: Die asynchrone Queue für die Abarbeitung von Trade-Gruppen.
        """
        self.root_directory_path: Path = root_directory_path
        self.database_path: Path = database_path
        self.config: Config = config
        self.notifier: TelegramNotifier = notifier
        self.interactive_brokers: IB = interactive_brokers
        self.queue: asyncio.Queue = queue
        self.is_reconnecting: bool = False
        self.tasks: tuple[asyncio.Task, ...] = ()
        self.shutdown_event: asyncio.Event = asyncio.Event()
        self.callbacks_manager: TwsCallbacksManager | None = None

    async def create_database_connection(self) -> aiosqlite.Connection:
        """Erstellt eine neue type-safe Verbindung zur Datenbank.

        Returns:
            aiosqlite.Connection: Eine geöffnete und konfigurierte Datenbankverbindung.
        """
        return await get_db(self.database_path)

    async def trigger_settlement_callback(
        self, trade_group_id: str, account_id: str
    ) -> None:
        """Callback für das Abwickeln (Settlement) von geschlossenen Trades.

        Args:
            trade_group_id: Eindeutige Kennung der Trade-Gruppe.
            account_id: Eindeutige Kontokennung bei Interactive Brokers.
        """
        await trigger_settlement(
            self.create_database_connection, trade_group_id, account_id, self.notifier
        )

    async def handle_retriable_error_callback(self, order_id: int) -> None:
        """Callback für die Handhabung transienter/wiederholbarer Orderfehler.

        Args:
            order_id: Die eindeutige numerische ID der fehlgeschlagenen Order.
        """
        await handle_retriable_error(
            self.create_database_connection,
            order_id,
            self.queue,
            self.notifier,
            self.config,
        )

    async def run_recovery_callback(self) -> None:
        """Führt eine Synchronisations- und Recovery-Phase aus.

        Gleicht offene Orders und Positionen zwischen der Datenbank und der TWS ab.
        """
        database_connection_instance = await self.create_database_connection()
        try:
            await run_recovery(
                database_connection_instance,
                self.interactive_brokers,
                self.queue,
                self.notifier,
                self.trigger_settlement_callback,
                self.config,
            )
        finally:
            await database_connection_instance.close()

    async def run_reconnect_callback(self) -> None:
        """Wrapper für die Wiederverbindungsschleife bei Verbindungsverlust.

        Sorgt dafür, dass nicht mehrere Wiederverbindungsversuche gleichzeitig laufen.
        """
        if self.is_reconnecting:
            logger.warning("Reconnection loop is already running. Skipping trigger.")
            return
        self.is_reconnecting = True
        try:
            await self._execute_reconnect_loop()
        finally:
            self.is_reconnecting = False

    def start_background_tasks(self) -> None:
        """Startet alle asynchronen Hintergrunddienste und speichert deren Tasks."""
        importer_task = asyncio.create_task(
            csv_directory_watcher(
                db_factory=self.create_database_connection,
                interactive_brokers=self.interactive_brokers,
                directory_path=self.root_directory_path / "data" / "orders",
                queue=self.queue,
                notifier=self.notifier,
                config=self.config,
                interval_seconds=self.config.app.csv_watcher_interval_s,
            )
        )

        worker_task = asyncio.create_task(
            execution_worker(
                self.create_database_connection,
                self.interactive_brokers,
                self.queue,
                self.notifier,
                self.config,
            )
        )

        watcher_task = asyncio.create_task(
            alert_watcher(
                db_factory=self.create_database_connection,
                notifier=self.notifier,
                config=self.config,
                interval_seconds=self.config.app.alert_watcher_interval_s,
                dead_order_threshold_minutes=self.config.app.dead_order_threshold_minutes,
                max_slippage_percentage=self.config.account.default_limit_pct,
            )
        )

        sync_task = asyncio.create_task(
            order_status_sync_loop(
                db_factory=self.create_database_connection,
                interactive_brokers=self.interactive_brokers,
                queue=self.queue,
                notifier=self.notifier,
                trigger_settlement_callback=self.trigger_settlement_callback,
                config=self.config,
                interval_seconds=self.config.app.order_sync_interval_s,
            )
        )

        heartbeat_task = asyncio.create_task(self.heartbeat_loop())

        self.tasks = (
            importer_task,
            worker_task,
            watcher_task,
            sync_task,
            heartbeat_task,
        )

    async def graceful_shutdown(self) -> None:
        """Führt eine geordnete Shutdown-Sequenz des gesamten Systems aus."""
        logger.info("Beginning shutdown sequence...")
        await self.notifier.send_system_status(
            title="System Shutdown initiiert", emoji="⚠️"
        )

        logger.info("Cancelling background tasks...")
        for task in self.tasks:
            task.cancel()

        try:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        except Exception as exception:
            logger.debug("Error cancelling tasks", error=str(exception))

        logger.info("Waiting for all queue tasks to complete (queue.join)...")
        try:
            await asyncio.wait_for(
                self.queue.join(), timeout=self.config.app.shutdown_join_timeout_s
            )
            logger.info("Queue successfully cleared")
        except TimeoutError:
            logger.warning("Timeout waiting for queue to clear. Continuing.")

        if self.interactive_brokers.isConnected():
            logger.info("Disconnecting API connection to TWS...")
            self.interactive_brokers.disconnect()
            logger.info("Connection disconnected")

        logger.info("Shutdown sequence completed successfully.")
        await self.notifier.send_system_status(
            title="System geordnet heruntergefahren", emoji="🛑"
        )

    async def _execute_reconnect_loop(self) -> None:
        """Führt die Wiederverbindungsschleife mit steigenden Intervallen aus."""
        logger.info("Starting automatic reconnection...")
        attempt = 1
        max_attempts = self.config.tws.reconnect_max_attempts

        while True:
            if attempt > max_attempts:
                current_delay = 3600.0
            else:
                current_delay = RECONNECT_DELAYS_SECONDS[
                    min(attempt - 1, len(RECONNECT_DELAYS_SECONDS) - 1)
                ]

            logger.info(
                "Waiting before reconnection attempt",
                attempt=attempt,
                delay_seconds=current_delay,
            )
            await asyncio.sleep(current_delay)

            if self.interactive_brokers.isConnected():
                logger.info("Already connected. Ending reconnect loop.")
                return

            success = await self._attempt_single_reconnect(attempt)
            if success:
                logger.info("Reconnection successfully established!")
                await self.notifier.send_system_status(
                    title="WIEDERVERBUNDEN", emoji="✅"
                )
                self.interactive_brokers.reqAutoOpenOrders(True)
                logger.info("Triggering recovery run after reconnection...")
                await self.run_recovery_callback()
                return

            if attempt == max_attempts:
                logger.error(
                    "Reconnection failed after %d attempts. "
                    "Switching to hourly retry mode.",
                    max_attempts,
                )
                await self.notifier.send_system_status(
                    title=(
                        f"WIEDERVERBINDUNG FEHLGESCHLAGEN ({max_attempts} Versuche). "
                        "Stündlicher Retry-Modus aktiv."
                    ),
                    emoji="🚨",
                )

            attempt += 1

    async def _attempt_single_reconnect(self, attempt: int) -> bool:
        """Führt einen einzelnen Verbindungsversuch zur TWS durch.

        Args:
            attempt: Die Nummer des aktuellen Versuchs.

        Returns:
            bool: True, falls die Verbindung erfolgreich aufgebaut wurde, sonst False.
        """
        logger.info(
            "Attempting reconnection",
            attempt=attempt,
            host=self.config.tws.host,
            port=self.config.tws.port,
        )
        try:
            await asyncio.wait_for(
                self.interactive_brokers.connectAsync(
                    self.config.tws.host,
                    self.config.tws.port,
                    clientId=self.config.tws.client_id,
                ),
                timeout=self.config.tws.connection_timeout_s,
            )
            _enable_socket_keepalive(self.interactive_brokers)
            return True
        except Exception as exception:
            logger.warning(
                "Reconnection attempt failed",
                attempt=attempt,
                error=str(exception),
            )
            return False

    async def heartbeat_loop(self) -> None:
        """Sendet periodisch einen Ping (reqCurrentTime) an das Gateway.

        Überprüft die Verbindung auf Ausfälle und führt Reconnects herbei.

        Raises:
            asyncio.CancelledError: Falls die Schleife vom System abgebrochen wird.
        """
        logger.info(
            "Starting application-level Heartbeat Keep-Alive",
            interval_seconds=self.config.tws.heartbeat_interval_s,
            timeout_seconds=self.config.tws.heartbeat_timeout_s,
        )

        while True:
            try:
                await self._execute_heartbeat_cycle()
            except asyncio.CancelledError:
                logger.info("Heartbeat loop was cancelled.")
                raise
            except Exception as exception:
                logger.error("Error in heartbeat loop", error=str(exception))

            await asyncio.sleep(self.config.tws.heartbeat_interval_s)

    async def _execute_heartbeat_cycle(self) -> None:
        """Führt eine einzelne Ausführung des Heartbeat-Pings aus."""
        if self._is_inside_maintenance_window():
            logger.info(
                f"Inside daily restart window ({MAINTENANCE_WINDOW_HOUR}:00-{MAINTENANCE_WINDOW_HOUR}:0{MAINTENANCE_WINDOW_DURATION_MINUTES}). Pausing heartbeat."
            )
            await asyncio.sleep(60.0)
            return

        if not self.interactive_brokers.isConnected():
            logger.debug("Heartbeat skipped: IB is not connected")
            return

        await self._send_ping_and_handle_timeout()

    def _is_inside_maintenance_window(self) -> bool:
        """Prüft, ob die aktuelle Uhrzeit im täglichen Restart-Fenster liegt.

        Returns:
            bool: True, falls im Wartungsfenster, andernfalls False.
        """
        from datetime import datetime

        now = datetime.now()
        is_hour_matching = now.hour == MAINTENANCE_WINDOW_HOUR
        is_minute_matching = 0 <= now.minute < MAINTENANCE_WINDOW_DURATION_MINUTES
        return is_hour_matching and is_minute_matching

    async def _send_ping_and_handle_timeout(self) -> None:
        """Sendet einen reqCurrentTime Ping und trennt bei Timeout die Verbindung."""
        try:
            await asyncio.wait_for(
                self.interactive_brokers.reqCurrentTimeAsync(),
                timeout=self.config.tws.heartbeat_timeout_s,
            )
            logger.debug("Heartbeat ping successful")
        except TimeoutError:
            logger.error(
                "Heartbeat timeout. API connection stalled. Triggering disconnect.",
                timeout_seconds=self.config.tws.heartbeat_timeout_s,
            )
            await self.notifier.send_message(
                "⚠️ <b>HEARTBEAT TIMEOUT</b> | API reagiert nicht. Reconnect wird erzwungen."
            )
            self.interactive_brokers.disconnect()
        except Exception as exception:
            logger.warning("Error during heartbeat ping", error=str(exception))


async def main() -> None:
    """Zentraler Orchestrierungs- und Lebenszyklus-Einstiegspunkt des Systems."""
    logger.info("Starting IBKR Equities Trading System (Release 1)")

    # 1. Konfiguration laden & Logging konfigurieren
    root_directory_path = Path(__file__).resolve().parent.parent
    config = _initialize_config_and_logging(root_directory_path)

    # 2. Telegram Notifier initialisieren
    notifier = TelegramNotifier(config)
    await notifier.send_system_status(title="Trading System startet", emoji="🚀")

    # 3. Datenbank-Integrity Check
    database_path = await _verify_database_integrity(root_directory_path, notifier)

    # 4. Verbindung zu TWS aufbauen
    interactive_brokers = IB()
    is_connected = await connect_to_tws(interactive_brokers, config)
    if not is_connected:
        sys.exit(1)

    # 5. DB-Verbindung öffnen und Migrationen ausführen
    await _run_database_migrations(
        root_directory_path, database_path, interactive_brokers
    )

    # 6. reqAutoOpenOrders aktivieren
    interactive_brokers.reqAutoOpenOrders(True)

    # 7. Orchestrator initialisieren und Hintergrunddienste starten
    orchestrator = await _initialize_and_start_orchestrator(
        root_directory_path=root_directory_path,
        database_path=database_path,
        config=config,
        notifier=notifier,
        interactive_brokers=interactive_brokers,
    )

    # 8. Graceful Shutdown Event registrieren
    _setup_graceful_shutdown(orchestrator)

    # Auf Beendigungssignal warten
    await orchestrator.shutdown_event.wait()

    # 9. Graceful Shutdown Sequenz ausführen
    await orchestrator.graceful_shutdown()


async def _initialize_and_start_orchestrator(
    root_directory_path: Path,
    database_path: Path,
    config: Config,
    notifier: TelegramNotifier,
    interactive_brokers: IB,
) -> TradingSystemOrchestrator:
    """Instanziiert den Orchestrator und startet alle Hintergrunddienste und Recovery.

    Args:
        root_directory_path: Pfad zum Hauptverzeichnis der Anwendung.
        database_path: Pfad zur SQL-Datenbankdatei.
        config: Die Systemkonfiguration.
        notifier: Telegram Notifier Dienst.
        interactive_brokers: TWS Client-API-Verbindung.

    Returns:
        TradingSystemOrchestrator: Die initialisierte und gestartete Orchestrator-Instanz.
    """
    queue: asyncio.Queue[str] = asyncio.Queue()
    orchestrator = TradingSystemOrchestrator(
        root_directory_path=root_directory_path,
        database_path=database_path,
        config=config,
        notifier=notifier,
        interactive_brokers=interactive_brokers,
        queue=queue,
    )
    _register_callbacks(orchestrator, interactive_brokers, notifier, config)
    await orchestrator.run_recovery_callback()
    orchestrator.start_background_tasks()
    return orchestrator


async def _run_database_migrations(
    root_directory_path: Path,
    database_path: Path,
    interactive_brokers: IB,
) -> None:
    """Führt die Datenbankschemas-Migrationen aus."""
    database_connection_instance = await get_db(database_path)
    try:
        migrations_directory = root_directory_path / "migrations"
        await run_migrations(database_connection_instance, migrations_directory)
    except Exception as exception:
        logger.critical("Error executing database migrations", error=str(exception))
        await interactive_brokers.disconnect()
        await database_connection_instance.close()
        sys.exit(1)
    finally:
        await database_connection_instance.close()


def _register_callbacks(
    orchestrator: TradingSystemOrchestrator,
    interactive_brokers: IB,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """Registriert alle TWS Callbacks."""
    orchestrator.callbacks_manager = TwsCallbacksManager(
        db_factory=orchestrator.create_database_connection,
        interactive_brokers=interactive_brokers,
        notifier=notifier,
        config=config,
        trigger_settlement_callback=orchestrator.trigger_settlement_callback,
        handle_retriable_error_callback=orchestrator.handle_retriable_error_callback,
        run_recovery_callback=orchestrator.run_recovery_callback,
        run_reconnect_callback=orchestrator.run_reconnect_callback,
    )
    orchestrator.callbacks_manager.register_all()


def _setup_graceful_shutdown(orchestrator: TradingSystemOrchestrator) -> None:
    """Registriert Signal-Handler für ein sauberes Herunterfahren."""
    loop = asyncio.get_running_loop()

    def signal_handler() -> None:
        logger.info("Signal received. Starting graceful shutdown...")
        orchestrator.shutdown_event.set()

    for signal_type in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_type, signal_handler)
        except NotImplementedError:
            pass


async def connect_to_tws(interactive_brokers: IB, config: Config) -> bool:
    """Verbindung zu TWS mit exponentiellem Backoff aufbauen.

    Nutzt die reconnect_initial_delay_s Parameter aus der Config.

    Args:
        interactive_brokers: Die Interactive Brokers API-Clientinstanz.
        config: Die geladene Konfigurations-Instanz.

    Returns:
        bool: True, falls die Verbindung erfolgreich aufgebaut wurde, sonst False.
    """
    delay = config.tws.reconnect_initial_delay_s
    max_attempts = config.tws.reconnect_max_attempts

    for attempt in range(1, max_attempts + 1):
        if await _attempt_connection(interactive_brokers, config, attempt):
            return True

        if attempt == max_attempts:
            break

        logger.info("Waiting before retry connection attempt", delay_seconds=delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2.0, config.tws.reconnect_max_delay_s)

    logger.critical(
        f"Connection to TWS impossible after {max_attempts} attempts. Terminating application."
    )
    return False


async def _attempt_connection(
    interactive_brokers: IB, config: Config, attempt: int
) -> bool:
    """Führt einen einzelnen Verbindungsversuch zur TWS durch."""
    logger.info(
        "TWS connection establishment started",
        attempt=attempt,
        host=config.tws.host,
        port=config.tws.port,
        client_id=config.tws.client_id,
    )
    try:
        await asyncio.wait_for(
            interactive_brokers.connectAsync(
                config.tws.host, config.tws.port, clientId=config.tws.client_id
            ),
            timeout=config.tws.connection_timeout_s,
        )
        logger.info("Successfully connected to TWS platform")
        _enable_socket_keepalive(interactive_brokers)
        return True
    except Exception as exception:
        logger.warning("Connection failed", attempt=attempt, error=str(exception))
        return False


def _initialize_config_and_logging(root_directory_path: Path) -> Config:
    """Lädt die Konfiguration und re-konfiguriert das Logging."""
    try:
        config = load_config(root_directory_path)

        # Sicherstellen, dass das orders-Verzeichnis existiert
        orders_directory = root_directory_path / "data" / "orders"
        orders_directory.mkdir(parents=True, exist_ok=True)

        configure_logging(
            log_file_path=root_directory_path / config.app.log_file_path,
            backup_count=config.app.log_rotation_backup_count,
        )
        logger.info("Configuration successfully loaded and logging re-configured")
        return config
    except Exception as exception:
        logger.critical("Severe error loading configuration", error=str(exception))
        sys.exit(1)


async def _verify_database_integrity(
    root_directory_path: Path, notifier: TelegramNotifier
) -> Path:
    """Führt eine Integritätsprüfung der SQL-Datenbank durch."""
    database_path = root_directory_path / "data" / "trading.db"
    is_db_ok = await verify_db_integrity(database_path)
    if not is_db_ok:
        logger.critical("DB integrity check failed. Terminating for safety.")
        await notifier.send_system_status(
            title="DB-Integritaetspruefung fehlgeschlagen! Anwendung beendet.",
            emoji="🚨",
        )
        sys.exit(1)
    return database_path


def _enable_socket_keepalive(interactive_brokers: IB) -> None:
    """Aktiviert TCP-Keep-Alive auf dem TWS-Verbindungs-Socket zur Vermeidung von Timeouts."""
    if not interactive_brokers.isConnected():
        return

    try:
        socket_object = interactive_brokers.client.conn.transport.get_extra_info(
            "socket"
        )
        if not socket_object:
            return

        socket_object.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        tcp_keepidle = getattr(socket, "TCP_KEEPALIVE", None) or getattr(
            socket, "TCP_KEEPIDLE", None
        )
        if tcp_keepidle is not None:
            socket_object.setsockopt(socket.IPPROTO_TCP, tcp_keepidle, 60)

        tcp_keepintvl = getattr(socket, "TCP_KEEPINTVL", None)
        if tcp_keepintvl is not None:
            socket_object.setsockopt(socket.IPPROTO_TCP, tcp_keepintvl, 10)

        tcp_keepcnt = getattr(socket, "TCP_KEEPCNT", None)
        if tcp_keepcnt is not None:
            socket_object.setsockopt(socket.IPPROTO_TCP, tcp_keepcnt, 5)

        logger.info("TCP keep-alive settings successfully applied to socket")
    except Exception as exception:
        logger.warning(
            "Error enabling TCP keep-alive on the socket",
            error=str(exception),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
