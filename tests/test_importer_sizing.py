import asyncio
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from ib_async import AccountValue

from app.services.importer import (
    determine_maximum_capital_allocation,
    fetch_account_balance_metrics,
)


def test_determine_maximum_capital_allocation_total_cash() -> None:
    """Prüft, dass bei sizing_mode 'total_cash' nur der TotalCashValue zurückgegeben wird."""
    allocation = determine_maximum_capital_allocation(
        net_liquidation_value=Decimal("100000.0"),
        available_funds_value=Decimal("50000.0"),
        total_cash_value=Decimal("40000.0"),
        margin_multiplier_factor=Decimal("2.0"),
        sizing_mode="total_cash",
        allocation_limit_percentage=Decimal("0.05"),
    )
    assert allocation == Decimal("40000.0")


def test_determine_maximum_capital_allocation_margin_adjusted() -> None:
    """Prüft, dass bei margin_adjusted_capital das theoretische Limit (NLV * Margin * Limit) greift."""
    allocation = determine_maximum_capital_allocation(
        net_liquidation_value=Decimal("100000.0"),
        available_funds_value=Decimal("50000.0"),
        total_cash_value=Decimal("40000.0"),
        margin_multiplier_factor=Decimal("2.0"),
        sizing_mode="margin_adjusted_capital",
        allocation_limit_percentage=Decimal("0.05"),
    )
    # 100000 * 2.0 * 0.05 = 10000. Capped by available_funds * 2.0 = 100000.
    assert allocation == Decimal("10000.0")


def test_determine_maximum_capital_allocation_margin_adjusted_limited_by_funds() -> None:
    """Prüft, dass bei unzureichendem AvailableFunds das Limit durch AvailableFunds * Margin gedeckelt wird."""
    allocation = determine_maximum_capital_allocation(
        net_liquidation_value=Decimal("100000.0"),
        available_funds_value=Decimal("10000.0"),
        total_cash_value=Decimal("5000.0"),
        margin_multiplier_factor=Decimal("2.0"),
        sizing_mode="margin_adjusted_capital",
        allocation_limit_percentage=Decimal("0.50"),
    )
    # 100000 * 2.0 * 0.50 = 100000. Capped by available_funds * 2.0 = 20000.
    assert allocation == Decimal("20000.0")


@pytest.mark.asyncio
async def test_fetch_account_balance_metrics_from_cache() -> None:
    """Prüft das Laden der Kontowerte aus dem Cache."""
    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [
        AccountValue(account="U123", tag="NetLiquidation", value="80000.00", currency="EUR", modelCode=""),
        AccountValue(account="U123", tag="AvailableFunds", value="50000.00", currency="EUR", modelCode=""),
        AccountValue(account="U123", tag="TotalCashValue", value="35000.00", currency="EUR", modelCode=""),
    ]

    metrics = await fetch_account_balance_metrics(mock_ib, "U123")
    assert metrics.net_liquidation_value == Decimal("80000.00")
    assert metrics.available_funds_value == Decimal("50000.00")
    assert metrics.total_cash_value == Decimal("35000.00")
    mock_ib.reqAccountSummary.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_account_balance_metrics_from_summary() -> None:
    """Prüft das Laden der Kontowerte per Fallback via reqAccountSummary."""
    mock_ib = MagicMock()
    # Leerer Cache
    mock_ib.accountValues.return_value = []

    # Callback registrieren simulieren
    registered_callback = None
    def mock_connect(callback):
        nonlocal registered_callback
        registered_callback = callback

    mock_ib.accountSummaryEvent.connect = mock_connect

    async def mock_req_summary():
        # Simuliere TWS Callback asynchron nach kurzer Zeit
        await asyncio.sleep(0.01)
        if registered_callback:
            registered_callback(1, "U123", "NetLiquidation", "90000.0", "EUR")
            registered_callback(1, "U123", "AvailableFunds", "60000.0", "EUR")
            registered_callback(1, "U123", "TotalCashValue", "40000.0", "EUR")

    mock_ib.reqAccountSummary.side_effect = lambda: asyncio.create_task(mock_req_summary())

    metrics = await fetch_account_balance_metrics(mock_ib, "U123")
    assert metrics.net_liquidation_value == Decimal("90000.0")
    assert metrics.available_funds_value == Decimal("60000.0")
    assert metrics.total_cash_value == Decimal("40000.0")

    mock_ib.reqAccountSummary.assert_called_once()
    mock_ib.cancelAccountSummary.assert_called_once()
