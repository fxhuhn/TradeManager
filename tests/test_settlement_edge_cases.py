# filename: tests/test_settlement_edge_cases.py
"""Unit tests for settlement calculation edge cases and financial precision."""

from decimal import Decimal

from app.trading.settlement import (
    ExecutionTuple,
    SettlementInput,
    SettlementOutput,
    calculate_settlement,
)


def test_calculate_settlement_handles_zero_entry_quantity_gracefully() -> None:
    """Verifies that calculate_settlement returns 0.0 VWAP when entry executions sum to 0 quantity."""
    # Arrange
    settlement_input = SettlementInput(
        entry_executions=[],
        exit_executions=[
            ExecutionTuple(quantity=Decimal("100"), price=Decimal("150.00"))
        ],
        entry_target_price=Decimal("140.00"),
        entry_action="BUY",
        total_commissions=Decimal("1.00"),
    )

    # Act
    output: SettlementOutput = calculate_settlement(settlement_input)

    # Assert
    assert output.avg_entry_price == Decimal("0.0")
    assert output.avg_exit_price == Decimal("150.00")
    assert output.net_profit_loss == Decimal("-1.00")


def test_calculate_settlement_handles_zero_exit_quantity_gracefully() -> None:
    """Verifies that calculate_settlement returns 0.0 exit VWAP when exit executions sum to 0 quantity."""
    # Arrange
    settlement_input = SettlementInput(
        entry_executions=[
            ExecutionTuple(quantity=Decimal("100"), price=Decimal("140.00"))
        ],
        exit_executions=[],
        entry_target_price=Decimal("140.00"),
        entry_action="BUY",
        total_commissions=Decimal("1.50"),
    )

    # Act
    output: SettlementOutput = calculate_settlement(settlement_input)

    # Assert
    assert output.avg_entry_price == Decimal("140.00")
    assert output.avg_exit_price == Decimal("0.0")
    assert output.net_profit_loss == Decimal("-14001.50")


def test_calculate_settlement_handles_short_sell_position_profit() -> None:
    """Verifies that SELL (short entry) position PnL is positive when exit price is lower than entry price."""
    # Arrange
    settlement_input = SettlementInput(
        entry_executions=[
            ExecutionTuple(quantity=Decimal("50"), price=Decimal("200.00")),
            ExecutionTuple(quantity=Decimal("50"), price=Decimal("210.00")),
        ],  # VWAP = 205.00
        exit_executions=[
            ExecutionTuple(quantity=Decimal("100"), price=Decimal("190.00"))
        ],  # Exit VWAP = 190.00
        entry_target_price=Decimal("200.00"),
        entry_action="SELL",
        total_commissions=Decimal("2.00"),
    )

    # Act
    output: SettlementOutput = calculate_settlement(settlement_input)

    # Assert
    assert output.avg_entry_price == Decimal("205.00")
    assert output.avg_exit_price == Decimal("190.00")
    # Slippage for SELL: avg_entry - target = 205.00 - 200.00 = +5.00
    assert output.price_diff_slippage == Decimal("5.00")
    # Gross PnL for SELL: -1 * (190 - 205) * 100 = +1500.00
    # Net PnL: 1500.00 - 2.00 = 1498.00
    assert output.net_profit_loss == Decimal("1498.00")


def test_calculate_settlement_handles_none_entry_target_price() -> None:
    """Verifies that calculate_settlement returns 0.0 slippage when entry_target_price is None (market orders)."""
    settlement_input = SettlementInput(
        entry_executions=[
            ExecutionTuple(quantity=Decimal("10"), price=Decimal("150.00"))
        ],
        exit_executions=[
            ExecutionTuple(quantity=Decimal("10"), price=Decimal("160.00"))
        ],
        entry_target_price=None,
        entry_action="BUY",
        total_commissions=Decimal("1.00"),
    )

    output: SettlementOutput = calculate_settlement(settlement_input)

    assert output.avg_entry_price == Decimal("150.00")
    assert output.avg_exit_price == Decimal("160.00")
    assert output.price_diff_slippage == Decimal("0.0")
    assert output.net_profit_loss == Decimal("99.00")


def test_calculate_settlement_handles_zero_entry_target_price() -> None:
    """Verifies that calculate_settlement returns 0.0 slippage when entry_target_price is 0.0."""
    settlement_input = SettlementInput(
        entry_executions=[
            ExecutionTuple(quantity=Decimal("10"), price=Decimal("150.00"))
        ],
        exit_executions=[
            ExecutionTuple(quantity=Decimal("10"), price=Decimal("160.00"))
        ],
        entry_target_price=Decimal("0.0"),
        entry_action="BUY",
        total_commissions=Decimal("1.00"),
    )

    output: SettlementOutput = calculate_settlement(settlement_input)

    assert output.price_diff_slippage == Decimal("0.0")
