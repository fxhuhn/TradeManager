import logging
import re
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import structlog


def _simplify_ibkr_warning(msg: str) -> str:
    """Extracts key details from a long raw Trade representation in IBKR warning."""
    if not msg.startswith("IBKR API validation warning: Trade("):
        return msg

    # Extract symbol
    symbol_match = re.search(r"symbol='([^']+)'", msg)
    symbol = symbol_match.group(1) if symbol_match else "UNKNOWN"

    # Extract orderId
    order_id_match = re.search(r"orderId=(\d+)", msg)
    order_id = order_id_match.group(1) if order_id_match else "UNKNOWN"

    # Extract action
    action_match = re.search(r"action='([^']+)'", msg)
    action = action_match.group(1) if action_match else "UNKNOWN"

    # Extract quantity
    qty_match = re.search(r"totalQuantity=([\d.]+)", msg)
    qty = qty_match.group(1) if qty_match else "UNKNOWN"

    # Extract order type
    order_type_match = re.search(r"orderType='([^']+)'", msg)
    order_type = order_type_match.group(1) if order_type_match else "UNKNOWN"

    # Extract price (lmtPrice)
    price_match = re.search(r"lmtPrice=([\d.]+)", msg)
    price = price_match.group(1) if price_match else "UNKNOWN"

    # Extract warning/error message from TradeLogEntry
    messages = re.findall(r"message='([^']*)'", msg)
    warning_msg = ""
    for m in reversed(messages):
        if m.strip():
            warning_msg = m
            break

    if not warning_msg:
        why_held_match = re.search(r"whyHeld='([^']*)'", msg)
        if why_held_match and why_held_match.group(1):
            warning_msg = f"Held: {why_held_match.group(1)}"

    details = f"{action} {qty} {symbol} ({order_type} @ {price})"
    if warning_msg:
        return f"IBKR API validation warning: {details} -> {warning_msg} (OrderId: {order_id})"
    else:
        return f"IBKR API validation warning: {details} (OrderId: {order_id})"


def clean_ib_async_warnings_processor(logger: object, method_name: str, event_dict: dict) -> dict:
    """Structlog processor to simplify verbose IBKR wrapper validation warnings."""
    event = event_dict.get("event")
    if isinstance(event, str) and event.startswith("IBKR API validation warning: Trade("):
        event_dict["event"] = _simplify_ibkr_warning(event)
    return event_dict



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
        clean_ib_async_warnings_processor,
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
