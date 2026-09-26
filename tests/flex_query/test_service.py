"""
Unit- und Integrationstests für den FlexReconciliationService.
"""

from decimal import Decimal

import aiosqlite
import pytest

from app.core.config import FlexQueryConfig
from app.core.db import run_migrations
from app.services.flex_query.service import FlexReconciliationService


@pytest.fixture
async def memory_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await run_migrations(db)
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_from_xml_inserts_and_is_idempotent(
    memory_db: aiosqlite.Connection,
    sample_flex_xml: str,
) -> None:
    # Vorbereitung: Historischen Trade für SAIC und MAA anlegen
    await memory_db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id,
            bracket_role, symbol, sec_type, exchange, action, quantity,
            order_type, target_price, tif, strategy_name, status, transmitted_at
        ) VALUES (
            101, 1001, NULL, 'TG_SAIC_SHORT', 'DU123456',
            'ENTRY', 'SAIC', 'STK', 'SMART', 'SELL', 100,
            'LMT', 140.00, 'GTC', 'BounceBandit', 'Filled', '2026-05-20 15:30:00'
        )
        """
    )
    await memory_db.execute(
        """
        INSERT INTO trades_settlement (
            account_id, trade_group_id, avg_entry_price, avg_exit_price,
            price_diff_slippage, total_commissions, net_pnl, settled_at
        ) VALUES (
            'DU123456', 'TG_SAIC_SHORT', 140.00, 135.00,
            0.00, 1.50, 498.50, '2026-05-30 21:00:00'
        )
        """
    )
    await memory_db.commit()

    config = FlexQueryConfig(enabled=True)
    service = FlexReconciliationService(db=memory_db, config=config)

    xml_content = sample_flex_xml

    # 1. Erster Durchlauf: Daten müssen verbucht werden
    report1 = await service.reconcile_from_xml(xml_content)
    assert report1.account_id == "DU123456"
    assert report1.inserted_count > 0
    assert report1.skipped_duplicate_count == 0

    # Prüfen, ob SAIC Borrow Fee dem Trade TG_SAIC_SHORT zugeordnet wurde
    async with memory_db.execute(
        "SELECT trade_group_id, amount, category FROM cash_ledger WHERE symbol = 'SAIC'"
    ) as cursor:
        rows = await cursor.fetchall()
        assert len(rows) > 0
        for row in rows:
            assert row["trade_group_id"] == "TG_SAIC_SHORT"
            assert row["category"] == "BORROW_FEE"

    # Prüfen der View v_trade_settlement_all_in
    async with memory_db.execute(
        "SELECT * FROM v_trade_settlement_all_in WHERE trade_group_id = 'TG_SAIC_SHORT'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["borrow_fees"] < 0  # Negative Gebühr
        assert row["all_in_net_pnl"] < Decimal(
            "498.50"
        )  # PnL geschmälert um Borrow Fees

    # 2. Zweiter Durchlauf mit demselben XML: Idempotenztest (alles Duplikate)
    report2 = await service.reconcile_from_xml(xml_content)
    assert report2.inserted_count == 0
    assert report2.skipped_duplicate_count == report1.inserted_count


@pytest.mark.asyncio
async def test_fetch_historical_trades_empty_db(
    memory_db: aiosqlite.Connection,
) -> None:
    config = FlexQueryConfig(enabled=True)
    service = FlexReconciliationService(db=memory_db, config=config)
    trades = await service.fetch_historical_trades()
    assert len(trades) == 0
