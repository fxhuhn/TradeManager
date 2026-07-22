# filename: tests/test_order_builder_decimal.py
"""Unit tests for Decimal tick size precision in order_builder.py."""

from decimal import Decimal

import pytest

from app.trading.order_builder import get_tick_size, round_to_tick


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
    # Act
    actual_tick_size = get_tick_size(symbol, price)

    # Assert
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
    # Act
    actual_rounded_price = round_to_tick(price, tick_size)

    # Assert
    assert actual_rounded_price == expected_rounded_price
    assert isinstance(actual_rounded_price, Decimal)
