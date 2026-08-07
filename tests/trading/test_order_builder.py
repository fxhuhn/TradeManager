# filename: tests/trading/test_order_builder.py
"""Unit tests for symbol normalization, contract creation, and order building in app.trading.order_builder."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.core.models import OrderRow
from app.trading.order_builder import (
    build_order,
    get_tick_size,
    make_stock_contract,
    normalize_symbol,
    round_to_tick,
)
from app.trading.recovery import _has_live_position
from app.trading.worker import _get_live_position_quantity


def test_normalize_symbol_strips_dot_de_suffix() -> None:
    """Verifies that normalize_symbol removes .DE suffix and upper-cases ticker."""
    assert normalize_symbol("SXRV.DE") == "SXRV"
    assert normalize_symbol("sxrv.de") == "SXRV"
    assert normalize_symbol("  sxrv.de  ") == "SXRV"


def test_normalize_symbol_preserves_standard_symbols() -> None:
    """Verifies that normalize_symbol preserves tickers without exchange suffixes."""
    assert normalize_symbol("AAPL") == "AAPL"
    assert normalize_symbol("msft") == "MSFT"


def test_normalize_symbol_converts_us_share_class_separators() -> None:
    """Verifies that normalize_symbol converts hyphens and dots in US share classes to spaces."""
    assert normalize_symbol("BF-B") == "BF B"
    assert normalize_symbol("BF.B") == "BF B"
    assert normalize_symbol("BRK-B") == "BRK B"
    assert normalize_symbol("BRK.B") == "BRK B"
    assert normalize_symbol("  bf-b  ") == "BF B"


def test_make_stock_contract_with_dot_de_suffix() -> None:
    """Verifies that make_stock_contract configures XETRA parameters for .DE symbols."""
    contract = make_stock_contract("SXRV.DE")
    assert contract.symbol == "SXRV"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "EUR"
    assert contract.primaryExchange == "IBIS2"


def test_make_stock_contract_for_us_stock() -> None:
    """Verifies that make_stock_contract configures USD SMART routing for US symbols."""
    contract = make_stock_contract("AAPL")
    assert contract.symbol == "AAPL"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"


def test_make_stock_contract_for_us_share_class() -> None:
    """Verifies that make_stock_contract normalizes US share class symbol BF-B to BF B."""
    contract = make_stock_contract("BF-B")
    assert contract.symbol == "BF B"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"


def test_get_live_position_quantity_matches_dot_de_symbol() -> None:
    """Verifies that _get_live_position_quantity matches DB symbol SXRV.DE against IBKR position SXRV."""
    mock_position = MagicMock()
    mock_position.account = "ACCOUNT_123"
    mock_position.contract.symbol = "SXRV"
    mock_position.position = 5.0

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]

    quantity = _get_live_position_quantity(mock_ib, "ACCOUNT_123", "SXRV.DE")
    assert quantity == Decimal("5.0")


def test_has_live_position_matches_dot_de_symbol() -> None:
    """Verifies that _has_live_position returns True when matching SXRV.DE against IBKR position SXRV."""
    mock_position = MagicMock()
    mock_position.account = "ACCOUNT_123"
    mock_position.contract.symbol = "SXRV"
    mock_position.position = 5.0

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]

    assert _has_live_position(mock_ib, "ACCOUNT_123", "SXRV.DE") is True
    assert _has_live_position(mock_ib, "ACCOUNT_999", "SXRV.DE") is False


@pytest.mark.parametrize(
    "symbol, price, expected_tick_size",
    [
        ("SXRV.DE", Decimal("55000.00"), Decimal("10.0")),
        ("SXRV.DE", Decimal("25000.00"), Decimal("5.0")),
        ("SXRV.DE", Decimal("12000.00"), Decimal("2.0")),
        ("SXRV.DE", Decimal("6000.00"), Decimal("1.0")),
        ("SXRV.DE", Decimal("3000.00"), Decimal("0.5")),
        ("SXRV.DE", Decimal("1500.00"), Decimal("0.2")),
        ("SXRV.DE", Decimal("600.00"), Decimal("0.1")),
        ("SXRV.DE", Decimal("300.00"), Decimal("0.05")),
        ("SXRV.DE", Decimal("150.00"), Decimal("0.02")),
        ("SXRV.DE", Decimal("60.00"), Decimal("0.01")),
        ("SXRV.DE", Decimal("30.00"), Decimal("0.005")),
        ("SXRV.DE", Decimal("15.00"), Decimal("0.002")),
        ("SXRV.DE", Decimal("6.00"), Decimal("0.001")),
        ("SXRV.DE", Decimal("3.00"), Decimal("0.0005")),
        ("SXRV.DE", Decimal("1.50"), Decimal("0.0002")),
        ("SXRV.DE", Decimal("0.50"), Decimal("0.0001")),
        ("AAPL", Decimal("150.25"), Decimal("0.01")),
        ("MSFT", Decimal("400.00"), Decimal("0.01")),
    ],
)
def test_get_tick_size_returns_correct_decimal_tick_size(
    symbol: str, price: Decimal, expected_tick_size: Decimal
) -> None:
    """Verifies that get_tick_size returns exact Decimal tick sizes across liquidity bands."""
    actual_tick_size = get_tick_size(symbol, price)
    assert actual_tick_size == expected_tick_size
    assert isinstance(actual_tick_size, Decimal)


@pytest.mark.parametrize(
    "price, tick_size, expected_rounded_price",
    [
        (Decimal("100.0049"), Decimal("0.01"), Decimal("100.00")),
        (Decimal("100.0050"), Decimal("0.01"), Decimal("100.01")),
        (Decimal("100.0070"), Decimal("0.01"), Decimal("100.01")),
        (Decimal("50004.99"), Decimal("10.0"), Decimal("50000.00")),
        (Decimal("50005.00"), Decimal("10.0"), Decimal("50010.00")),
        (Decimal("123.456"), Decimal("0.05"), Decimal("123.45")),
        (Decimal("123.480"), Decimal("0.05"), Decimal("123.50")),
    ],
)
def test_round_to_tick_snaps_price_with_half_up_rounding(
    price: Decimal, tick_size: Decimal, expected_rounded_price: Decimal
) -> None:
    """Verifies that round_to_tick correctly quantizes Decimal prices using ROUND_HALF_UP."""
    actual_rounded_price = round_to_tick(price, tick_size)
    assert actual_rounded_price == expected_rounded_price
    assert isinstance(actual_rounded_price, Decimal)


@pytest.mark.asyncio
async def test_order_builder_order_ref() -> None:
    """Verify that build_order() correctly sets the TWS orderRef field to the strategy name."""
    order_row = OrderRow(
        order_id=42,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("180.0"),
        tif="GTC",
        strategy_name="NDXMomentum",
        status="Created",
    )
    tws_order = build_order(order_row)
    assert tws_order.orderRef == "NDXMomentum"


def test_build_order_rounds_price_to_tick_size() -> None:
    """Verify that build_order() rounds prices to correct tick sizes for .DE and US symbols."""
    # 1. SXRV.DE (price >= 100 -> tick size 0.05)
    order_row_de = OrderRow(
        order_id=647,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="SXRV.DE",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=5,
        order_type="LMT",
        target_price=Decimal("1473.91"),
        tif="DAY",
        strategy_name="TwoPercent",
        status="Created",
    )
    tws_order_de = build_order(order_row_de)
    assert tws_order_de.lmtPrice == 1474.00

    # 2. AAPL (US stock -> tick size 0.01)
    order_row_us = OrderRow(
        order_id=42,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("180.123"),
        tif="GTC",
        strategy_name="NDXMomentum",
        status="Created",
    )
    tws_order_us = build_order(order_row_us)
    assert tws_order_us.lmtPrice == 180.12


@pytest.mark.parametrize(
    "symbol, price, expected_tick",
    [
        ("SXRV.DE", 55000.0, 10.0),
        ("SXRV.DE", 25000.0, 5.0),
        ("SXRV.DE", 12000.0, 2.0),
        ("SXRV.DE", 6000.0, 1.0),
        ("SXRV.DE", 3000.0, 0.5),
        ("SXRV.DE", 1500.0, 0.2),
        ("SXRV.DE", 600.0, 0.1),
        ("SXRV.DE", 300.0, 0.05),
        ("SXRV.DE", 150.0, 0.02),
        ("SXRV.DE", 60.0, 0.01),
        ("SXRV.DE", 30.0, 0.005),
        ("SXRV.DE", 15.0, 0.002),
        ("SXRV.DE", 6.0, 0.001),
        ("SXRV.DE", 3.0, 0.0005),
        ("SXRV.DE", 1.5, 0.0002),
        ("SXRV.DE", 0.5, 0.0001),
        ("AAPL", 150.0, 0.01),
    ],
)
def test_get_tick_size_all_brackets(
    symbol: str, price: float, expected_tick: float
) -> None:
    """Verifiziert die korrekte Ermittlung der Tick-Größe für alle Preisstufen."""
    actual_tick = float(get_tick_size(symbol, Decimal(str(price))))
    assert actual_tick == expected_tick
