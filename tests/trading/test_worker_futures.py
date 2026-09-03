"""Integrationstest für den Execution Worker mit BounceBandit Future-Orders."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_async import OrderStatus, Trade

from app.core.config import load_config
from app.trading.worker import process_trade_group


@pytest.mark.asyncio
async def test_worker_process_bounce_bandit_futures_bracket(db) -> None:
    """Verifiziert, dass der Worker BounceBandit Future-Orders als atomaren Bracket übermittelt."""
    # 1. Orders in DB anlegen
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status, strategy_name)
        VALUES
            (5001, 'TG_BB_TEST', 'U19605236', 'ENTRY', 'MNQU6', 'FUT', 'CME', 'BUY', 1, 'MKT', NULL, 'Created', 'BounceBandit'),
            (5002, 'TG_BB_TEST', 'U19605236', 'TP', 'MNQU6', 'FUT', 'CME', 'SELL', 1, 'LOC', 715.50, 'Created', 'BounceBandit')
        """
    )
    await db.commit()

    config = load_config(Path("."))
    mock_notifier = MagicMock()
    mock_notifier.send_bracket_order_submitted = AsyncMock()
    mock_notifier.send_order_failed = AsyncMock()

    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    req_id_counter = 7000

    def mock_get_req_id() -> int:
        nonlocal req_id_counter
        req_id_counter += 1
        return req_id_counter

    mock_ib.client.getReqId.side_effect = mock_get_req_id

    # Account values für Cushion-Check
    cushion_val = MagicMock()
    cushion_val.tag = "Cushion"
    cushion_val.value = "0.75"
    mock_ib.accountValues.return_value = [cushion_val]

    # Pre-Trade Simulation What-If
    mock_order_state = MagicMock()
    mock_order_state.initMarginAfter = "25000.0"
    mock_order_state.equityWithLoanAfter = "75000.0"
    mock_ib.whatIfOrderAsync = AsyncMock(return_value=mock_order_state)

    # Place order trades
    placed_trades: list[tuple] = []

    def mock_place_order(contract, order):
        trade = MagicMock(spec=Trade)
        trade.order = order
        trade.contract = contract
        trade.orderStatus = MagicMock(spec=OrderStatus)
        # Entry becomes PreSubmitted, Child becomes Inactive (waiting for parent)
        trade.orderStatus.status = (
            "PreSubmitted" if order.action == "BUY" else "Inactive"
        )
        trade.log = []
        placed_trades.append((contract, order, trade))
        return trade

    mock_ib.placeOrder.side_effect = mock_place_order

    # Ausführung des Workers
    await process_trade_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="TG_BB_TEST",
        notifier=mock_notifier,
        config=config,
    )

    # 2. Prüfe, dass 2 Orders platziert wurden
    assert len(placed_trades) == 2
    entry_contract, entry_order, entry_trade = placed_trades[0]
    tp_contract, tp_order, tp_trade = placed_trades[1]

    # Prüfe Entry
    assert entry_contract.secType == "FUT"
    assert entry_contract.localSymbol == "MNQU6"
    assert entry_order.action == "BUY"
    assert entry_order.orderType == "MKT"
    assert entry_order.transmit is False  # Bracket parent transmit=False
    assert "08:30:00 US/Central" in entry_order.goodAfterTime

    # Prüfe Child TP
    assert tp_contract.secType == "FUT"
    assert tp_contract.localSymbol == "MNQU6"
    assert tp_order.action == "SELL"
    assert tp_order.orderType == "MKT"
    assert tp_order.parentId == entry_order.orderId  # Verknüpft via parentId
    assert tp_order.transmit is True  # Bracket child transmit=True
    assert "14:59:00 US/Central" in tp_order.goodAfterTime
    assert len(tp_order.conditions) == 2

    # Prüfe Notifier
    mock_notifier.send_bracket_order_submitted.assert_called_once()


@pytest.mark.asyncio
async def test_worker_futures_fail_closed_on_cushion_violation(db) -> None:
    """Verifiziert, dass bei zu geringem Cushion (<5%) die Order sofort abbricht (Fail-Closed)."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status, strategy_name)
        VALUES
            (5011, 'TG_BB_MARGIN_FAIL', 'U19605236', 'ENTRY', 'MNQU6', 'FUT', 'CME', 'BUY', 1, 'MKT', NULL, 'Created', 'BounceBandit'),
            (5012, 'TG_BB_MARGIN_FAIL', 'U19605236', 'TP', 'MNQU6', 'FUT', 'CME', 'SELL', 1, 'LOC', 715.50, 'Created', 'BounceBandit')
        """
    )
    await db.commit()

    config = load_config(Path("."))
    mock_notifier = MagicMock()
    mock_notifier.send_order_failed = AsyncMock()

    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True

    # Cushion < 5% (z.B. 0.02)
    cushion_val = MagicMock()
    cushion_val.tag = "Cushion"
    cushion_val.value = "0.02"
    mock_ib.accountValues.return_value = [cushion_val]

    await process_trade_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="TG_BB_MARGIN_FAIL",
        notifier=mock_notifier,
        config=config,
    )

    # Verifiziere, dass keine Orders gesendet wurden
    mock_ib.placeOrder.assert_not_called()

    # Verifiziere Status in DB = Error
    async with db.execute(
        "SELECT status FROM orders WHERE trade_group_id = 'TG_BB_MARGIN_FAIL'"
    ) as cursor:
        statuses = [row["status"] for row in await cursor.fetchall()]
        assert all(s == "Error" for s in statuses)

    # Verifiziere Notifier-Alert
    mock_notifier.send_order_failed.assert_called_once()
