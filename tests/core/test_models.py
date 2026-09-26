"""Unit tests for domain models and helper functions in app.core.models."""

from decimal import Decimal

import pytest

from app.core.models import decimal_from_db, parse_positive_decimal


def test_decimal_from_db() -> None:
    """Prüft die Konvertierung von DB-Werten in Decimal."""
    assert decimal_from_db(None) is None
    assert decimal_from_db("150.50") == Decimal("150.50")
    assert decimal_from_db(150.5) == Decimal("150.5")


@pytest.mark.parametrize(
    "input_value, expected",
    [
        (None, None),
        (0, None),
        (0.0, None),
        ("0.0", None),
        ("-10.5", None),
        (-5, None),
        ("invalid", None),
        ("150.50", Decimal("150.50")),
        (150.5, Decimal("150.5")),
        (Decimal("42.0"), Decimal("42.0")),
    ],
)
def test_parse_positive_decimal(input_value: object, expected: Decimal | None) -> None:
    """Prüft, dass nur positive Zahlen in Decimal umgewandelt werden, ansonsten None."""
    assert parse_positive_decimal(input_value) == expected


def test_cash_ledger_row_from_db_row() -> None:
    """Verifiziert die Deserialisierung von DB-Zeilen in CashLedgerRow."""
    from app.core.models import cash_ledger_row_from_db_row

    row_data = {
        "ledger_id": 1,
        "account_id": "DU123456",
        "trade_group_id": "TG-1234",
        "symbol": "SAIC",
        "category": "BORROW_FEE",
        "description": "Hard to borrow fee",
        "amount": "-0.03",
        "currency": "USD",
        "fx_rate_to_base": "0.85",
        "amount_in_base": "-0.0255",
        "status": "SETTLED",
        "effective_date": "2026-05-21",
        "settled_date": "2026-05-22",
        "source": "FLEX_QUERY",
        "external_reference_id": "hash-abc-123",
        "created_at": "2026-05-22 05:30:00",
    }
    model = cash_ledger_row_from_db_row(row_data)
    assert model.ledger_id == 1
    assert model.account_id == "DU123456"
    assert model.trade_group_id == "TG-1234"
    assert model.symbol == "SAIC"
    assert model.category == "BORROW_FEE"
    assert model.amount == Decimal("-0.03")
    assert model.fx_rate_to_base == Decimal("0.85")
    assert model.amount_in_base == Decimal("-0.0255")
    assert model.status == "SETTLED"
    assert model.effective_date == "2026-05-21"
    assert model.settled_date == "2026-05-22"
    assert model.source == "FLEX_QUERY"
    assert model.external_reference_id == "hash-abc-123"


def test_settled_trade_all_in_from_db_row() -> None:
    """Verifiziert die Deserialisierung von View-Ergebnissen in SettledTradeAllInRow."""
    from app.core.models import settled_trade_all_in_from_db_row

    row_data = {
        "account_id": "DU123456",
        "trade_group_id": "TG-1234",
        "avg_entry_price": "100.00",
        "avg_exit_price": "105.00",
        "price_diff_slippage": "0.10",
        "trading_commissions": "2.00",
        "trading_net_pnl": "48.00",
        "reg_fees": "-0.50",
        "borrow_fees": "-0.03",
        "net_dividends": "5.00",
        "syep_income": "0.10",
        "all_in_net_pnl": "52.57",
        "has_adjustments": 1,
        "settled_at": "2026-05-22 20:00:00",
    }
    model = settled_trade_all_in_from_db_row(row_data)
    assert model.account_id == "DU123456"
    assert model.trade_group_id == "TG-1234"
    assert model.trading_net_pnl == Decimal("48.00")
    assert model.reg_fees == Decimal("-0.50")
    assert model.borrow_fees == Decimal("-0.03")
    assert model.net_dividends == Decimal("5.00")
    assert model.all_in_net_pnl == Decimal("52.57")
    assert model.has_adjustments is True
