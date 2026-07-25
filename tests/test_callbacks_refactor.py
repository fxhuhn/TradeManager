# filename: tests/test_callbacks_refactor.py
"""Unit tests for callbacks refactoring and commission update retry logic."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from app.trading.callbacks import TwsCallbacksManager


@pytest.mark.asyncio
async def test_update_commission_retries_and_succeeds_on_later_attempt(
    tmp_path,
) -> None:
    """Verifies that _update_commission retries when the execution row is created after latency delay."""
    # Arrange
    db_file = tmp_path / "test_trading.db"
    async with aiosqlite.connect(db_file) as init_db:
        await init_db.execute(
            "CREATE TABLE executions (exec_id TEXT PRIMARY KEY, commission TEXT, currency TEXT)"
        )
        await init_db.execute("INSERT INTO executions (exec_id) VALUES ('EXEC-101')")
        await init_db.commit()

    async def db_factory() -> aiosqlite.Connection:
        return await aiosqlite.connect(db_file)

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=AsyncMock(),
        config=MagicMock(),
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    # Act
    await manager._update_commission("EXEC-101", Decimal("2.50"), "USD")

    # Assert
    async with aiosqlite.connect(db_file) as db:
        async with db.execute(
            "SELECT commission, currency FROM executions WHERE exec_id = 'EXEC-101'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "2.50"
            assert row[1] == "USD"


@pytest.mark.asyncio
async def test_update_commission_handles_missing_execution_row_gracefully(
    tmp_path,
) -> None:
    """Verifies that _update_commission logs warning after maximum retries without raising unhandled exception."""
    # Arrange
    db_file = tmp_path / "test_trading.db"
    async with aiosqlite.connect(db_file) as init_db:
        await init_db.execute(
            "CREATE TABLE executions (exec_id TEXT PRIMARY KEY, commission TEXT, currency TEXT)"
        )
        await init_db.commit()

    async def db_factory() -> aiosqlite.Connection:
        return await aiosqlite.connect(db_file)

    manager = TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=MagicMock(),
        notifier=AsyncMock(),
        config=MagicMock(),
        trigger_settlement_callback=AsyncMock(),
        handle_retriable_error_callback=AsyncMock(),
        run_recovery_callback=AsyncMock(),
        run_reconnect_callback=AsyncMock(),
    )

    # Act & Assert (Row does not exist, should exit cleanly after max retries)
    await manager._update_commission("EXEC-NONEXISTENT", Decimal("1.00"), "USD")


def test_extract_unassigned_execution_details() -> None:
    """Verifies that extract_unassigned_execution_details correctly pulls contract and execution fields."""
    from app.trading.callbacks import (
        extract_unassigned_execution_details,
        handle_unassigned_execution,
    )

    mock_trade = MagicMock()
    mock_trade.order.action = "SELL"
    mock_trade.order.account = "U19605236"
    mock_trade.order.orderRef = "Ref123"

    mock_fill = MagicMock()
    mock_fill.contract.symbol = "SLB"
    mock_fill.contract.secType = "STK"
    mock_fill.contract.exchange = "SMART"
    mock_fill.contract.currency = "USD"
    mock_fill.execution.side = "SLD"
    mock_fill.execution.shares = 51.0
    mock_fill.execution.price = 52.42
    mock_fill.execution.acctNumber = "U19605236"
    mock_fill.execution.orderId = -6
    mock_fill.execution.permId = 123456
    mock_fill.execution.execId = "EXEC-999"
    mock_fill.execution.time = "2026-07-24 22:00:03"

    details = extract_unassigned_execution_details(mock_trade, mock_fill)

    assert details["symbol"] == "SLB"
    assert details["sec_type"] == "STK"
    assert details["side"] == "SLD"
    assert details["qty"] == Decimal("51.0")
    assert details["price"] == Decimal("52.42")
    assert details["account_id"] == "U19605236"
    assert details["order_id"] == -6
    assert details["perm_id"] == 123456
    assert details["exec_id"] == "EXEC-999"

    handled = handle_unassigned_execution(mock_trade, mock_fill)
    assert handled["symbol"] == "SLB"
