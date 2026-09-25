"""
Konstruktion von Verträgen (Contracts) und TWS-Aufträgen (Orders).

Erstellt SMART-Routing US-Aktienkontrakte und konfiguriert
die entsprechenden Stop-, Limit- oder Market-Orders inkl. OCA-Gruppen.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from zoneinfo import ZoneInfo

import structlog
from ib_async import (
    Contract,
    Future,
    Order,
    OrderCondition,
    PriceCondition,
    Stock,
    TimeCondition,
)

from app.core.models import OrderRow

logger = structlog.get_logger()

# Permanente IBKR Contract IDs für US-Basiswert-ETFs (SMART / ARCA / NASDAQ)
QQQ_CON_ID: Final[int] = 320227571
SPY_CON_ID: Final[int] = 756733
IWM_CON_ID: Final[int] = 13317
DIA_CON_ID: Final[int] = 4391

UNDERLYING_ETF_CON_IDS: Final[dict[str, int]] = {
    "QQQ": QQQ_CON_ID,
    "SPY": SPY_CON_ID,
    "IWM": IWM_CON_ID,
    "DIA": DIA_CON_ID,
}

FUTURE_TO_ETF_PREFIX: Final[dict[str, str]] = {
    "MNQ": "QQQ",
    "MES": "SPY",
    "M2K": "IWM",
    "MYM": "DIA",
}

CME_TIMEZONE: Final[ZoneInfo] = ZoneInfo("America/Chicago")

_CME_RTH_CLOSE_HOUR: Final[int] = 15
_CME_RTH_CLOSE_MINUTE: Final[int] = 0

_CME_LOC_ACTIVATION_HOUR: Final[int] = 14
_CME_LOC_ACTIVATION_MINUTE: Final[int] = 59

_CME_RTH_OPEN_HOUR: Final[int] = 8
_CME_RTH_OPEN_MINUTE: Final[int] = 30

_US_GTD_CUTOFF_HOUR: Final[int] = 15
_US_GTD_CUTOFF_MINUTE: Final[int] = 48

_XETRA_GTD_CUTOFF_HOUR: Final[int] = 17
_XETRA_GTD_CUTOFF_MINUTE: Final[int] = 18

_PRICE_TRIGGER_METHOD_LAST: Final[int] = 2

_OCA_TYPE_CANCEL_ALL: Final[int] = 1
_OCA_TYPE_REDUCE_WITH_NO_BLOCK: Final[int] = 3
_OCA_GROUP_VERSION_SUFFIX: Final[str] = "_v4"


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

    Raises:
        ValueError: Wenn weder ein konkretes LocalSymbol mit Ziffern noch ein Verfallsmonat übergeben wurde.
    """
    clean_symbol = normalize_symbol(symbol)
    if len(clean_symbol) > 3 and any(char.isdigit() for char in clean_symbol):
        return Future(
            localSymbol=clean_symbol,
            exchange=exchange,
            currency=currency,
        )
    if not contract_month:
        raise ValueError(
            f"Cannot build Future contract for '{symbol}': "
            "a localSymbol (e.g. 'MNQU6') or contract_month (e.g. '20260918') is required."
        )
    return Future(
        symbol=clean_symbol,
        lastTradeDateOrContractMonth=contract_month,
        exchange=exchange,
        currency=currency,
    )


def make_contract_by_type(
    symbol: str,
    sec_type: str = "STK",
    exchange: str = "SMART",
    currency: str = "USD",
) -> Contract:
    """Erstellt ein passendes Contract-Objekt (Stock oder Future) anhand von sec_type und Symbol.

    Args:
        symbol: Ticker-Symbol oder Future LocalSymbol (z. B. 'AAPL', 'MNQU6').
        sec_type: Wertpapiertyp ('STK' oder 'FUT').
        exchange: Zielbörse (z. B. 'SMART' oder 'CME').
        currency: Kontraktwährung (Standard: 'USD').

    Returns:
        Ein konfiguriertes Stock- oder Future-Objekt für die IBKR API.
    """
    if (sec_type or "STK").upper() == "FUT":
        return make_future_contract(
            symbol=symbol,
            exchange=exchange or "CME",
            currency=currency,
        )
    return make_stock_contract(symbol)


def make_contract_for_order(order_row: OrderRow) -> Contract:
    """Erstellt ein passendes Contract-Objekt (Stock oder Future) für die gegebene OrderRow."""
    return make_contract_by_type(
        symbol=order_row.symbol,
        sec_type=order_row.sec_type,
        exchange=order_row.exchange,
    )


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
_FUTURES_CME_TICK_SIZE: Decimal = Decimal("0.25")
_XETRA_MIN_TICK_SIZE: Decimal = Decimal("0.0001")


