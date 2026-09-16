"""Unit-Tests für die dynamische Future-Kontraktauflösung (future_resolver.py)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_async import ContractDetails, Future, Ticker

from app.trading.future_resolver import (
    calculate_days_to_expiration,
    resolve_active_future_contract,
)


def test_calculate_days_to_expiration() -> None:
    """Prüft die korrekte Berechnung der Kalendertage bis zum Verfall."""
    ref_date = datetime(2026, 9, 16, 12, 0, 0)

    # 10 Tage in der Zukunft
    assert calculate_days_to_expiration("20260926", ref_date) == 10
    # 9 Tage in der Zukunft (< 10)
    assert calculate_days_to_expiration("20260925", ref_date) == 9
    # Gleicher Tag (0 Tage)
    assert calculate_days_to_expiration("20260916", ref_date) == 0
    # In der Vergangenheit
    assert calculate_days_to_expiration("20260910", ref_date) == -6
    # Mit Uhrzeit und Leerzeichen
    assert calculate_days_to_expiration("20260926 16:00:00 US/Eastern", ref_date) == 10

    # Ungültige Formate
    with pytest.raises(ValueError, match="Invalid expiry date string format"):
        calculate_days_to_expiration("2026", ref_date)
    with pytest.raises(ValueError, match="Invalid expiry date string format"):
        calculate_days_to_expiration("abcdefgh", ref_date)


@pytest.mark.asyncio
async def test_resolve_active_future_contract_highest_volume() -> None:
    """Prüft, ob der Kontrakt mit dem höheren Handelsvolumen ausgewählt wird."""
    mock_ib = MagicMock()

    contract_u = Future(
        conId=1001,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20990918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU99",
    )
    contract_z = Future(
        conId=1002,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20991218",
        exchange="CME",
        currency="USD",
        localSymbol="MNQZ99",
    )

    cd1 = ContractDetails(contract=contract_u)
    cd2 = ContractDetails(contract=contract_z)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd1, cd2])

    ticker_u = MagicMock(spec=Ticker)
    ticker_u.contract = contract_u
    ticker_u.volume = 500000.0

    ticker_z = MagicMock(spec=Ticker)
    ticker_z.contract = contract_z
    ticker_z.volume = 12000.0

    mock_ib.reqTickersAsync = AsyncMock(return_value=[ticker_u, ticker_z])

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME"
    )
    assert selected.localSymbol == "MNQU99"
    assert selected.conId == 1001
    mock_ib.reqMarketDataType.assert_called_once_with(3)


@pytest.mark.asyncio
async def test_resolve_active_future_contract_roll_to_next_month() -> None:
    """Prüft, ob nach dem Roll der Folgemonat gewählt wird, wenn dieser mehr Volumen hat."""
    mock_ib = MagicMock()

    contract_u = Future(
        conId=1001,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20990918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU99",
    )
    contract_z = Future(
        conId=1002,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20991218",
        exchange="CME",
        currency="USD",
        localSymbol="MNQZ99",
    )

    cd1 = ContractDetails(contract=contract_u)
    cd2 = ContractDetails(contract=contract_z)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd1, cd2])

    ticker_u = MagicMock(spec=Ticker)
    ticker_u.contract = contract_u
    ticker_u.volume = 2000.0

    ticker_z = MagicMock(spec=Ticker)
    ticker_z.contract = contract_z
    ticker_z.volume = 450000.0  # Folgemonat hat nach dem Roll das Hauptvolumen

    mock_ib.reqTickersAsync = AsyncMock(return_value=[ticker_u, ticker_z])

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME"
    )
    assert selected.localSymbol == "MNQZ99"
    assert selected.conId == 1002


@pytest.mark.asyncio
async def test_resolve_active_future_contract_skips_contract_under_10_days() -> None:
    """Prüft, dass ein Kontrakt mit < 10 Tagen Restlaufzeit übersprungen wird, selbst bei hohem Volumen."""
    mock_ib = MagicMock()

    today = date.today()
    # Expire in 5 days (under 10 days)
    short_expiry_str = (today.fromordinal(today.toordinal() + 5)).strftime("%Y%m%d")
    # Expire in 90 days (valid next contract)
    valid_expiry_str = (today.fromordinal(today.toordinal() + 90)).strftime("%Y%m%d")

    short_contract = Future(
        conId=2001,
        symbol="MNQ",
        lastTradeDateOrContractMonth=short_expiry_str,
        exchange="CME",
        currency="USD",
        localSymbol="MNQ_SHORT",
    )
    valid_contract = Future(
        conId=2002,
        symbol="MNQ",
        lastTradeDateOrContractMonth=valid_expiry_str,
        exchange="CME",
        currency="USD",
        localSymbol="MNQ_NEXT",
    )

    cd_short = ContractDetails(contract=short_contract)
    cd_valid = ContractDetails(contract=valid_contract)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd_short, cd_valid])

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME", min_days_to_expiration=10
    )
    # The short contract must be filtered out, leaving only the next contract
    assert selected.localSymbol == "MNQ_NEXT"
    assert selected.conId == 2002


@pytest.mark.asyncio
async def test_resolve_active_future_contract_accepts_contract_exactly_10_days() -> (
    None
):
    """Prüft, dass ein Kontrakt mit exakt 10 Tagen Restlaufzeit erhalten bleibt (< 10 Bedingung)."""
    mock_ib = MagicMock()

    today = date.today()
    # Exakt 10 Tage Restlaufzeit
    exact_10_expiry_str = (today.fromordinal(today.toordinal() + 10)).strftime("%Y%m%d")

    exact_contract = Future(
        conId=3001,
        symbol="MNQ",
        lastTradeDateOrContractMonth=exact_10_expiry_str,
        exchange="CME",
        currency="USD",
        localSymbol="MNQ_EXACT10",
    )

    cd = ContractDetails(contract=exact_contract)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd])

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME", min_days_to_expiration=10
    )
    assert selected.localSymbol == "MNQ_EXACT10"
    assert selected.conId == 3001


@pytest.mark.asyncio
async def test_resolve_active_future_contract_single_candidate() -> None:
    """Prüft, dass bei nur einem aktiven Kontrakt dieser sofort gewählt wird."""
    mock_ib = MagicMock()

    contract_u = Future(
        conId=1001,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20990918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU99",
    )
    cd1 = ContractDetails(contract=contract_u)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd1])

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME"
    )
    assert selected.localSymbol == "MNQU99"
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
        ValueError, match="No active contracts with >= 10 days to expiration found"
    ):
        await resolve_active_future_contract(mock_ib, symbol="MNQ", exchange="CME")


@pytest.mark.asyncio
async def test_resolve_active_future_contract_no_candidates_with_min_days_raises_error() -> (
    None
):
    """Prüft, dass ValueError geworfen wird, wenn alle Kontrakte weniger als min_days Restlaufzeit haben."""
    mock_ib = MagicMock()

    today = date.today()
    short_1 = (today.fromordinal(today.toordinal() + 3)).strftime("%Y%m%d")
    short_2 = (today.fromordinal(today.toordinal() + 7)).strftime("%Y%m%d")

    c1 = Future(
        conId=5001,
        symbol="MNQ",
        lastTradeDateOrContractMonth=short_1,
        exchange="CME",
        currency="USD",
    )
    c2 = Future(
        conId=5002,
        symbol="MNQ",
        lastTradeDateOrContractMonth=short_2,
        exchange="CME",
        currency="USD",
    )
    mock_ib.reqContractDetailsAsync = AsyncMock(
        return_value=[ContractDetails(contract=c1), ContractDetails(contract=c2)]
    )

    with pytest.raises(
        ValueError, match="No active contracts with >= 10 days to expiration found"
    ):
        await resolve_active_future_contract(
            mock_ib, symbol="MNQ", exchange="CME", min_days_to_expiration=10
        )


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

    contract_u = Future(
        conId=1001,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20990918",
        exchange="CME",
        currency="USD",
        localSymbol="MNQU99",
    )
    contract_z = Future(
        conId=1002,
        symbol="MNQ",
        lastTradeDateOrContractMonth="20991218",
        exchange="CME",
        currency="USD",
        localSymbol="MNQZ99",
    )

    cd1 = ContractDetails(contract=contract_u)
    cd2 = ContractDetails(contract=contract_z)
    mock_ib.reqContractDetailsAsync = AsyncMock(return_value=[cd1, cd2])
    mock_ib.reqTickersAsync = AsyncMock(side_effect=TimeoutError("Tickers timed out"))

    selected = await resolve_active_future_contract(
        mock_ib, symbol="MNQ", exchange="CME", timeout_seconds=0.05
    )
    # Front-month contract should be selected as fallback
    assert selected.localSymbol == "MNQU99"
