from dataclasses import dataclass
from decimal import Decimal


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


@dataclass
class OrderRow:
    """Repräsentiert eine Zeile der Tabelle 'orders'.

    Diese Klasse bleibt veränderbar, da der Order-Status und die zugewiesenen
    TWS-OrderIDs während des Übermittlungsprozesses im Arbeitsspeicher aktualisiert werden.
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
