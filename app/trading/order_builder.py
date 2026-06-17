"""
Konstruktion von Verträgen (Contracts) und TWS-Aufträgen (Orders).

Erstellt SMART-Routing US-Aktienkontrakte und konfiguriert
die entsprechenden Stop-, Limit- oder Market-Orders inkl. OCA-Gruppen.
"""

import structlog
from ib_async import Order, Stock

from app.core.models import OrderRow

logger = structlog.get_logger()


def make_stock_contract(symbol: str) -> Stock:
    """Erstellt ein TWS-konformes US-Aktien-Vertragsobjekt via SMART-Routing."""
    return Stock(symbol.upper(), "SMART", "USD")


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
    if order.orderType in ("LMT", "LOC"):
        order.lmtPrice = float(order_row.target_price)
    elif order.orderType == "STP":
        # TWS Stop-Orders nutzen auxPrice für das Stop-Trigger-Niveau
        order.auxPrice = float(order_row.target_price)
    elif order.orderType in ("MKT", "MOC"):
        pass
    else:
        logger.warning(
            "Unbekannter Order-Typ. Keinen Preis zugewiesen.",
            order_type=order.orderType,
        )

    # OCA (One-Cancels-All) Gruppe konfigurieren für SL und TP
    # WICHTIG: LOC und MOC Orders duerfen laut IBKR nicht in einer OCA Gruppe sein!
    if order_row.bracket_role in ("SL", "TP") and order.orderType not in ("LOC", "MOC"):
        # Alle Legs derselben trade_group_id tragen denselben OCA-String.
        # Wir haengen _v3 an, um Probleme mit dem TWS Session-Memory zu umgehen.
        order.ocaGroup = f"OCA_{order_row.trade_group_id}_v3"
        order.ocaType = 1

    return order
