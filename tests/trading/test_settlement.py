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


@pytest.mark.asyncio
async def test_trigger_settlement_aborts_if_already_settled(db) -> None:
    """Verifies trigger_settlement returns early if settlement record already exists."""
    await db.execute(
        """
        INSERT INTO trades_settlement (
            account_id, trade_group_id, avg_entry_price, avg_exit_price,
            price_diff_slippage, total_commissions, net_pnl
        ) VALUES ('A1', 'G_EXISTING', 100.0, 105.0, 0.0, 1.0, 49.0)
        """
    )
    await db.commit()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()

    try:
        await trigger_settlement(db_factory, "G_EXISTING", "A1", mock_notifier)
    finally:
        db.close = original_close

    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_settlement_handles_missing_executions(db) -> None:
    """Verifies trigger_settlement handles missing ENTRY or EXIT executions gracefully."""
    # Insert order without executions
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (10, NULL, 'G_NO_EXEC', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 150.00, 'Filled')
        """
    )
    await db.commit()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()

    try:
        await trigger_settlement(db_factory, "G_NO_EXEC", "A1", mock_notifier)
    finally:
        db.close = original_close

    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_settlement_handles_db_exception() -> None:
    """Verifies trigger_settlement catches and logs database exceptions gracefully."""
    mock_db = AsyncMock()
    mock_db.execute.side_effect = RuntimeError("Database query failed")
    mock_db.close = AsyncMock()

    async def db_factory():
        return mock_db

    mock_notifier = MagicMock()
    # Should catch exception internally and close db
    await trigger_settlement(db_factory, "G_ERR", "A1", mock_notifier)
    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_settlement_with_missing_exits_returns_none(db) -> None:
    """Verifies _fetch_settlement_data returns None when ENTRY exists but no EXIT executions exist."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (30, NULL, 'G_NO_EXIT', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'Filled')
        """
    )
    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E30', 30, 150.0, 10, 1.0)"
    )
    await db.commit()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()

    try:
        await trigger_settlement(db_factory, "G_NO_EXIT", "A1", mock_notifier)
    finally:
        db.close = original_close

    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_settlement_with_null_target_price(db) -> None:
    """Verifies settlement formatting with target_price NULL (Market order) and notification N/A rendering."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (20, NULL, 'G_NULL_TARGET', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'MKT', NULL, 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (21, 20, 'G_NULL_TARGET', 'A1', 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'MKT', NULL, 'Filled')
        """
    )
    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E20', 20, 150.0, 10, 1.0)"
    )
    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E21', 21, 160.0, 10, 1.0)"
    )
    await db.commit()

    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)

    try:
        await trigger_settlement(db_factory, "G_NULL_TARGET", "A1", mock_notifier)
    finally:
        db.close = original_close

    mock_notifier.send_message.assert_called_once()
    msg = mock_notifier.send_message.call_args[0][0]
    assert "Target: N/A" in msg
    assert "Slippage:</b> <code>N/A</code>" in msg
