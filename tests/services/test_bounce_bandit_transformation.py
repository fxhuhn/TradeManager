"""Integrationstests für die BounceBandit QQQ -> MNQ Future Transformation im Importer."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_async import Future

from app.core.models import LegRow
from app.services.importer import _process_and_upsert_group


@pytest.mark.asyncio
async def test_bounce_bandit_qqq_transformation(db, test_config, monkeypatch) -> None:
    """Prüft, dass eine BounceBandit QQQ-Order in 1 MNQ-Kontrakt transformiert und in der DB gesichert wird."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True

    mock_future = Future(
        conId=793356225,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20260918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU6",
    )

    # Mock resolve_active_future_contract
    mock_resolve = AsyncMock(return_value=mock_future)
    monkeypatch.setattr(
        "app.services.importer.resolve_active_future_contract", mock_resolve
    )

    # Mock Notifier
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()

    # Raw legs wie aus der CSV
    raw_legs = [
        LegRow(
            trade_group_id="TG_BOUNCE_1",
            bracket_role="ENTRY",
            symbol="QQQ",
            sec_type="STK",
            exchange="SMART",
            account_id="U19605236",
            action="BUY",
            quantity=150,  # Soll ignoriert werden
            order_type="MKT",
            target_price=Decimal("700.00"),
            tif="OPG",
            strategy_name="BounceBandit",
        ),
        LegRow(
            trade_group_id="TG_BOUNCE_1",
            bracket_role="TP",
            symbol="QQQ",
            sec_type="STK",
            exchange="SMART",
            account_id="U19605236",
            action="SELL",
            quantity=150,  # Soll ignoriert werden
            order_type="LOC",
            target_price=Decimal("715.50"),
            tif="DAY",
            strategy_name="BounceBandit",
        ),
    ]

    queue: asyncio.Queue[str] = asyncio.Queue()
    config = test_config

    await _process_and_upsert_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="TG_BOUNCE_1",
        raw_legs=raw_legs,
        queue=queue,
        notifier=mock_notifier,
        config=config,
    )

    # Verifiziere DB-Einträge
    async with db.execute(
        "SELECT bracket_role, symbol, sec_type, exchange, quantity, target_price FROM orders WHERE trade_group_id = 'TG_BOUNCE_1' ORDER BY bracket_role"
    ) as cursor:
        rows = await cursor.fetchall()
        assert len(rows) == 2

        entry_row = next(r for r in rows if r["bracket_role"] == "ENTRY")
        assert entry_row["symbol"] == "MNQU6"
        assert entry_row["sec_type"] == "FUT"
        assert entry_row["exchange"] == "CME"
        assert entry_row["quantity"] == 1

        tp_row = next(r for r in rows if r["bracket_role"] == "TP")
        assert tp_row["symbol"] == "MNQU6"
        assert tp_row["sec_type"] == "FUT"
        assert tp_row["exchange"] == "CME"
        assert tp_row["quantity"] == 1
        assert Decimal(str(tp_row["target_price"])) == Decimal("715.50")

    # Verifiziere Queue-Push
    assert queue.qsize() == 1
    assert await queue.get() == "TG_BOUNCE_1"


