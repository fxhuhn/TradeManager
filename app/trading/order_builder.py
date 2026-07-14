"""
Konstruktion von Verträgen (Contracts) und TWS-Aufträgen (Orders).

Erstellt SMART-Routing US-Aktienkontrakte und konfiguriert
die entsprechenden Stop-, Limit- oder Market-Orders inkl. OCA-Gruppen.
"""

from decimal import ROUND_HALF_UP, Decimal

import structlog
from ib_async import Order, Stock

from app.core.models import OrderRow

logger = structlog.get_logger()


def make_stock_contract(symbol: str) -> Stock:
    """Erstellt ein TWS-konformes Aktien-Vertragsobjekt via SMART-Routing."""
    symbol_upper = symbol.upper()

    if symbol_upper.endswith(".DE"):
        clean_symbol = symbol_upper[:-3]
        return Stock(clean_symbol, "SMART", "EUR", primaryExchange="IBIS2")

    return Stock(symbol_upper, "SMART", "USD")


# Xetra tick-size table: (lower_bound, tick_size)
# Source: Deutsche Börse Xetra Tick Size Table (MiFID II liquidity bands)
_XETRA_TICK_TABLE: list[tuple[float, float]] = [
    (50000.0, 10.0),
    (20000.0, 5.0),
    (10000.0, 2.0),
    (5000.0, 1.0),
    (2000.0, 0.5),
    (1000.0, 0.2),
    (500.0, 0.1),
    (200.0, 0.05),
    (100.0, 0.02),
    (50.0, 0.01),
    (20.0, 0.005),
    (10.0, 0.002),
    (5.0, 0.001),
    (2.0, 0.0005),
    (1.0, 0.0002),
]

_DEFAULT_US_TICK_SIZE: float = 0.01
_XETRA_MIN_TICK_SIZE: float = 0.0001


def get_tick_size(symbol: str, price: float) -> float:
    """Ermittelt die minimale Preisänderung (Tick Size) für ein Symbol."""
    if not symbol.upper().endswith(".DE"):
        return _DEFAULT_US_TICK_SIZE

    for lower_bound, tick_size in _XETRA_TICK_TABLE:
        if price >= lower_bound:
            return tick_size

    return _XETRA_MIN_TICK_SIZE


def round_to_tick(price: float, tick_size: float) -> float:
    """Rundet einen Preis auf das nächste Vielfache der Tick-Größe unter Berücksichtigung von Float-Ungenauigkeiten."""
    price_dec = Decimal(str(price))
    tick_dec = Decimal(str(tick_size))
    rounded = (price_dec / tick_dec).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ) * tick_dec
    return float(rounded)


def build_order(order_row: OrderRow) -> Order:
    """
    Konstruiert ein ib_async Order-Objekt aus den DB-Orderzeilen.
    Berücksichtigt Order-Typen und OCA-Konfigurationen für Stop-Loss (SL) und Take-Profit (TP).
    """
    order = Order()
    order.orderId = order_row.order_id
    order.action = order_row.action.upper()
    order.totalQuantity = float(order_row.quantity)
    order.orderType = order_row.order_type.upper()
    order.tif = order_row.tif.upper() if order_row.tif else "GTC"

    # Strategie-Name im Order Reference-Feld fuer TWS hinterlegen
    if order_row.strategy_name:
        order.orderRef = order_row.strategy_name

    # Preise setzen (Dezimal-zu-Float-Konvertierung an der API-Schnittstelle)
    # Runden auf die minimale Tick-Größe des Zielmarkts
    if order.orderType in ("LMT", "LOC"):
        price = float(order_row.target_price)
        tick_size = get_tick_size(order_row.symbol, price)
        order.lmtPrice = round_to_tick(price, tick_size)
    elif order.orderType == "STP":
        # TWS Stop-Orders nutzen auxPrice für das Stop-Trigger-Niveau
        price = float(order_row.target_price)
        tick_size = get_tick_size(order_row.symbol, price)
        order.auxPrice = round_to_tick(price, tick_size)
    elif order.orderType in ("MKT", "MOC"):
        pass
    else:
        logger.warning(
            "Unbekannter Order-Typ. Keinen Preis zugewiesen.",
            order_type=order.orderType,
        )

    # OCA (One-Cancels-All) Gruppe konfigurieren für SL, TP und EXIT
    if order_row.bracket_role in ("SL", "TP", "EXIT"):
        # Alle Legs derselben trade_group_id tragen denselben OCA-String.
        # Wir haengen _v4 an, um Probleme mit dem TWS Session-Memory zu umgehen.
        order.ocaGroup = f"OCA_{order_row.trade_group_id}_v4"

        # LOC und MOC Orders duerfen laut IBKR nur mit ocaType = 3 (reduce with no block) in einer OCA Gruppe sein.
        # Auch bei EXIT-Orders nutzen wir ocaType = 3, da sie standardmaessig aus LMT + LOC Exits bestehen.
        if order.orderType in ("LOC", "MOC") or order_row.bracket_role == "EXIT":
            order.ocaType = 3
        else:
            order.ocaType = 1

    return order


def extract_transmitted_price(ib_order: Order) -> Decimal | None:
    """Extracts the actual tick-rounded price from a constructed ib_async Order.

    After build_order() applies tick-size rounding, the price stored on the
    ib_async Order object may differ from the original target_price. This
    function returns that rounded price so callers can update DB and
    notifications to reflect the value actually transmitted to TWS.
    """
    order_type = ib_order.orderType.upper() if ib_order.orderType else ""

    if order_type in ("LMT", "LOC") and ib_order.lmtPrice:
        return Decimal(str(ib_order.lmtPrice))

    if order_type == "STP" and ib_order.auxPrice:
        return Decimal(str(ib_order.auxPrice))

    return None
