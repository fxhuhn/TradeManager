# filename: tests/test_worker_refactor.py
"""Unit tests for worker refactoring and order ID generation invariants."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from app.core.models import OrderRow
from app.trading.worker import _check_cushion_limit, _get_next_non_colliding_order_id


@pytest.mark.asyncio
async def test_get_next_non_colliding_order_id_uses_db_max_plus_one_when_higher() -> (
    None
):
    """Verifies that DB MAX(order_id) + 1 is returned if DB max is greater than or equal to TWS reqId."""
    # Arrange
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, status TEXT)"
        )
        await db.execute(
            "INSERT INTO orders (order_id, status) VALUES (5000, 'Created')"
        )
        await db.commit()

        ib_mock = MagicMock()
        ib_mock.client.getReqId.return_value = 1200  # TWS has lower ID sequence

        # Act
        next_id = await _get_next_non_colliding_order_id(db, ib_mock)

        # Assert
        assert next_id == 5001


@pytest.mark.asyncio
async def test_get_next_non_colliding_order_id_uses_tws_id_when_higher() -> None:
    """Verifies that TWS getReqId() is returned when it is strictly greater than DB MAX(order_id)."""
    # Arrange
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, status TEXT)"
        )
        await db.execute(
            "INSERT INTO orders (order_id, status) VALUES (1000, 'Created')"
        )
        await db.commit()

        ib_mock = MagicMock()
        ib_mock.client.getReqId.return_value = 2500  # TWS has higher ID sequence

        # Act
        next_id = await _get_next_non_colliding_order_id(db, ib_mock)

        # Assert
        assert next_id == 2500


@pytest.mark.asyncio
async def test_check_cushion_limit_blocks_order_when_cushion_below_threshold() -> None:
    """Verifies that _check_cushion_limit sets order status to Error when cushion is below minimum."""
    # Arrange
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, status TEXT)"
        )
        await db.execute("INSERT INTO orders (order_id, status) VALUES (1, 'Created')")
        await db.commit()

        account_val = MagicMock()
        account_val.tag = "Cushion"
        account_val.account = "U123456"
        account_val.value = "0.02"  # 2% cushion

        ib_mock = MagicMock()
        ib_mock.accountValues.return_value = [account_val]

        entry_order = OrderRow(
            order_id=1,
            perm_id=None,
            parent_id=None,
            trade_group_id="TG-101",
            account_id="U123456",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            action="BUY",
            quantity=10,
            order_type="LMT",
            target_price=Decimal("150.00"),
            tif="GTC",
            strategy_name="DipBuyer",
            status="Created",
        )

        config_mock = MagicMock()
        config_mock.account.min_cushion_pct = 0.05  # Requires 5% minimum cushion

        notifier_mock = AsyncMock()

        # Act
        passed, updated_order, cushion_pct = await _check_cushion_limit(
            db, ib_mock, entry_order, config_mock, notifier_mock
        )

        # Assert
        assert passed is False
        assert updated_order.status == "Error"
        assert cushion_pct == Decimal("2.0")
        notifier_mock.send_margin_limit_exceeded.assert_called_once()
