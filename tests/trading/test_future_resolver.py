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


@pytest.mark.asyncio
async def test_resolve_active_future_contract_all_expired_raises_error() -> None:
    """Prüft, dass ein ValueError geworfen wird, wenn alle Kontrakte verfallen sind."""
    mock_ib = MagicMock()
    expired_contract = Future(
        conId=999,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20200101",
        exchange="CME",
        currency="USD",
        localSymbol="MNQF0",
    )
    cd = ContractDetails(contract=expired_contract)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd])

    with pytest.raises(
        ValueError, match="No non-expired active contracts found for future symbol"
    ):
        await resolve_active_future_contract(mock_ib, symbol="MNQ", exchange="CME")


@pytest.mark.asyncio
async def test_resolve_active_future_contract_none_contract_raises_error() -> None:
    """Prüft, dass ein ValueError geworfen wird, wenn candidate.contract None ist."""
    mock_ib = MagicMock()

    class SneakyDetails:
        def __init__(self) -> None:
            self._count = 0
            self._fut = Future(
                conId=999,
                symbol="MNQ",
                lastTradeDateOrContractMonth="20990101",
                exchange="CME",
                currency="USD",
            )

        @property
        def contract(self) -> Future | None:
            self._count += 1
            # Return contract during list comprehension (3 calls) and sort (2 calls)
            if self._count <= 5:
                return self._fut
            # Return None when candidate_subset[0].contract is accessed
            return None

    sneaky = SneakyDetails()
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[sneaky])
    with pytest.raises(
        ValueError, match="Candidate contract for 'MNQ' is unexpectedly None"
    ):
        await resolve_active_future_contract(mock_ib, symbol="MNQ", exchange="CME")


@pytest.mark.asyncio
async def test_resolve_active_future_contract_timeout_raises_timeout_error() -> None:
    """Prüft, dass ein TimeoutError geworfen wird, wenn reqContractDetailsAsync das Timeout überschreitet."""
    mock_ib = MagicMock()
    mock_ib.reqContractDetailsAsync = AsyncMock(
        side_effect=TimeoutError("Connection timed out")
    )

    with pytest.raises(
        TimeoutError,
        match="Timeout .* resolving contract details for future symbol 'MNQ'",
    ):
        await resolve_active_future_contract(
            mock_ib, symbol="MNQ", exchange="CME", timeout_seconds=0.05
        )


@pytest.mark.asyncio
async def test_resolve_active_future_contract_tickers_timeout_falls_back() -> None:
    """Prüft, dass bei Timeout beim Abruf der Tickers auf den ersten Kandidaten (Front-Month) zurückgegriffen wird."""
    mock_ib = MagicMock()

    contract_u6 = Future(
        conId=1001,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20990918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU99",
    )
    contract_z6 = Future(
        conId=1002,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20991218",
        exchange="CME",
        currency="USD",
        localSymbol="MNQZ99",
    )

    cd1 = ContractDetails(contract=contract_u6)
    cd2 = ContractDetails(contract=contract_z6)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd1, cd2])
    mock_ib.reqTickersAsync = AsyncMock(side_effect=TimeoutError("Tickers timed out"))

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME", timeout_seconds=0.05
    )
    # Front-month contract should be selected as fallback
    assert selected.localSymbol == "MNQU99"
