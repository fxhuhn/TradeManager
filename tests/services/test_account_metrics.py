"""Unit-Tests für den Kontokennzahlen-Dienst app.services.account_metrics."""

from decimal import Decimal
from unittest.mock import MagicMock

import aiosqlite
import pytest
from ib_async import AccountValue

from app.services.account_metrics import (
    AccountMetricsSnapshot,
    get_latest_account_metrics,
    save_account_metrics,
    sync_and_save_account_metrics,
)


@pytest.mark.asyncio
async def test_save_and_get_account_metrics(db: aiosqlite.Connection) -> None:
    """Prüft das atomare Speichern und Auslesen von Kontokennzahlen."""
    snapshot = AccountMetricsSnapshot(
        account_id="U12345",
        net_liquidation=Decimal("125000.50"),
        total_cash_value=Decimal("45000.00"),
        available_funds=Decimal("80000.50"),
        maint_margin_req=Decimal("35000.00"),
        cushion_pct=Decimal("68.5"),
        buying_power=Decimal("320000.00"),
    )

    await save_account_metrics(db, "U12345", snapshot)

    loaded = await get_latest_account_metrics(db, "U12345")
    assert loaded is not None
    assert loaded.account_id == "U12345"
    assert loaded.net_liquidation == Decimal("125000.50")
    assert loaded.total_cash_value == Decimal("45000.00")
    assert loaded.available_funds == Decimal("80000.50")
    assert loaded.maint_margin_req == Decimal("35000.00")
    assert loaded.cushion_pct == Decimal("68.5")
    assert loaded.buying_power == Decimal("320000.00")
    assert loaded.updated_at != ""


@pytest.mark.asyncio
async def test_save_account_metrics_upsert(db: aiosqlite.Connection) -> None:
    """Prüft das Überschreiben bestehender Kontokennzahlen (UPSERT)."""
    first_snapshot = AccountMetricsSnapshot(
        account_id="U12345",
        net_liquidation=Decimal("100000.00"),
        total_cash_value=Decimal("50000.00"),
        available_funds=Decimal("50000.00"),
        maint_margin_req=Decimal("20000.00"),
        cushion_pct=Decimal("80.0"),
        buying_power=Decimal("200000.00"),
    )
    await save_account_metrics(db, "U12345", first_snapshot)

    updated_snapshot = AccountMetricsSnapshot(
        account_id="U12345",
        net_liquidation=Decimal("105000.00"),
        total_cash_value=Decimal("55000.00"),
        available_funds=Decimal("50000.00"),
        maint_margin_req=Decimal("22000.00"),
        cushion_pct=Decimal("78.0"),
        buying_power=Decimal("210000.00"),
    )
    await save_account_metrics(db, "U12345", updated_snapshot)

    loaded = await get_latest_account_metrics(db)
    assert loaded is not None
    assert loaded.net_liquidation == Decimal("105000.00")
    assert loaded.cushion_pct == Decimal("78.0")


@pytest.mark.asyncio
async def test_get_latest_account_metrics_empty(db: aiosqlite.Connection) -> None:
    """Prüft, dass None zurückgegeben wird, wenn keine Daten vorliegen."""
    loaded = await get_latest_account_metrics(db, "NON_EXISTENT")
    assert loaded is None


@pytest.mark.asyncio
async def test_sync_and_save_account_metrics_success(db: aiosqlite.Connection) -> None:
    """Prüft die erfolgreiche Synchronisation mit Interactive Brokers und Persistierung."""
    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [
        AccountValue(
            account="U999",
            tag="NetLiquidation",
            value="150000.00",
            currency="USD",
            modelCode="",
        ),
        AccountValue(
            account="U999",
            tag="AvailableFunds",
            value="95000.00",
            currency="USD",
            modelCode="",
        ),
        AccountValue(
            account="U999",
            tag="TotalCashValue",
            value="60000.00",
            currency="USD",
            modelCode="",
        ),
        AccountValue(
            account="U999",
            tag="MaintMarginReq",
            value="40000.00",
            currency="USD",
            modelCode="",
        ),
        AccountValue(
            account="U999",
            tag="Cushion",
            value="0.75",
            currency="",
            modelCode="",
        ),
        AccountValue(
            account="U999",
            tag="BuyingPower",
            value="380000.00",
            currency="USD",
            modelCode="",
        ),
    ]

    snapshot = await sync_and_save_account_metrics(mock_ib, "U999", db)
    assert snapshot is not None
    assert snapshot.account_id == "U999"
    assert snapshot.net_liquidation == Decimal("150000.00")
    assert snapshot.cushion_pct == Decimal("75.00")
    assert snapshot.maint_margin_req == Decimal("40000.00")

    loaded = await get_latest_account_metrics(db, "U999")
    assert loaded is not None
    assert loaded.net_liquidation == Decimal("150000.00")
    assert loaded.cushion_pct == Decimal("75.00")


@pytest.mark.asyncio
async def test_sync_and_save_account_metrics_handles_failure(
    db: aiosqlite.Connection,
) -> None:
    """Prüft, dass Fehler bei der IB-Abfrage sauber abgefangen werden."""
    mock_ib = MagicMock()
    mock_ib.accountValues.side_effect = RuntimeError("Broker disconnected")

    snapshot = await sync_and_save_account_metrics(mock_ib, "U999", db)
    assert snapshot is None
