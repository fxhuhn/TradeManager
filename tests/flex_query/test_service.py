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


@pytest.mark.asyncio
async def test_sync_and_reconcile_flow(
    memory_db: aiosqlite.Connection,
    sample_flex_xml: str,
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    config = FlexQueryConfig(enabled=True)
    mock_notifier = MagicMock()
    mock_notifier.is_active = True
    mock_notifier.send_flex_reconciliation_summary = AsyncMock()

    service = FlexReconciliationService(
        db=memory_db, config=config, notifier=mock_notifier
    )

    with patch(
        "app.services.flex_query.client.FlexWebServiceClient.fetch_statement",
        new_callable=AsyncMock,
    ) as mock_fetch:
        mock_fetch.return_value = sample_flex_xml

        report = await service.sync_and_reconcile(token="TOK123", query_id="QRY456")
        assert report.account_id == "DU123456"
        mock_fetch.assert_awaited_once_with(token="TOK123", query_id="QRY456")
        mock_notifier.send_flex_reconciliation_summary.assert_awaited_once_with(report)


@pytest.mark.asyncio
async def test_sync_and_reconcile_handles_notifier_error(
    memory_db: aiosqlite.Connection,
    sample_flex_xml: str,
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    config = FlexQueryConfig(enabled=True)
    mock_notifier = MagicMock()
    mock_notifier.is_active = True
    mock_notifier.send_flex_reconciliation_summary = AsyncMock(
        side_effect=RuntimeError("Telegram network down")
    )

    service = FlexReconciliationService(
        db=memory_db, config=config, notifier=mock_notifier
    )

    with patch(
        "app.services.flex_query.client.FlexWebServiceClient.fetch_statement",
        new_callable=AsyncMock,
    ) as mock_fetch:
        mock_fetch.return_value = sample_flex_xml

        # Should not raise exception, logs error instead
        report = await service.sync_and_reconcile()
        assert report.account_id == "DU123456"
        mock_notifier.send_flex_reconciliation_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_from_xml_reconciles_missing_trade_and_settles(
    memory_db: aiosqlite.Connection,
) -> None:
    """Verifiziert die automatische Erkennung und Abrechnung eines im Flex-Statement enthaltenen Trades."""
    # 1. Entry-Order und Ausführung für MNQU6 anlegen
    await memory_db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id,
            bracket_role, symbol, sec_type, exchange, action, quantity,
            order_type, target_price, tif, strategy_name, status, transmitted_at
        ) VALUES (
            200, 2000, NULL, '1570_TwoPercent_QQQ', 'U12345',
            'ENTRY', 'MNQU6', 'FUT', 'CME', 'BUY', 1,
            'LMT', 20000.0, 'DAY', 'TwoPercent', 'Filled', '2026-09-28 15:30:00'
        )
        """
    )
    await memory_db.execute(
        """
        INSERT INTO executions (
            exec_id, order_id, price, qty, commission, currency, executed_at
        ) VALUES (
            'EXEC_ENTRY_1', 200, 20000.0, 1.0, 0.85, 'USD', '2026-09-28 15:30:00'
        )
        """
    )
    # Aktive SL-Order, die storniert werden muss
    await memory_db.execute(
        """
        INSERT INTO orders (
            order_id, perm_id, parent_id, trade_group_id, account_id,
            bracket_role, symbol, sec_type, exchange, action, quantity,
            order_type, target_price, tif, strategy_name, status, transmitted_at
        ) VALUES (
            201, 2001, 200, '1570_TwoPercent_QQQ', 'U12345',
            'SL', 'MNQU6', 'FUT', 'CME', 'SELL', 1,
            'STP', 19800.0, 'GTC', 'TwoPercent', 'Submitted', '2026-09-28 15:30:00'
        )
        """
    )
    await memory_db.commit()

    config = FlexQueryConfig(enabled=True)
    service = FlexReconciliationService(db=memory_db, config=config)

    xml_with_trades = """<FlexQueryResponse queryName="Trades Sample" type="AF">
    <FlexStatements count="1">
    <FlexStatement accountId="U12345" fromDate="20260928" toDate="20260928">
    <Trades>
    <Trade accountId="U12345" currency="USD" assetCategory="FUT" symbol="MNQ" dateTime="20260928;215900"
           tradePrice="20050.0" quantity="-1" proceeds="20050.0" ibCommission="-0.85" buySell="SELL"
           ibOrderID="205" ibExecutionID="EXEC_LIQ_1" notes="L" exchange="GLOBEX" />
    </Trades>
    </FlexStatement>
    </FlexStatements>
    </FlexQueryResponse>"""

    # 1. Erster Durchlauf: Trade muss erkannt, verbucht und abgerechnet werden
    report = await service.reconcile_from_xml(xml_with_trades)
    assert report.reconciled_trades_count == 1
    assert report.settled_trades_count == 1

    # Verifikation: SL-Order 201 wurde storniert
    async with memory_db.execute(
        "SELECT status FROM orders WHERE order_id = 201"
    ) as cursor:
        sl_row = await cursor.fetchone()
        assert sl_row["status"] == "Cancelled"

    # Verifikation: Synthetischer Exit und Execution angelegt
    async with memory_db.execute(
        "SELECT order_id, status, bracket_role FROM orders WHERE trade_group_id = '1570_TwoPercent_QQQ' AND bracket_role = 'EXIT'"
    ) as cursor:
        exit_row = await cursor.fetchone()
        assert exit_row is not None
        assert exit_row["status"] == "Filled"

    async with memory_db.execute(
        "SELECT price, qty, commission FROM executions WHERE exec_id = 'EXEC_LIQ_1'"
    ) as cursor:
        exec_row = await cursor.fetchone()
        assert exec_row is not None
        assert Decimal(str(exec_row["price"])) == Decimal("20050.0")

    # Verifikation: Settlement gebucht mit CME Multiplikator 2.0
    async with memory_db.execute(
        "SELECT avg_entry_price, avg_exit_price, net_pnl FROM trades_settlement WHERE trade_group_id = '1570_TwoPercent_QQQ'"
    ) as cursor:
        settlement_row = await cursor.fetchone()
        assert settlement_row is not None
        assert Decimal(str(settlement_row["avg_entry_price"])) == Decimal("20000.0")
        assert Decimal(str(settlement_row["avg_exit_price"])) == Decimal("20050.0")
        # (20050 - 20000) * 1 - (0.85 + 0.85) = 50 - 1.70 = 48.30
        assert Decimal(str(settlement_row["net_pnl"])) == Decimal("48.30")

    # 2. Zweiter Durchlauf: Idempotenztest (keine Doppelbuchung)
    report2 = await service.reconcile_from_xml(xml_with_trades)
    assert report2.reconciled_trades_count == 0
    assert report2.settled_trades_count == 0
