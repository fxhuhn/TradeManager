"""
Fehlercode-Klassifizierung für die Interactive Brokers API.

Ordnet TWS-Fehlermeldungen und Informationscodes in strukturierte Klassen ein
(INFO, RECONNECT, RETRIABLE, CANCEL, FATAL), um das automatische Fehlermanagement zu steuern.
"""

from enum import Enum, auto


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
