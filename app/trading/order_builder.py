import structlog
from ib_async import Stock, Order
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
    if order.orderType == "LMT":
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
    if order_row.bracket_role in ("SL", "TP"):
        # Alle Legs derselben trade_group_id tragen denselben OCA-String
        order.ocaGroup = f"OCA_{order_row.trade_group_id}"
        # ocaType = 2 (Proportional reduce with auto-size)
        order.ocaType = 2

    return order