@pytest.mark.asyncio
async def test_generic_future_transformation_spy_to_mes(
    db, test_config, monkeypatch
) -> None:
    """Prüft, dass eine aktivierte Strategie SpxTrend mit SPY in MES-Futures transformiert wird."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True

    mock_future = Future(
        conId=888888,
        symbol="MES",
        lastTradeDateOrContractMonth="20260918",
        exchange="CME",
        currency="USD",
        localSymbol="MESU6",
    )

    mock_resolve = AsyncMock(return_value=mock_future)
    monkeypatch.setattr(
        "app.services.importer.resolve_active_future_contract", mock_resolve
    )

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()

    raw_legs = [
        LegRow(
            trade_group_id="TG_SPX_1",
            bracket_role="ENTRY",
            symbol="SPY",
            sec_type="STK",
            exchange="SMART",
            account_id="U19605236",
            action="BUY",
            quantity=50,
            order_type="LMT",
            target_price=Decimal("550.00"),
            tif="GTC",
            strategy_name="SpxTrend",
        ),
    ]

    queue: asyncio.Queue[str] = asyncio.Queue()
    await _process_and_upsert_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="TG_SPX_1",
        raw_legs=raw_legs,
        queue=queue,
        notifier=mock_notifier,
        config=test_config,
    )

    async with db.execute(
        "SELECT symbol, sec_type, exchange, quantity, target_price FROM orders WHERE trade_group_id = 'TG_SPX_1'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["symbol"] == "MESU6"
        assert row["sec_type"] == "FUT"
        assert row["exchange"] == "CME"
        assert row["quantity"] == 1
        assert Decimal(str(row["target_price"])) == Decimal("550.00")

    mock_resolve.assert_called_once_with(mock_ib, symbol="MES", exchange="CME")


@pytest.mark.asyncio
async def test_strategy_not_enabled_remains_equity(
    db, test_config, monkeypatch
) -> None:
    """Prüft, dass eine nicht aktivierte Strategie trotz vorhandenem Symbol-Mapping als Aktie importiert wird."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True

    mock_resolve = AsyncMock()
    monkeypatch.setattr(
        "app.services.importer.resolve_active_future_contract", mock_resolve
    )

    # Mock fetch_account_balance_metrics for equity sizing
    from app.services.importer import AccountBalanceMetrics

    monkeypatch.setattr(
        "app.services.importer.fetch_account_balance_metrics",
        AsyncMock(
            return_value=AccountBalanceMetrics(
                net_liquidation_value=Decimal("100000.0"),
                available_funds_value=Decimal("50000.0"),
                total_cash_value=Decimal("50000.0"),
            )
        ),
    )

    raw_legs = [
        LegRow(
            trade_group_id="TG_NOT_ENABLED_1",
            bracket_role="ENTRY",
            symbol="QQQ",
            sec_type="STK",
            exchange="SMART",
            account_id="U19605236",
            action="BUY",
            quantity=10,
            order_type="LMT",
            target_price=Decimal("450.00"),
            tif="DAY",
            strategy_name="UnlistedStrategy",
        ),
    ]

    queue: asyncio.Queue[str] = asyncio.Queue()
    await _process_and_upsert_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="TG_NOT_ENABLED_1",
        raw_legs=raw_legs,
        queue=queue,
        notifier=MagicMock(),
        config=test_config,
    )

    async with db.execute(
        "SELECT symbol, sec_type, exchange, quantity FROM orders WHERE trade_group_id = 'TG_NOT_ENABLED_1'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["symbol"] == "QQQ"
        assert row["sec_type"] == "STK"
        assert row["exchange"] == "SMART"
        assert row["quantity"] == 10

    mock_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_unmapped_symbol_remains_equity(db, test_config, monkeypatch) -> None:
    """Prüft, dass ein nicht-gemapptes Symbol bei aktivierter Strategie regulär als Aktie importiert wird."""
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True

    mock_resolve = AsyncMock()
    monkeypatch.setattr(
        "app.services.importer.resolve_active_future_contract", mock_resolve
    )

    from app.services.importer import AccountBalanceMetrics

    monkeypatch.setattr(
        "app.services.importer.fetch_account_balance_metrics",
        AsyncMock(
            return_value=AccountBalanceMetrics(
                net_liquidation_value=Decimal("100000.0"),
                available_funds_value=Decimal("50000.0"),
                total_cash_value=Decimal("50000.0"),
            )
        ),
    )

    raw_legs = [
        LegRow(
            trade_group_id="TG_UNMAPPED_1",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="U19605236",
            action="BUY",
            quantity=25,
            order_type="LMT",
            target_price=Decimal("220.00"),
            tif="DAY",
            strategy_name="BounceBandit",
        ),
    ]

    queue: asyncio.Queue[str] = asyncio.Queue()
    await _process_and_upsert_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="TG_UNMAPPED_1",
        raw_legs=raw_legs,
        queue=queue,
        notifier=MagicMock(),
        config=test_config,
    )

    async with db.execute(
        "SELECT symbol, sec_type, exchange, quantity FROM orders WHERE trade_group_id = 'TG_UNMAPPED_1'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["symbol"] == "AAPL"
        assert row["sec_type"] == "STK"
        assert row["exchange"] == "SMART"
        assert row["quantity"] == 25

    mock_resolve.assert_not_called()
