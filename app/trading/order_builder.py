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


def get_tick_size(symbol: str, price: float) -> float:
    """Ermittelt die minimale Preisänderung (Tick Size) für ein Symbol."""
    symbol_upper = symbol.upper()
    if symbol_upper.endswith(".DE"):
        if price >= 50000.0:
            return 10.0
        if price >= 20000.0:
            return 5.0
        if price >= 10000.0:
            return 2.0
        if price >= 5000.0:
            return 1.0
        if price >= 2000.0:
            return 0.5
        if price >= 1000.0:
            return 0.2
        if price >= 500.0:
            return 0.1
        if price >= 200.0:
            return 0.05
        if price >= 100.0:
            return 0.02
        if price >= 50.0:
            return 0.01
        if price >= 20.0:
            return 0.005
        if price >= 10.0:
            return 0.002
        if price >= 5.0:
            return 0.001
        if price >= 2.0:
            return 0.0005
        if price >= 1.0:
            return 0.0002
        return 0.0001
    return 0.01  # Standard für US-Aktien


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
