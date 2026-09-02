"""
Fehlercode-Klassifizierung für die Interactive Brokers API.

Ordnet TWS-Fehlermeldungen und Informationscodes in strukturierte Klassen ein
(INFO, RECONNECT, RETRIABLE, CANCEL, FATAL), um das automatische Fehlermanagement zu steuern.
"""

import re
from datetime import datetime
from enum import Enum, auto
from zoneinfo import ZoneInfo


class ErrorClass(Enum):
    INFO = auto()  # Rein informativ, keine Aktion nötig
    RECONNECT = auto()  # Verbindungsabbruch/Reconnect-Events
    RETRIABLE = auto()  # Transiente Netzwerkfehler, automatischer Retry
    CANCEL = auto()  # Stornierung der Order
    FATAL = auto()  # Schwerer Fehler, Order fehlgeschlagen


def classify_error_code(code: int) -> ErrorClass:
    """
    Klassifiziert TWS-Fehlercodes in funktionale Reaktionsklassen.
    Reagiert gemäß Abschnitt 5 (Error-Code-Klassifikation).
    """
    # 1. Informative Codes
    if code in (2104, 2106, 2107, 2108, 2119, 2158, 2100, 2182, 399):
        return ErrorClass.INFO

    # 2. Reconnect Codes
    elif code in (1101, 1102):
        return ErrorClass.RECONNECT

    # 3. Retriable Codes (Transiente API-Fehler)
    elif code in (1100, 1300, 10148, 502, 504, 162):
        return ErrorClass.RETRIABLE

    # 4. Cancel Codes (Order storniert)
    elif code in (202, 10147, 10149, 10268):
        return ErrorClass.CANCEL

    # 5. Alle anderen Codes standardmäßig als FATAL einstufen (zur Sicherheit)
    else:
        return ErrorClass.FATAL


def is_reauthorization_error(error_code: int, error_message: str) -> bool:
    """
    Prüft, ob ein Fehlercode oder eine Fehlermeldung eine 2FA/Token-Bestätigung erfordert.

    Erkennt Fehlercode 201 sowie Textbausteine wie 'LOGIN TO CLIENT PORTAL',
    'VERIFY USING THE TOKEN', 'VERIFICATION PROCESS' oder 'TOKEN' in Kombination mit 'VERIFY'.

    Args:
        error_code: Der numerische TWS-Fehlercode.
        error_message: Die Fehlermeldung als Text.

    Returns:
        True, wenn eine Reautorisierung/Token-Bestätigung gefordert wird, sonst False.
    """
    clean_message = re.sub(
        r"[ \t]+", " ", re.sub(r"(?i)<br\s*/?>", " ", error_message)
    ).strip()
    reason_upper = clean_message.upper()

    if (
        "LOGIN TO CLIENT PORTAL" in reason_upper
        or "VERIFY USING THE TOKEN" in reason_upper
        or "VERIFICATION PROCESS" in reason_upper
        or ("TOKEN" in reason_upper and "VERIFY" in reason_upper)
    ):
        return True

    if error_code == 201 and (
        "TOKEN" in reason_upper
        or "PORTAL" in reason_upper
        or "SECURITY" in reason_upper
    ):
        return True

    return False


def is_market_closed_for_symbol(
    symbol: str, current_time: datetime | None = None
) -> bool:
    """
    Überprüft zeitzonengenau, ob der reguläre Marktschluss für ein Wertpapier erreicht ist.

    - Deutsche Aktien (Suffix '.DE'): XETRA schließt um 17:30 Uhr Berlin-Zeit.
    - US-Aktien (Standard): NASDAQ/NYSE schließt um 16:00 Uhr New York-Zeit (22:00 Uhr Berlin-Zeit).

    Args:
        symbol: Ticker-Symbol des Wertpapiers (z. B. 'AAPL' oder 'SXRV.DE').
        current_time: Optionaler Referenzzeitpunkt (für Tests). Falls None, wird datetime.now() verwendet.

    Returns:
        True, wenn der aktuelle Zeitpunkt gleich oder nach dem regulären Marktschluss liegt.
    """
    symbol_upper = symbol.upper()
    if symbol_upper.endswith(".DE"):
        berlin_tz = ZoneInfo("Europe/Berlin")
        if current_time is None:
            now_berlin = datetime.now(berlin_tz)
        elif current_time.tzinfo is None:
            now_berlin = current_time.replace(tzinfo=berlin_tz)
        else:
            now_berlin = current_time.astimezone(berlin_tz)
        market_close = now_berlin.replace(hour=17, minute=30, second=0, microsecond=0)
        return now_berlin >= market_close
    else:
        ny_tz = ZoneInfo("America/New_York")
        if current_time is None:
            now_ny = datetime.now(ny_tz)
        elif current_time.tzinfo is None:
            now_ny = current_time.replace(tzinfo=ny_tz)
        else:
            now_ny = current_time.astimezone(ny_tz)
        market_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
        return now_ny >= market_close
