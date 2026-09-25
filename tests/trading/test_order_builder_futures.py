"""Unit-Tests für Future-Kontrakterstellung und BounceBandit-Order-Konfiguration."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ib_async import Future, PriceCondition, Stock, TimeCondition

from app.core.models import OrderRow
from app.trading.order_builder import (
    CME_TIMEZONE,
    QQQ_CON_ID,
    SPY_CON_ID,
    build_order,
    get_underlying_etf_info,
    make_contract_for_order,
    make_future_contract,
)


def test_make_future_contract() -> None:
    """Prüft die Erstellung von Future-Kontrakten via LocalSymbol und Basis-Symbol."""
    contract_local = make_future_contract("MNQU6", exchange="CME")
    assert isinstance(contract_local, Future)
    assert contract_local.localSymbol == "MNQU6"
    assert contract_local.exchange == "CME"

    contract_month = make_future_contract(
        "MNQ", contract_month="20260918", exchange="CME"
    )
    assert isinstance(contract_month, Future)
    assert contract_month.symbol == "MNQ"
    assert contract_month.lastTradeDateOrContractMonth == "20260918"

    import pytest

    with pytest.raises(ValueError, match="Cannot build Future contract for 'MNQ'"):
        make_future_contract("MNQ", exchange="CME")


def test_make_contract_for_order() -> None:
    """Prüft, ob je nach sec_type ein Stock- oder Future-Vertrag erstellt wird."""
    stock_row = OrderRow(
        order_id=1,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_1",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=10,
        order_type="MKT",
        target_price=None,
        tif="GTC",
        strategy_name="DipBuyer",
        status="Created",
    )
    contract_stock = make_contract_for_order(stock_row)
    assert isinstance(contract_stock, Stock)
    assert contract_stock.symbol == "AAPL"

    future_row = OrderRow(
        order_id=2,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_2",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MNQU6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="MKT",
        target_price=None,
        tif="DAY",
        strategy_name="BounceBandit",
        status="Created",
    )
    contract_future = make_contract_for_order(future_row)
    assert isinstance(contract_future, Future)
    assert contract_future.localSymbol == "MNQU6"
    assert contract_future.exchange == "CME"


def test_build_order_bounce_bandit_entry() -> None:
    """Prüft die Order-Parameter für einen BounceBandit MNQ Future ENTRY."""
    entry_row = OrderRow(
        order_id=10,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_BB_1",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MNQU6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="MKT",
        target_price=None,
        tif="DAY",
        strategy_name="BounceBandit",
        status="Created",
    )
    # Vor Markteröffnung (07:00 Central): goodAfterTime wird auf 08:30 Central gesetzt
    morning_time = datetime(2026, 9, 21, 7, 0, 0, tzinfo=CME_TIMEZONE)
    ib_order = build_order(entry_row, now=morning_time)

    assert ib_order.action == "BUY"
    assert ib_order.orderType == "MKT"
    assert ib_order.totalQuantity == 1.0
    assert ib_order.outsideRth is True
    assert "08:30:00 US/Central" in ib_order.goodAfterTime


def test_build_order_bounce_bandit_tp_with_conditions() -> None:
    """Prüft die Order-Parameter und Bedingungen für einen BounceBandit MNQ Future TP."""
    tp_row = OrderRow(
        order_id=11,
        perm_id=None,
        parent_id=10,
        trade_group_id="TG_BB_1",
        account_id="ACC1",
        bracket_role="TP",
        symbol="MNQU6",
        sec_type="FUT",
        exchange="CME",
        action="SELL",
        quantity=1,
        order_type="LOC",
        target_price=Decimal("714.80"),
        tif="DAY",
        strategy_name="BounceBandit",
        status="Created",
    )
    # Vor RTH Close (12:00 Central): goodAfterTime wird auf 14:59 Central gesetzt
    noon_time = datetime(2026, 9, 21, 12, 0, 0, tzinfo=CME_TIMEZONE)
    ib_order = build_order(tp_row, now=noon_time)

    assert ib_order.action == "SELL"
    assert ib_order.orderType == "MKT"
    assert ib_order.totalQuantity == 1.0
    assert ib_order.outsideRth is True
    assert "14:59:00 US/Central" in ib_order.goodAfterTime
    assert len(ib_order.conditions) == 2

    pc = ib_order.conditions[0]
    assert isinstance(pc, PriceCondition)
    assert pc.conId == QQQ_CON_ID
    assert pc.price == 714.80
    assert pc.isMore is True

    tc = ib_order.conditions[1]
    assert isinstance(tc, TimeCondition)
    assert "15:00:00 US/Central" in tc.time
    assert tc.isMore is False


def test_get_underlying_etf_info() -> None:
    """Prüft die universelle Auflösung von Future-Symbolen auf Basiswert-ETFs und ConIDs."""
    assert get_underlying_etf_info("MNQU6") == ("QQQ", QQQ_CON_ID)
    assert get_underlying_etf_info("MNQZ6") == ("QQQ", QQQ_CON_ID)
    assert get_underlying_etf_info("MESU6") == ("SPY", SPY_CON_ID)
    assert get_underlying_etf_info("MESZ6") == ("SPY", SPY_CON_ID)
    assert get_underlying_etf_info("M2KU6") == ("IWM", 13317)
    assert get_underlying_etf_info("MYMU6") == ("DIA", 4391)
    assert get_underlying_etf_info("XYZU6") is None


def test_build_order_tgim_spy_loc_entry_conditioned() -> None:
    """Prüft, dass TGIM BUY SPY LOC als MES MKT mit PriceCondition (SPY <= price) kurz vor Close aufgebaut wird."""
    entry_row = OrderRow(
        order_id=20,
        perm_id=None,
        parent_id=None,
        trade_group_id="1529_TGIM_SPY",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MESZ6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="LOC",
        target_price=Decimal("760.71"),
        tif="DAY",
        strategy_name="TGIM",
        status="Created",
    )
    # Vor RTH Close (12:00 Central): goodAfterTime wird auf 14:59 Central gesetzt
    noon_time = datetime(2026, 9, 21, 12, 0, 0, tzinfo=CME_TIMEZONE)
    ib_order = build_order(entry_row, now=noon_time)

    assert ib_order.action == "BUY"
    assert ib_order.orderType == "MKT"
    assert ib_order.totalQuantity == 1.0
    assert ib_order.outsideRth is True
    assert "14:59:00 US/Central" in ib_order.goodAfterTime
    assert len(ib_order.conditions) == 2

    pc = ib_order.conditions[0]
    assert isinstance(pc, PriceCondition)
    assert pc.conId == SPY_CON_ID
    assert pc.price == 760.71
    assert pc.isMore is False  # Kaufen wenn SPY <= 760.71

    tc = ib_order.conditions[1]
    assert isinstance(tc, TimeCondition)
    assert "15:00:00 US/Central" in tc.time
    assert tc.isMore is False


def test_build_order_two_percent_qqq_lmt_entry_conditioned() -> None:
    """Prüft, dass TwoPercent BUY QQQ LMT als MNQ MKT mit PriceCondition (QQQ <= price) während RTH aufgebaut wird."""
    entry_row = OrderRow(
        order_id=21,
        perm_id=None,
        parent_id=None,
        trade_group_id="1528_TwoPercent_QQQ",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MNQZ6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="LMT",
        target_price=Decimal("714.24"),
        tif="DAY",
        strategy_name="TwoPercent",
        status="Created",
    )
    # Vor Markteröffnung (07:00 Central): goodAfterTime wird auf 08:30 Central gesetzt
    morning_time = datetime(2026, 9, 21, 7, 0, 0, tzinfo=CME_TIMEZONE)
    ib_order = build_order(entry_row, now=morning_time)

    assert ib_order.action == "BUY"
    assert ib_order.orderType == "MKT"
    assert ib_order.totalQuantity == 1.0
    assert ib_order.outsideRth is True
    assert "08:30:00 US/Central" in ib_order.goodAfterTime
    assert len(ib_order.conditions) == 2

    pc = ib_order.conditions[0]
    assert isinstance(pc, PriceCondition)
    assert pc.conId == QQQ_CON_ID
    assert pc.price == 714.24
    assert pc.isMore is False  # Kaufen wenn QQQ <= 714.24

    tc = ib_order.conditions[1]
    assert isinstance(tc, TimeCondition)
    assert "15:00:00 US/Central" in tc.time
    assert tc.isMore is False


def test_build_order_future_lmt_omits_good_after_time_if_past_open() -> None:
    """Prüft, dass goodAfterTime weggelassen wird, wenn die Order nach 08:30 Central (Market Open) gesendet wird."""
    entry_row = OrderRow(
        order_id=25,
        perm_id=None,
        parent_id=None,
        trade_group_id="1528_TwoPercent_QQQ",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MNQZ6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="LMT",
        target_price=Decimal("714.24"),
        tif="DAY",
        strategy_name="TwoPercent",
        status="Created",
    )
    # 12:47:34 US/Central (wie im heutigen Prod-Fehler nach Eröffnung)
    now_past_open = datetime(2026, 9, 21, 12, 47, 34, tzinfo=CME_TIMEZONE)
    ib_order = build_order(entry_row, now=now_past_open)

    assert ib_order.orderType == "MKT"
    assert (
        ib_order.goodAfterTime == ""
    )  # Darf NICHT gesetzt werden, um 'Invalid effective time' zu verhindern
    assert len(ib_order.conditions) == 2


def test_build_order_two_percent_sxrv_stock_unaffected() -> None:
    """Prüft, dass TwoPercent mit SXRV.DE (Aktie/ETF) unverändert als reguläre STK LMT-Order mit Xetra-Tick ausgeführt wird."""
    entry_row = OrderRow(
        order_id=22,
        perm_id=None,
        parent_id=None,
        trade_group_id="1527_TwoPercent_SXRV.DE",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="SXRV.DE",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=5,
        order_type="LMT",
        target_price=Decimal("1460.45"),
        tif="DAY",
        strategy_name="TwoPercent",
        status="Created",
    )
    ib_order = build_order(entry_row)

    assert ib_order.action == "BUY"
    assert ib_order.orderType == "LMT"
    assert ib_order.lmtPrice == 1460.40  # 1460.45 gerundet auf 0.20 Xetra Tick
    assert ib_order.totalQuantity == 5.0
    assert ib_order.conditions == []


def test_build_order_unmapped_future_without_etf_conditions() -> None:
    """Prüft, dass nicht gemappte Futures ohne PriceCondition mit TimeCondition aufgebaut werden."""
    entry_row = OrderRow(
        order_id=23,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_CUSTOM_FUT",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="XYZU6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="LMT",
        target_price=Decimal("100.00"),
        tif="DAY",
        strategy_name="CustomStrategy",
        status="Created",
    )
    ib_order = build_order(entry_row)

    assert ib_order.action == "BUY"
    assert ib_order.orderType == "MKT"
    assert len(ib_order.conditions) == 1
    assert isinstance(ib_order.conditions[0], TimeCondition)


def test_build_order_bounce_bandit_forces_tif_day_even_if_row_has_opg() -> None:
    """Prüft, dass BounceBandit Entry-Orders stets tif='DAY' haben, selbst wenn in der DB tif='OPG' steht."""
    entry_row = OrderRow(
        order_id=30,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_BB_OPG",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MNQU6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="MKT",
        target_price=None,
        tif="OPG",
        strategy_name="BounceBandit",
        status="Created",
    )
    ib_order = build_order(entry_row)

    assert ib_order.tif == "DAY"
    assert ib_order.orderType == "MKT"
    assert ib_order.outsideRth is True


def test_build_order_future_with_opg_defensively_corrected_to_day() -> None:
    """Prüft, dass jede Future-Order mit tif='OPG' defensiv auf tif='DAY' korrigiert wird (CME Globex Invariante)."""
    future_row = OrderRow(
        order_id=31,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_GEN_FUT",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MESU6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="MKT",
        target_price=None,
        tif="OPG",
        strategy_name="ArbitraryStrategy",
        status="Created",
    )
    ib_order = build_order(future_row)

    assert ib_order.tif == "DAY"


def test_conditioned_future_order_after_hours_and_boundaries() -> None:
    """Verifies that goodAfterTime and TimeConditions are omitted when current_time is past the thresholds."""
    from ib_async import Order

    from app.trading.order_builder import apply_conditioned_future_order

    # 1. Past RTH close (15:30 Central) for LMT
    late_time = datetime(2026, 9, 21, 15, 30, 0, tzinfo=CME_TIMEZONE)
    lmt_row = OrderRow(
        order_id=50,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_LATE",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MNQU6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="LMT",
        target_price=Decimal("700.0"),
        tif="DAY",
        strategy_name="Strat",
        status="Created",
    )
    late_order = Order()
    apply_conditioned_future_order(late_order, lmt_row, "20260921", now=late_time)
    assert late_order.goodAfterTime == ""
    # Should only have price condition, no time condition
    assert len(late_order.conditions) == 1
    assert isinstance(late_order.conditions[0], PriceCondition)

    # 2. LOC order right at 14:59:30 (past loc_activation, but before 15:00 close)
    near_close = datetime(2026, 9, 21, 14, 59, 30, tzinfo=CME_TIMEZONE)
    loc_row = OrderRow(
        order_id=51,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_LOC",
        account_id="ACC1",
        bracket_role="TP",
        symbol="MNQU6",
        sec_type="FUT",
        exchange="CME",
        action="SELL",
        quantity=1,
        order_type="LOC",
        target_price=Decimal("710.0"),
        tif="DAY",
        strategy_name="Strat",
        status="Created",
    )
    loc_order = Order()
    apply_conditioned_future_order(loc_order, loc_row, "20260921", now=near_close)
    assert loc_order.goodAfterTime == ""
    assert any(isinstance(c, TimeCondition) for c in loc_order.conditions)

    # 3. MKT order during regular hours (10:00 Central)
    mid_day = datetime(2026, 9, 21, 10, 0, 0, tzinfo=CME_TIMEZONE)
    mkt_row = OrderRow(
        order_id=52,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_MKT",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MNQU6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="MKT",
        target_price=None,
        tif="DAY",
        strategy_name="Strat",
        status="Created",
    )
    mkt_order = Order()
    apply_conditioned_future_order(mkt_order, mkt_row, "20260921", now=mid_day)
    assert mkt_order.goodAfterTime == ""
    assert len(mkt_order.conditions) == 0

    # 4. Unknown future symbol with target price -> no price condition added
    unknown_fut_row = OrderRow(
        order_id=53,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_UNK",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="CLZ6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="LMT",
        target_price=Decimal("75.0"),
        tif="DAY",
        strategy_name="Strat",
        status="Created",
    )
    unk_order = Order()
    morning_time = datetime(2026, 9, 21, 8, 0, 0, tzinfo=CME_TIMEZONE)
    apply_conditioned_future_order(
        unk_order, unknown_fut_row, "20260921", now=morning_time
    )
    assert not any(isinstance(c, PriceCondition) for c in unk_order.conditions)


def test_conditioned_future_order_price_condition_directions() -> None:
    """Verifies isMore logic for BUY STP, BUY LMT, SELL SL, and SELL TP."""
    from ib_async import Order

    from app.trading.order_builder import apply_conditioned_future_order

    morning_time = datetime(2026, 9, 21, 8, 0, 0, tzinfo=CME_TIMEZONE)

    # BUY STP (Breakout): isMore == True
    buy_stp = OrderRow(
        order_id=60,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_STP",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MESZ6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="STP",
        target_price=Decimal("500.0"),
        tif="DAY",
        strategy_name="Strat",
        status="Created",
    )
    order_buy_stp = Order()
    apply_conditioned_future_order(order_buy_stp, buy_stp, "20260921", now=morning_time)
    assert order_buy_stp.conditions[0].isMore is True

    # SELL SL: isMore == False
    sell_sl = OrderRow(
        order_id=61,
        perm_id=None,
        parent_id=60,
        trade_group_id="TG_STP",
        account_id="ACC1",
        bracket_role="SL",
        symbol="MESZ6",
        sec_type="FUT",
        exchange="CME",
        action="SELL",
        quantity=1,
        order_type="STP",
        target_price=Decimal("490.0"),
        tif="DAY",
        strategy_name="Strat",
        status="Created",
    )
    order_sell_sl = Order()
    apply_conditioned_future_order(order_sell_sl, sell_sl, "20260921", now=morning_time)
    assert order_sell_sl.conditions[0].isMore is False
