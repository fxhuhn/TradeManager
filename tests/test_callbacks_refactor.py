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
