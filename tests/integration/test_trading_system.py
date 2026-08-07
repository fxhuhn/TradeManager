# filename: tests/integration/test_trading_system.py
"""System-level integration tests for database constraints, status protection, and trading operations."""

import pytest


@pytest.mark.asyncio
async def test_upsert_idempotency(db) -> None:
    """UPSERT-Idempotenz: Doppeltes Importieren aktualisiert nur Preis/Menge und erzeugt keine Duplikate."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 180.0, 'Created')
        """
    )
    await db.commit()

    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 200, 'LMT', 185.0, 'Created')
        ON CONFLICT(account_id, trade_group_id, bracket_role, order_type) DO UPDATE SET
            quantity = excluded.quantity,
            target_price = excluded.target_price
        """
    )
    await db.commit()

    async with db.execute(
        "SELECT quantity, target_price FROM orders WHERE trade_group_id = 'G1'"
    ) as cursor:
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["quantity"] == 200
        assert rows[0]["target_price"] == 185.0


@pytest.mark.asyncio
async def test_upsert_protects_submitted(db) -> None:
    """UPSERT schützt Submitted: Aktive Orders werden nicht überschrieben."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 180.0, 'Submitted')
        """
    )
    await db.commit()

    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 200, 'LMT', 185.0, 'Created')
        ON CONFLICT(account_id, trade_group_id, bracket_role, order_type) DO UPDATE SET
            quantity = excluded.quantity,
            target_price = excluded.target_price
        WHERE status IN ('Created', 'Error')
        """
    )
    await db.commit()

    async with db.execute(
        "SELECT quantity, target_price FROM orders WHERE trade_group_id = 'G1'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row["quantity"] == 100
        assert row["target_price"] == 180.0
