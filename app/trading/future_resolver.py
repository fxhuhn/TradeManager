"""Modul zur dynamischen Ermittlung und Auflösung aktiver Future-Kontrakte.

Identifiziert unter den verfügbaren Quartalskontrakten an der Börse
den Kontrakt mit dem aktuell höchsten Handelsvolumen (Front-Month bzw. Roll-Kontrakt).
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import Final, cast

import structlog
from ib_async import IB, ContractDetails, Future

logger = structlog.get_logger()

# Standardmäßiger Marktdatentyp: 3 = Verzögerte Marktdaten (Delayed), 1 = Live
DEFAULT_MARKET_DATA_TYPE: Final[int] = 3
DEFAULT_CONTRACT_TIMEOUT_SECONDS: Final[float] = 15.0


async def resolve_active_future_contract(
    interactive_brokers: IB,
    symbol: str = "MNQ",
    exchange: str = "CME",
    currency: str = "USD",
    timeout_seconds: float = DEFAULT_CONTRACT_TIMEOUT_SECONDS,
) -> Future:
    """Ermittelt den liquidesten, aktiven Future-Kontrakt mit dem höchsten Handelsvolumen.

    Fragt alle verfügbaren Kontrakte für das angegebene Symbol ab, filtert bereits
    abgelaufene Kontrakte heraus, analysiert das Handelsvolumen der beiden nächsten
    Quartalsfälligkeiten und gibt das qualifizierte Future-Objekt zurück.

    Args:
        interactive_brokers: Aktive Instanz des Interactive Brokers API Clients.
        symbol: Das Basis-Ticker-Symbol (z. B. 'MNQ' oder 'MES').
        exchange: Zielbörse des Futures (Standard: 'CME').
        currency: Währung des Kontrakts (Standard: 'USD').
        timeout_seconds: Maximales Timeout für API-Anfragen an den Broker (Standard: 15.0s).

    Returns:
        Ein vollständig qualifiziertes ib_async Future-Objekt.

    Raises:
        TimeoutError: Falls die Broker-Abfrage das Timeout überschreitet.
        ValueError: Falls keine aktiven Kontrakte für das Symbol gefunden werden.
    """
    logger.info(
        "Resolving active future contract with highest volume",
        symbol=symbol,
        exchange=exchange,
    )

    search_contract = Future(symbol=symbol, exchange=exchange, currency=currency)
    try:
        contract_details_list: list[ContractDetails] = await asyncio.wait_for(
            interactive_brokers.reqContractDetailsAsync(search_contract),
            timeout=timeout_seconds,
        )
    except TimeoutError as err:
        error_message = f"Timeout ({timeout_seconds}s) resolving contract details for future symbol '{symbol}'."
        logger.error(error_message, symbol=symbol, exchange=exchange)
        raise TimeoutError(error_message) from err

    if not contract_details_list:
        error_message = f"No contract details found for future symbol '{symbol}' on exchange '{exchange}'."
        logger.error(error_message)
        raise ValueError(error_message)

    today_string = datetime.now().strftime("%Y%m%d")
    active_candidates: list[ContractDetails] = [
        details
        for details in contract_details_list
        if details.contract is not None
        and details.contract.lastTradeDateOrContractMonth is not None
        and details.contract.lastTradeDateOrContractMonth >= today_string
    ]

    if not active_candidates:
        error_message = (
            f"No non-expired active contracts found for future symbol '{symbol}'."
        )
        logger.error(error_message)
        raise ValueError(error_message)

    # Sortiere chronologisch nach Verfallsdatum (nächste Fälligkeit zuerst)
    active_candidates.sort(
        key=lambda item: str(
            item.contract.lastTradeDateOrContractMonth if item.contract else ""
        )
    )

    # Betrachte die beiden nächsten Quartalsfälligkeiten für den Volumenvergleich
    candidate_subset = active_candidates[:2]
    first_contract = candidate_subset[0].contract
    if first_contract is None:
        raise ValueError(f"Candidate contract for '{symbol}' is unexpectedly None.")

    if len(candidate_subset) == 1:
        chosen_contract: Future = cast(Future, first_contract)
        logger.info(
            "Only one active future contract candidate found",
            local_symbol=chosen_contract.localSymbol,
            expiry=chosen_contract.lastTradeDateOrContractMonth,
        )
        return chosen_contract

    # Verzögerte Marktdaten aktivieren, um verlässliche Volumendaten abzurufen
    interactive_brokers.reqMarketDataType(DEFAULT_MARKET_DATA_TYPE)
    contracts_to_query = [
        candidate.contract
        for candidate in candidate_subset
        if candidate.contract is not None
    ]
    try:
        tickers = await asyncio.wait_for(
            interactive_brokers.reqTickersAsync(*contracts_to_query),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "Timeout fetching market data tickers for future candidates, falling back to front-month",
            symbol=symbol,
            timeout_seconds=timeout_seconds,
        )
        tickers = []

    best_candidate = candidate_subset[0]
    highest_volume = -1.0

    for candidate in candidate_subset:
        if candidate.contract is None:
            continue
        candidate_con_id = candidate.contract.conId
        matching_ticker = next(
            (
                ticker
                for ticker in tickers
                if ticker.contract is not None
                and ticker.contract.conId == candidate_con_id
            ),
            None,
        )

        volume = 0.0
        if matching_ticker and matching_ticker.volume is not None:
            raw_volume = float(matching_ticker.volume)
            if not math.isnan(raw_volume):
                volume = raw_volume

        logger.info(
            "Evaluated candidate volume",
            local_symbol=candidate.contract.localSymbol,
            expiry=candidate.contract.lastTradeDateOrContractMonth,
            volume=volume,
        )

        if volume > highest_volume:
            highest_volume = volume
            best_candidate = candidate

    final_contract = best_candidate.contract
    if final_contract is None:
        raise ValueError(f"Resolved future contract for '{symbol}' is None.")

    selected_contract: Future = cast(Future, final_contract)
    logger.info(
        "Successfully resolved highest-volume future contract",
        symbol=symbol,
        local_symbol=selected_contract.localSymbol,
        expiry=selected_contract.lastTradeDateOrContractMonth,
        con_id=selected_contract.conId,
        volume=highest_volume,
    )

    return selected_contract