def get_underlying_etf_info(future_symbol: str) -> tuple[str, int] | None:
    """Ermittelt Basiswert-Symbol und ConID für ein CME-Future-Symbol."""
    clean_sym = normalize_symbol(future_symbol).upper()
    for fut_prefix, etf_sym in FUTURE_TO_ETF_PREFIX.items():
        if clean_sym.startswith(fut_prefix):
            con_id = UNDERLYING_ETF_CON_IDS.get(etf_sym)
            if con_id is not None:
                return etf_sym, con_id
    return None


def get_tick_size(
    symbol: str, price: Decimal | float, sec_type: str = "STK"
) -> Decimal:
    """Ermittelt die minimale Preisänderung (Tick Size) als Decimal für ein Symbol."""
    if sec_type.upper() == "FUT" or any(
        symbol.upper().startswith(prefix) for prefix in ("MNQ", "MES", "M2K", "MYM")
    ):
        return _FUTURES_CME_TICK_SIZE

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


def _build_future_time_window(
    raw_order_type: str,
    today_string: str,
    current_time: datetime,
) -> tuple[str | None, TimeCondition | None]:
    """Ermittelt goodAfterTime und TimeCondition für eine CME-Future-Order.

    WICHTIG: goodAfterTime darf NUR gesetzt werden, wenn der Zeitpunkt in der Zukunft liegt!
    Liegt der Zeitpunkt in der Vergangenheit, lehnt IBKR die Order mit Error 201 'Invalid effective time' ab.
    """
    good_after_time: str | None = None
    time_condition: TimeCondition | None = None
    rth_close = current_time.replace(
        hour=_CME_RTH_CLOSE_HOUR,
        minute=_CME_RTH_CLOSE_MINUTE,
        second=0,
        microsecond=0,
    )

    if raw_order_type in ("LOC", "MOC"):
        loc_activation = current_time.replace(
            hour=_CME_LOC_ACTIVATION_HOUR,
            minute=_CME_LOC_ACTIVATION_MINUTE,
            second=0,
            microsecond=0,
        )
        if current_time < loc_activation:
            good_after_time = f"{today_string} 14:59:00 US/Central"

        if current_time < rth_close:
            time_condition = TimeCondition()
            time_condition.isMore = False  # Gültig bis zum Ende der RTH (15:00 Central)
            time_condition.time = f"{today_string} 15:00:00 US/Central"
            time_condition.conjunction = "a"
    elif raw_order_type in ("LMT", "STP"):
        rth_open = current_time.replace(
            hour=_CME_RTH_OPEN_HOUR,
            minute=_CME_RTH_OPEN_MINUTE,
            second=0,
            microsecond=0,
        )
        if current_time < rth_open:
            good_after_time = f"{today_string} 08:30:00 US/Central"

        if current_time < rth_close:
            time_condition = TimeCondition()
            time_condition.isMore = False  # Gültig bis zum Ende der RTH (15:00 Central)
            time_condition.time = f"{today_string} 15:00:00 US/Central"
            time_condition.conjunction = "a"
    elif raw_order_type == "MKT":
        rth_open = current_time.replace(
            hour=_CME_RTH_OPEN_HOUR,
            minute=_CME_RTH_OPEN_MINUTE,
            second=0,
            microsecond=0,
        )
        if current_time < rth_open:
            good_after_time = f"{today_string} 08:30:00 US/Central"

    return good_after_time, time_condition


def _build_future_price_condition(
    order_row: OrderRow,
    raw_order_type: str,
) -> PriceCondition | None:
    """Erstellt eine PriceCondition auf den Basiswert-ETF für eine CME-Future-Order."""
    if order_row.target_price is None or raw_order_type not in ("LMT", "LOC", "STP"):
        return None

    etf_info = get_underlying_etf_info(order_row.symbol)
    if etf_info is None:
        logger.warning(
            "Kein Basiswert-ETF für Future-Symbol gefunden. Keine Preiskondition gesetzt.",
            symbol=order_row.symbol,
        )
        return None

    _, con_id = etf_info
    price_cond = PriceCondition()
    price_cond.conId = con_id
    price_cond.exch = "SMART"
    price_cond.price = float(order_row.target_price)
    price_cond.triggerMethod = _PRICE_TRIGGER_METHOD_LAST
    price_cond.conjunction = "a"

    # Trigger-Richtung (isMore):
    # Bei BUY (LMT/LOC): Kaufen, wenn Kurs fällt auf/unter Ziel (isMore = False)
    # Bei BUY STP (Breakout): Kaufen, wenn Kurs steigt auf/über Stop (isMore = True)
    # Bei SELL TP/EXIT: Verkaufen, wenn Kurs steigt auf/über Ziel (isMore = True)
    # Bei SELL SL: Verkaufen, wenn Kurs fällt auf/unter Stop (isMore = False)
    if order_row.action.upper() == "BUY":
        price_cond.isMore = raw_order_type == "STP"
    else:  # SELL
        price_cond.isMore = (
            order_row.bracket_role not in ("SL",) and raw_order_type != "STP"
        )

    return price_cond


