"""Unit-Tests für Future-Kontrakterstellung und BounceBandit-Order-Konfiguration."""

from __future__ import annotations

from decimal import Decimal

from ib_async import Future, PriceCondition, Stock, TimeCondition

from app.core.models import OrderRow
from app.trading.order_builder import (
    QQQ_CON_ID,
    build_order,
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
    ib_order = build_order(entry_row)

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
    ib_order = build_order(tp_row)

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


def test_build_order_generic_future_without_bounce_bandit_conditions() -> None:
    """Prüft, dass generische Futures-Orders anderer Strategien reguläre LMT/STP-Orders ohne BB-Konditionen bleiben."""
    entry_row = OrderRow(
        order_id=20,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_SPX_1",
        account_id="ACC1",
        bracket_role="ENTRY",
        symbol="MESU6",
        sec_type="FUT",
        exchange="CME",
        action="BUY",
        quantity=1,
        order_type="LMT",
        target_price=Decimal("5500.25"),
        tif="GTC",
        strategy_name="SpxTrend",
        status="Created",
    )
    ib_order = build_order(entry_row)

    assert ib_order.action == "BUY"
    assert ib_order.orderType == "LMT"
    assert ib_order.lmtPrice == 5500.25
    assert ib_order.totalQuantity == 1.0
    assert ib_order.goodAfterTime == ""
    assert ib_order.conditions == []
