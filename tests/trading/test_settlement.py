# filename: tests/trading/test_settlement.py
"""Unit tests for settlement calculations, VWAP weighting, edge cases, and database triggers."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.trading.settlement import (
    ExecutionTuple,
    SettlementInput,
    SettlementOutput,
    calculate_settlement,
    trigger_settlement,
)


def test_calculate_settlement_pure() -> None:
    """Pure mathematical test of PnL, VWAP, and slippage calculations."""
    settlement_input = SettlementInput(
        entry_executions=[
            ExecutionTuple(quantity=Decimal("60"), price=Decimal("150.10")),
            ExecutionTuple(quantity=Decimal("40"), price=Decimal("149.80")),
        ],
        exit_executions=[
            ExecutionTuple(quantity=Decimal("100"), price=Decimal("155.05"))
        ],
        entry_target_price=Decimal("150.00"),
        entry_action="BUY",
        total_commissions=Decimal("4.00"),
    )
    result = calculate_settlement(settlement_input)

    assert result.avg_entry_price == Decimal("149.98")
    assert result.avg_exit_price == Decimal("155.05")
    assert result.price_diff_slippage == Decimal("0.02")
    assert settlement_input.total_commissions == Decimal("4.00")
    assert result.net_profit_loss == Decimal("503.00")


@pytest.mark.asyncio
async def test_settlement_vwap_calculation(db) -> None:
    """Settlement VWAP: Korrekte mengengewichtete PnL-Berechnung bei Partial Fills."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 150.00, 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (2, 1, 'G1', 'A1', 'TP', 'AAPL', 'STK', 'SMART', 'SELL', 100, 'LMT', 155.00, 'Filled')
        """
    )
    await db.commit()

    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E1', 1, 150.10, 60, 1.0)"
    )
    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E2', 1, 149.80, 40, 1.0)"
    )
    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E3', 2, 155.05, 100, 2.0)"
    )
    await db.commit()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)

    try:
        await trigger_settlement(db_factory, "G1", "A1", mock_notifier)
    finally:
        db.close = original_close

    async with db.execute(
        "SELECT * FROM trades_settlement WHERE trade_group_id = 'G1'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row["avg_entry_price"] - 149.98) < 0.001
        assert abs(row["avg_exit_price"] - 155.05) < 0.001
        assert abs(row["price_diff_slippage"] - 0.02) < 0.001
        assert row["total_commissions"] == 4.0
        assert abs(row["net_pnl"] - 503.0) < 0.01


def test_calculate_settlement_handles_zero_entry_quantity_gracefully() -> None:
    """Verifies that calculate_settlement returns 0.0 VWAP when entry executions sum to 0 quantity."""
    settlement_input = SettlementInput(
        entry_executions=[],
        exit_executions=[
            ExecutionTuple(quantity=Decimal("100"), price=Decimal("150.00"))
        ],
        entry_target_price=Decimal("140.00"),
        entry_action="BUY",
        total_commissions=Decimal("1.00"),
    )

    output: SettlementOutput = calculate_settlement(settlement_input)

    assert output.avg_entry_price == Decimal("0.0")
    assert output.avg_exit_price == Decimal("150.00")
    assert output.net_profit_loss == Decimal("-1.00")


def test_calculate_settlement_handles_zero_exit_quantity_gracefully() -> None:
    """Verifies that calculate_settlement returns 0.0 exit VWAP when exit executions sum to 0 quantity."""
    settlement_input = SettlementInput(
        entry_executions=[
            ExecutionTuple(quantity=Decimal("100"), price=Decimal("140.00"))
        ],
        exit_executions=[],
        entry_target_price=Decimal("140.00"),
        entry_action="BUY",
        total_commissions=Decimal("1.50"),
    )

    output: SettlementOutput = calculate_settlement(settlement_input)

    assert output.avg_entry_price == Decimal("140.00")
    assert output.avg_exit_price == Decimal("0.0")
    assert output.net_profit_loss == Decimal("-14001.50")


def test_calculate_settlement_handles_short_sell_position_profit() -> None:
    """Verifies that SELL (short entry) position PnL is positive when exit price is lower than entry price."""
    settlement_input = SettlementInput(
        entry_executions=[
            ExecutionTuple(quantity=Decimal("50"), price=Decimal("200.00")),
            ExecutionTuple(quantity=Decimal("50"), price=Decimal("210.00")),
        ],
        exit_executions=[
            ExecutionTuple(quantity=Decimal("100"), price=Decimal("190.00"))
        ],
        entry_target_price=Decimal("200.00"),
        entry_action="SELL",
        total_commissions=Decimal("2.00"),
    )

    output: SettlementOutput = calculate_settlement(settlement_input)

    assert output.avg_entry_price == Decimal("205.00")
    assert output.avg_exit_price == Decimal("190.00")
    assert output.price_diff_slippage == Decimal("5.00")
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