def apply_conditioned_future_order(
    order: Order,
    order_row: OrderRow,
    today_string: str,
    now: datetime | None = None,
) -> None:
    """Wendet die universellen Handelszeit- und Preistrigger-Bedingungen für Future-Orders an.

    Transformiert die Order in eine MKT-Order auf den Future mit Ausführung außerhalb der RTH,
    gesteuert über Price- und Time-Conditions auf den zugrunde liegenden US-ETF.
    """
    order.orderType = "MKT"
    order.outsideRth = True
    order.tif = "DAY"
    order.conditionsIgnoreRth = False
    order.conditionsCancelOrder = False

    raw_order_type = order_row.order_type.upper() if order_row.order_type else "MKT"
    current_time = now if now is not None else datetime.now(CME_TIMEZONE)

    good_after_time, time_condition = _build_future_time_window(
        raw_order_type, today_string, current_time
    )
    if good_after_time:
        order.goodAfterTime = good_after_time

    conditions: list[OrderCondition] = []
    price_condition = _build_future_price_condition(order_row, raw_order_type)
    if price_condition is not None:
        conditions.append(price_condition)
    if time_condition is not None:
        conditions.append(time_condition)

    order.conditions = conditions


def _configure_order_prices(order: Order, order_row: OrderRow) -> None:
    """Weist der Order je nach Order-Typ die tick-gerundeten Preise zu."""
    if order.orderType in ("LMT", "LOC"):
        # Standard Aktien/ETF-Orders (sec_type == "STK")
        # Runden auf die minimale Tick-Größe des Zielmarkts
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


def _configure_oca_group(order: Order, order_row: OrderRow) -> None:
    """Konfiguriert die OCA-Gruppe (One-Cancels-All) für Stop-Loss, Take-Profit und Exits."""
    if order_row.bracket_role not in ("SL", "TP", "EXIT"):
        return

    # Alle Legs derselben trade_group_id tragen denselben OCA-String.
    # Wir hängen _v4 an, um Probleme mit dem TWS Session-Memory zu umgehen.
    order.ocaGroup = f"OCA_{order_row.trade_group_id}{_OCA_GROUP_VERSION_SUFFIX}"

    # LOC und MOC Orders dürfen laut IBKR nur mit ocaType = 3 (reduce with no block) in einer OCA Gruppe sein.
    # Auch bei EXIT-Orders nutzen wir ocaType = 3, da sie standardmässig aus LMT + LOC Exits bestehen.
    if order.orderType in ("LOC", "MOC") or order_row.bracket_role == "EXIT":
        order.ocaType = _OCA_TYPE_REDUCE_WITH_NO_BLOCK
    else:
        order.ocaType = _OCA_TYPE_CANCEL_ALL


def build_order(order_row: OrderRow, now: datetime | None = None) -> Order:
    """Konstruiert ein ib_async Order-Objekt aus den DB-Orderzeilen.

    Berücksichtigt Order-Typen und OCA-Konfigurationen für Stop-Loss (SL) und Take-Profit (TP).
    """
    order = Order()
    order.orderId = order_row.order_id
    order.action = order_row.action.upper()
    order.totalQuantity = float(order_row.quantity)
    order.orderType = order_row.order_type.upper()
    order.tif = order_row.tif.upper() if order_row.tif else "GTC"

    # Strategie-Name im Order Reference-Feld für TWS hinterlegen
    if order_row.strategy_name:
        order.orderRef = order_row.strategy_name

    # Future-Orders: Universelle Behandlung via CME Globex MKT + ETF Conditions
    if order_row.sec_type == "FUT":
        ref_time = now if now is not None else datetime.now(CME_TIMEZONE)
        today_string = ref_time.strftime("%Y%m%d")
        apply_conditioned_future_order(order, order_row, today_string, now=ref_time)

        # Defensive Härtung für alle Futures: CME Globex unterstützt kein OPG
        if (order_row.tif or "").upper() == "OPG":
            logger.warning(
                "TIF 'OPG' ist für Futures unzulässig. Wird automatisch auf 'DAY' korrigiert.",
                symbol=order_row.symbol,
                order_id=order_row.order_id,
            )
            order.tif = "DAY"
    else:
        _configure_order_prices(order, order_row)

    _configure_oca_group(order, order_row)
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


