import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import structlog


def configure_logging(
    log_file_path: Path = Path("data/logs/app.log"), backup_count: int = 5
) -> None:
    """
    Konfiguriert structlog in Kombination mit dem Python-Standard-logging-Modul.

    Schreibt Logs formatiert auf stdout (Konsole) und in eine rotierende Datei
    (täglicher Wechsel, 5 Backups werden aufbewahrt).
    """
    # 1. Sicherstellen, dass das Log-Verzeichnis existiert
    if log_file_path != Path(":memory:"):
        log_file_path.parent.mkdir(parents=True, exist_ok=True)

    # 3. StreamHandler fuer farbige Konsolenausgabe
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    # 4. Root-Logger des Standard-logging konfigurieren
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Vorherige Handler entfernen (um Duplikate bei Reconnects/Imports zu verhindern)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)

    # 2. TimedRotatingFileHandler erstellen (nur wenn nicht :memory:)
    file_handler = None
    if log_file_path != Path(":memory:"):
        file_handler = TimedRotatingFileHandler(
            filename=str(log_file_path),
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)

    # Gemeinsame Pre-Prozessoren definieren (auch fuer externe Bibliotheken)
    shared_pre_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S.%f", utc=False),
    ]

    # 5. Structlog so konfigurieren, dass es an das Standard-logging weiterleitet
    structlog.configure(
        processors=[
            *shared_pre_processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            # Der Processor fuer das Standard-logging wandelt Event-Dicts in Strings um
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 6. Formatter fuer den stdlib-ProcessorFormatter im root logger setzen
    # console handler soll structlog Dev Console Renderer nutzen (fuer Farben und Formatierung)
    console_formatter_struct = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=shared_pre_processors,
    )
    console_handler.setFormatter(console_formatter_struct)

    # file handler soll einen sauberen ConsoleRenderer ohne ANSI-Farben erhalten
    if file_handler is not None:
        file_formatter_struct = structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=shared_pre_processors,
        )
        file_handler.setFormatter(file_formatter_struct)

    # 7. Exzessives Log-Spamming von externen Bibliotheken drosseln
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    logging.getLogger("ib_insync").setLevel(logging.WARNING)
