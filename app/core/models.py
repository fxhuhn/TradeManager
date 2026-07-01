"""
Datenmodelle und Datenbankschemata.

Definiert die internen Datenstrukturen für Order-Legs, TWS-Orders,
Order-Ausführungen und Settlements als typsichere Dataclasses.

Architektur & Datenfluss-Zusammenhang:
- orders: Repräsentiert die Absicht (Order-Intention). Bei Market-Orders ist der `target_price` standardmäßig 0.00.
- executions: Repräsentiert die Realisierung (Teilausführungen). Hier werden die tatsächlichen Preise (`price`) gespeichert.
- trades_settlement: Berechnete, konsolidierte Ergebnisse geschlossener Trades (VWAP von Einstieg/Ausstieg und Slippage).

Event-Datenfluss & Timing:
1. TWS meldet `execDetailsEvent` (Teilausführung) -> Speicherung in `executions` (falls Order in DB vorhanden).
2. TWS meldet `orderStatusEvent` ('Filled') -> Callback liest `trade.orderStatus.avgFillPrice` (von TWS konsolidierter Schnittkurs) und sendet ihn sofort an Telegram.
3. Callback aktualisiert Status in `orders` auf 'Filled'.
4. Callback triggert das Settlement, welches Einstiegs- und Ausstiegspreise aus `executions` berechnet und in `trades_settlement` sichert.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def decimal_from_db(value: object) -> Decimal | None:
    """Converts a nullable database value (TEXT or legacy REAL) to Decimal.

    Handles backward compatibility with legacy float-stored values and new
    TEXT-stored Decimal strings. Returns None for NULL database values.
    """
    if value is None:
        return None
    return Decimal(str(value))


@dataclass(frozen=True)
class LegRow:
    """Repräsentiert ein einzelnes Leg (Order-Leg) aus der CSV-Datei.

    Diese Klasse ist unveränderlich (frozen), um Datenintegrität beim Importieren
    von Handelsspezifikationen sicherzustellen.
    """

    trade_group_id: str
    bracket_role: str  # ENTRY, SL, TP, EXIT
    symbol: str
    sec_type: str  # Nur 'STK' erlaubt
    exchange: str  # Nur 'SMART' erlaubt
    account_id: str
    action: str  # BUY, SELL
    quantity: int
    order_type: str  # LMT, STP, MKT, MOC
    target_price: Decimal | None  # Pflicht bei LMT und STP, sonst leer
    tif: str  # DAY, GTC (Standard: GTC)
    strategy_name: str


@dataclass(frozen=True)
class OrderRow:
    """Repräsentiert eine Zeile der Tabelle 'orders'.

    Diese Klasse ist unveränderlich (frozen), um Datenintegrität zu gewährleisten.
    Zustandsänderungen werden über funktionale Kopien (dataclasses.replace) abgebildet.
    """

    order_id: int
    perm_id: int | None
    parent_id: int | None
    trade_group_id: str
    account_id: str
    bracket_role: str  # ENTRY, SL, TP, EXIT
    symbol: str
    sec_type: str
    exchange: str
    action: str
    quantity: int
    order_type: str
    target_price: Decimal | None
    tif: str
    strategy_name: str | None
    status: str  # Created, Submitted, PreSubmitted, Filled, Cancelled, Error
    retry_count: int = 0
    transmitted_at: str | None = None


def order_row_from_db_row(row: Mapping[str, Any]) -> OrderRow:
    """Centralized OrderRow construction from an aiosqlite database row mapping."""
    return OrderRow(
        order_id=row["order_id"],
        perm_id=row["perm_id"],
        parent_id=row["parent_id"],
        trade_group_id=row["trade_group_id"],
        account_id=row["account_id"],
        bracket_role=row["bracket_role"],
        symbol=row["symbol"],
        sec_type=row["sec_type"],
        exchange=row["exchange"],
        action=row["action"],
        quantity=row["quantity"],
        order_type=row["order_type"],
        target_price=decimal_from_db(row["target_price"]),
        tif=row["tif"],
        strategy_name=row["strategy_name"],
        status=row["status"],
        retry_count=row["retry_count"],
        transmitted_at=row["transmitted_at"],
    )


@dataclass(frozen=True)
class ExecutionRow:
    """Repräsentiert eine Zeile der Tabelle 'executions'.

    Diese Klasse ist unveränderlich (frozen), um nachträgliche Modifikationen
    historischer Ausführungsdaten im Speicher zu verhindern.
    """

    exec_id: str
    order_id: int
    price: Decimal
    qty: Decimal
    commission: Decimal | None = None
    currency: str | None = None
    executed_at: str | None = None


@dataclass(frozen=True)
class SettlementRow:
    """Repräsentiert das konsolidierte Ergebnis eines geschlossenen Trades.

    Alle Berechnungen werden hochpräzise mit Decimal deklariert.
    """

    account_id: str
    trade_group_id: str
    avg_entry_price: Decimal
    avg_exit_price: Decimal
    price_diff_slippage: Decimal
    total_commissions: Decimal
    net_pnl: Decimal
    settled_at: str | None = None