def should_apply_loc_gtd(child: OrderRow, sibling_orders: Sequence[OrderRow]) -> bool:
    """Prüft, ob eine untergeordnete Order mit TIF=GTD versehen werden muss.

    Verhindert das Overfill-Risiko bei OCA-Gruppen mit ocaType=3:
    Falls eine Limit-Exit-Order ('TP' oder 'EXIT') eine Schwester-Order mit
    Typ 'LOC' oder 'MOC' hat, muss die Limit-Order vor dem regulatorischen
    Cut-Off der Börse (15:50 US/Eastern) automatisch verfallen.

    Args:
        child: Die zu prüfende untergeordnete Order.
        sibling_orders: Alle untergeordneten Geschwister-Orders der Trade-Gruppe.

    Returns:
        True, falls child eine LMT-Exit-Order ist und mindestens eine Geschwister-Order
        vom Typ LOC oder MOC existiert, sonst False.
    """
    if child.bracket_role not in ("TP", "EXIT"):
        return False
    if child.order_type.upper() != "LMT":
        return False

    return any(
        sibling.order_type.upper() in ("LOC", "MOC")
        and sibling.order_id != child.order_id
        for sibling in sibling_orders
    )


def _calculate_loc_gtd_cutoff_datetime(
    symbol: str, reference_time: datetime | None = None
) -> tuple[datetime, datetime, str]:
    """Berechnet den GTD-Cutoff-Zeitpunkt sowie den aktuellen Referenzzeitpunkt in der Zielzeitzone.

    Args:
        symbol: Das Ticker-Symbol des Wertpapiers.
        reference_time: Optionaler Referenzzeitpunkt (für Tests). Falls None, wird die aktuelle Zeit verwendet.

    Returns:
        Ein Tupel aus (now_in_target_tz, cutoff_datetime, timezone_name).
    """
    symbol_upper = symbol.strip().upper()
    if symbol_upper.endswith(".DE"):
        target_timezone = ZoneInfo("Europe/Berlin")
        cutoff_hour = _XETRA_GTD_CUTOFF_HOUR
        cutoff_minute = _XETRA_GTD_CUTOFF_MINUTE
        timezone_name = "Europe/Berlin"
    else:
        target_timezone = ZoneInfo("America/New_York")
        cutoff_hour = _US_GTD_CUTOFF_HOUR
        cutoff_minute = _US_GTD_CUTOFF_MINUTE
        timezone_name = "US/Eastern"

    if reference_time is None:
        now_in_target_tz = datetime.now(target_timezone)
    elif reference_time.tzinfo is None:
        now_in_target_tz = reference_time.replace(tzinfo=target_timezone)
    else:
        now_in_target_tz = reference_time.astimezone(target_timezone)

    cutoff_datetime = now_in_target_tz.replace(
        hour=cutoff_hour,
        minute=cutoff_minute,
        second=0,
        microsecond=0,
    )
    return now_in_target_tz, cutoff_datetime, timezone_name


def compute_loc_gtd_cutoff(symbol: str, reference_time: datetime | None = None) -> str:
    """Berechnet den Good-Till-Date (GTD) Verfallszeitpunkt für Limit-Exit-Orders.

    Setzt den Verfallszeitpunkt auf 12 Minuten vor regulärem Marktschluss
    (2 Minuten Sicherheitsabstand vor dem harten 15:50 US/Eastern Cut-Off der Börse).

    - US-Märkte (Standard): 15:48:00 US/Eastern
    - Deutsche Märkte (Suffix '.DE'): 17:18:00 Europe/Berlin

    Args:
        symbol: Das Ticker-Symbol des Wertpapiers.
        reference_time: Optionaler Referenzzeitpunkt (für Tests). Falls None, wird die aktuelle Zeit verwendet.

    Returns:
        Formatierter TWS API GTD-String: 'YYYYMMDD HH:mm:ss {TZ}' (z. B. '20260910 15:48:00 US/Eastern').
    """
    _, cutoff_datetime, timezone_name = _calculate_loc_gtd_cutoff_datetime(
        symbol, reference_time
    )
    return f"{cutoff_datetime.strftime('%Y%m%d %H:%M:%S')} {timezone_name}"


def is_past_loc_gtd_cutoff(symbol: str, current_time: datetime | None = None) -> bool:
    """Prüft, ob der GTD-Verfallszeitpunkt für ein Symbol bereits erreicht oder überschritten ist.

    Args:
        symbol: Das Ticker-Symbol des Wertpapiers.
        current_time: Optionaler Referenzzeitpunkt (für Tests). Falls None, wird die aktuelle Zeit verwendet.

    Returns:
        True, wenn der aktuelle Zeitpunkt gleich oder nach dem GTD-Cutoff liegt.
    """
    now_in_target_tz, cutoff_datetime, _ = _calculate_loc_gtd_cutoff_datetime(
        symbol, current_time
    )
    return now_in_target_tz >= cutoff_datetime
