"""
Konstruktion von Verträgen (Contracts) und TWS-Aufträgen (Orders).

Erstellt SMART-Routing US-Aktienkontrakte und konfiguriert
die entsprechenden Stop-, Limit- oder Market-Orders inkl. OCA-Gruppen.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from zoneinfo import ZoneInfo

import structlog
from ib_async import Contract, Future, Order, PriceCondition, Stock, TimeCondition

from app.core.models import OrderRow

logger = structlog.get_logger()

# Permanente IBKR Contract ID für QQQ STK SMART USD
QQQ_CON_ID: Final[int] = 320227571
CME_TIMEZONE: Final[ZoneInfo] = ZoneInfo("America/Chicago")


def normalize_symbol(symbol: str) -> str:
    """Normalisiert ein Aktiensymbol durch Entfernung von Börsensuffixen
    und Konvertierung von US-Aktienklassen-Trennzeichen für TWS.

    Args:
        symbol: Das rohe Symbol (z. B. 'SXRV.DE', 'BF-B', 'BRK.B' oder 'AAPL').

    Returns:
        Das bereinigte Ticker-Symbol in Grossbuchstaben (z. B. 'SXRV', 'BF B', 'BRK B').
    """
    cleaned_symbol = symbol.strip().upper()
    if cleaned_symbol.endswith(".DE"):
        return cleaned_symbol[:-3]

    # US-Share-Class Trennzeichen (z. B. 'BF-B' oder 'BRK.B') durch Leerzeichen für IBKR TWS API ersetzen
    return cleaned_symbol.replace("-", " ").replace(".", " ")


def symbols_match(symbol_a: str | None, symbol_b: str | None) -> bool:
    """Prüft, ob zwei Aktiensymbole nach Normalisierung identisch sind.

    Gibt True zurück, wenn beide Symbole nach Bereinigung von Börsensuffixen
    und US-Share-Class-Trennzeichen übereinstimmen. Wenn mindestens ein Symbol
    None oder leer ist, wird True zurückgegeben.

    Args:
        symbol_a: Erstes Symbol (z. B. aus TWS-Event).
        symbol_b: Zweites Symbol (z. B. aus lokaler DB).

    Returns:
        True bei Übereinstimmung oder fehlendem Vergleichswert, sonst False.
    """
    if not symbol_a or not symbol_b:
        return True
    return normalize_symbol(symbol_a) == normalize_symbol(symbol_b)


def make_stock_contract(symbol: str) -> Stock:
    """Erstellt ein TWS-konformes Aktien-Vertragsobjekt via SMART-Routing."""
    clean_symbol = normalize_symbol(symbol)

    if symbol.strip().upper().endswith(".DE"):
        return Stock(clean_symbol, "SMART", "EUR", primaryExchange="IBIS2")

    return Stock(clean_symbol, "SMART", "USD")


def make_future_contract(
    symbol: str,
    contract_month: str = "",
    exchange: str = "CME",
    currency: str = "USD",
) -> Future:
    """Erstellt ein TWS-konformes Future-Vertragsobjekt (z. B. für MNQ oder MES).

    Unterstützt sowohl konkrete LocalSymbols (z. B. 'MNQU6') als auch
    Basis-Symbole mit Verfallsmonat (z. B. symbol='MNQ', contract_month='20260918').
    """
    clean_symbol = normalize_symbol(symbol)
    if len(clean_symbol) > 3 and any(char.isdigit() for char in clean_symbol):
        return Future(
            localSymbol=clean_symbol,
            exchange=exchange,
            currency=currency,
        )
    return Future(
        symbol=clean_symbol,
        lastTradeDateOrContractMonth=contract_month,
        exchange=exchange,
        currency=currency,
    )


def make_contract_for_order(order_row: OrderRow) -> Contract:
    """Erstellt ein passendes Contract-Objekt (Stock oder Future) für die gegebene OrderRow."""
    if order_row.sec_type == "FUT":
        return make_future_contract(
            symbol=order_row.symbol,
            exchange=order_row.exchange or "CME",
            currency="USD",
        )
    return make_stock_contract(order_row.symbol)


# Xetra tick-size table: (lower_bound, tick_size)
# Source: Deutsche Börse Xetra Tick Size Table (MiFID II liquidity bands)
_XETRA_TICK_TABLE: list[tuple[Decimal, Decimal]] = [
    (Decimal("50000.0"), Decimal("10.0")),
    (Decimal("20000.0"), Decimal("5.0")),
    (Decimal("10000.0"), Decimal("2.0")),
    (Decimal("5000.0"), Decimal("1.0")),
    (Decimal("2000.0"), Decimal("0.5")),
    (Decimal("1000.0"), Decimal("0.2")),
    (Decimal("500.0"), Decimal("0.1")),
    (Decimal("200.0"), Decimal("0.05")),
    (Decimal("100.0"), Decimal("0.02")),
    (Decimal("50.0"), Decimal("0.01")),
    (Decimal("20.0"), Decimal("0.005")),
    (Decimal("10.0"), Decimal("0.002")),
    (Decimal("5.0"), Decimal("0.001")),
    (Decimal("2.0"), Decimal("0.0005")),
    (Decimal("1.0"), Decimal("0.0002")),
]

_DEFAULT_US_TICK_SIZE: Decimal = Decimal("0.01")
_XETRA_MIN_TICK_SIZE: Decimal = Decimal("0.0001")


def get_tick_size(symbol: str, price: Decimal | float) -> Decimal:
    """Ermittelt die minimale Preisänderung (Tick Size) als Decimal für ein Symbol."""
    price_decimal = price if isinstance(price, Decimal) else Decimal(str(price))
    if not symbol.upper().endswith(".DE"):
        return _DEFAULT_US_TICK_SIZE

    for lower_bound, tick_size in _XETRA_TICK_TABLE:
        if price_decimal >= lower_bound:
            return tick_size

    return _XETRA_MIN_TICK_SIZE


def round_to_tick(price: Decimal | float, tick_size: Decimal | float) -> Decimal:
    """Rundet einen Preis auf das nächste Vielfache der Tick-Größe als Decimal."""
    price_decimal = price if isinstance(price, Decimal) else Decimal(str(price))
    tick_decimal = (
        tick_size if isinstance(tick_size, Decimal) else Decimal(str(tick_size))
    )
    return (price_decimal / tick_decimal).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ) * tick_decimal


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
        target_price = order_row.target_price or Decimal("0.0")
        tick_size = get_tick_size(order_row.symbol, target_price)
        rounded_price = round_to_tick(target_price, tick_size)
        order.lmtPrice = float(rounded_price)
    elif order.orderType == "STP":
        # TWS Stop-Orders nutzen auxPrice für das Stop-Trigger-Niveau
        target_price = order_row.target_price or Decimal("0.0")
        tick_size = get_tick_size(order_row.symbol, target_price)
        rounded_price = round_to_tick(target_price, tick_size)
        order.auxPrice = float(rounded_price)
    elif order.orderType in ("MKT", "MOC"):
        pass
    else:
        logger.warning(
            "Unbekannter Order-Typ. Keinen Preis zugewiesen.",
            order_type=order.orderType,
        )

    # Spezifische Behandlung für BounceBandit Future-Orders (MNQ @ CME)
    is_bounce_bandit_future = (
        order_row.strategy_name is not None
        and order_row.strategy_name.lower() == "bouncebandit"
        and order_row.sec_type == "FUT"
    )

    if is_bounce_bandit_future:
        order.orderType = "MKT"
        order.outsideRth = True
        today_string = datetime.now(CME_TIMEZONE).strftime("%Y%m%d")

        if order_row.bracket_role == "ENTRY":
            order.goodAfterTime = f"{today_string} 08:30:00 US/Central"
        elif order_row.bracket_role in ("TP", "EXIT"):
            order.goodAfterTime = f"{today_string} 14:59:00 US/Central"
            order.conditionsIgnoreRth = False
            order.conditionsCancelOrder = False

            if order_row.target_price is not None:
                price_condition = PriceCondition()
                price_condition.conId = QQQ_CON_ID
                price_condition.exch = "SMART"
                price_condition.isMore = True  # QQQ Kurs >= target_price
                price_condition.price = float(order_row.target_price)
                price_condition.triggerMethod = 2  # Last Price
                price_condition.conjunction = "a"

                time_condition = TimeCondition()
                time_condition.isMore = False  # Zeit <= 15:00:00 US/Central
                time_condition.time = f"{today_string} 15:00:00 US/Central"
                time_condition.conjunction = "a"

                order.conditions = [price_condition, time_condition]

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
