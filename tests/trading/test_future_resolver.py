"""Unit-Tests für die dynamische Future-Kontraktauflösung (future_resolver.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_async import ContractDetails, Future, Ticker

from app.trading.future_resolver import resolve_active_future_contract


@pytest.mark.asyncio
async def test_resolve_active_future_contract_highest_volume() -> None:
    """Prüft, ob der Kontrakt mit dem höheren Handelsvolumen ausgewählt wird."""
    mock_ib = MagicMock()

    contract_u6 = Future(
        conId=1001,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20260918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU6",
    )
    contract_z6 = Future(
        conId=1002,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20261218",
        exchange="CME",
        currency="USD",
        localSymbol="MNQZ6",
    )

    cd1 = ContractDetails(contract=contract_u6)
    cd2 = ContractDetails(contract=contract_z6)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd1, cd2])

    ticker_u6 = MagicMock(spec=Ticker)
    ticker_u6.contract = contract_u6
    ticker_u6.volume = 500000.0

    ticker_z6 = MagicMock(spec=Ticker)
    ticker_z6.contract = contract_z6
    ticker_z6.volume = 12000.0

    mock_ib.reqTickersAsync = AsyncMock(return_value=[ticker_u6, ticker_z6])

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME"
    )
    assert selected.localSymbol == "MNQU6"
    assert selected.conId == 1001
    mock_ib.reqMarketDataType.assert_called_once_with(3)


@pytest.mark.asyncio
async def test_resolve_active_future_contract_roll_to_next_month() -> None:
    """Prüft, ob nach dem Roll der Folgemonat gewählt wird, wenn dieser mehr Volumen hat."""
    mock_ib = MagicMock()

    contract_u6 = Future(
        conId=1001,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20260918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU6",
    )
    contract_z6 = Future(
        conId=1002,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20261218",
        exchange="CME",
        currency="USD",
        localSymbol="MNQZ6",
    )

    cd1 = ContractDetails(contract=contract_u6)
    cd2 = ContractDetails(contract=contract_z6)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd1, cd2])

    ticker_u6 = MagicMock(spec=Ticker)
    ticker_u6.contract = contract_u6
    ticker_u6.volume = 2000.0

    ticker_z6 = MagicMock(spec=Ticker)
    ticker_z6.contract = contract_z6
    ticker_z6.volume = 450000.0  # Folgemonat hat nach dem Roll das Hauptvolumen

    mock_ib.reqTickersAsync = AsyncMock(return_value=[ticker_u6, ticker_z6])

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME"
    )
    assert selected.localSymbol == "MNQZ6"
    assert selected.conId == 1002


@pytest.mark.asyncio
async def test_resolve_active_future_contract_single_candidate() -> None:
    """Prüft, dass bei nur einem aktiven Kontrakt dieser sofort gewählt wird."""
    mock_ib = MagicMock()

    contract_u6 = Future(
        conId=1001,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20260918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU6",
    )
    cd1 = ContractDetails(contract=contract_u6)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd1])

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME"
    )
    assert selected.localSymbol == "MNQU6"
    mock_ib.reqTickersAsync.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_active_future_contract_empty_raises_error() -> None:
    """Prüft, dass ein ValueError geworfen wird, wenn keine Kontrakte vorhanden sind."""
    mock_ib = MagicMock()
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="No contract details found for future symbol"):
        await resolve_active_future_contract(mock_ib, symbol="XYZ", exchange="CME")
