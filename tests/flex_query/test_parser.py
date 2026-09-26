"""Unit-Tests für den Flex Query XML-Parser unter Verwendung echter Beispieldaten."""

from decimal import Decimal

import pytest

from app.services.flex_query.parser import (
    generate_external_reference_id,
    parse_flex_xml,
    parse_ibkr_date,
    to_decimal,
)


def test_to_decimal_conversion() -> None:
    """Prüft die sichere Konvertierung beliebiger Typen in Decimal."""
    assert to_decimal("123.45") == Decimal("123.45")
    assert to_decimal("-0.03") == Decimal("-0.03")
    assert to_decimal(None) == Decimal("0.0")
    assert to_decimal("") == Decimal("0.0")
    assert to_decimal("invalid") == Decimal("0.0")


def test_parse_ibkr_date() -> None:
    """Prüft die Normalisierung von Datumsstrings."""
    assert parse_ibkr_date("20260918") == "2026-09-18"
    assert parse_ibkr_date("20260130;202000") == "2026-01-30"
    assert parse_ibkr_date("already-formatted") == "already-formatted"


def test_generate_external_reference_id() -> None:
    """Verifiziert die deterministische Hash-Erzeugung."""
    hash_one = generate_external_reference_id("U123", "20260101", "DIVIDEND", "10.0")
    hash_two = generate_external_reference_id("U123", "20260101", "DIVIDEND", "10.0")
    hash_three = generate_external_reference_id("U123", "20260101", "DIVIDEND", "10.1")

    assert hash_one == hash_two
    assert hash_one != hash_three
    assert len(hash_one) == 32


def test_parse_sample_flex_xml(sample_flex_xml: str) -> None:
    """Parst das anonymisierte Flex-Statement."""
    statement = parse_flex_xml(sample_flex_xml)

    assert statement.account_id == "DU123456"
    assert statement.from_date == "2026-01-01"
    assert statement.to_date == "2026-09-17"

    # 1. HardToBorrowDetails prüfen
    assert len(statement.borrow_fees) == 5
    first_htb = statement.borrow_fees[0]
    assert first_htb.symbol == "SAIC"
    assert first_htb.value_date == "2026-05-21"
    assert first_htb.quantity == Decimal("30")
    assert first_htb.borrow_fee_rate == Decimal("0.3804")
    assert first_htb.borrow_fee == Decimal("-0.03")

    # 2. OpenDividendAccruals prüfen
    assert len(statement.dividend_accruals) == 1
    nvda_accrual = statement.dividend_accruals[0]
    assert nvda_accrual.symbol == "NVDA"
    assert nvda_accrual.ex_date == "2026-09-10"
    assert nvda_accrual.pay_date == "2026-10-01"
    assert nvda_accrual.gross_amount == Decimal("3.25")
    assert nvda_accrual.tax == Decimal("0.49")
    assert nvda_accrual.net_amount == Decimal("2.76")

    # 3. CashTransactions prüfen
    assert len(statement.cash_transactions) > 0
    # Dividenden-Eintrag für MAA suchen
    maa_divs = [
        tx
        for tx in statement.cash_transactions
        if tx.symbol == "MAA" and tx.transaction_type == "Dividends"
    ]
    assert len(maa_divs) == 1
    assert maa_divs[0].amount == Decimal("47.43")
    assert maa_divs[0].fx_rate_to_base == Decimal("0.84387")


def test_parse_invalid_xml() -> None:
    """Verifiziert das Fehlerverhalten bei fehlerhaftem XML."""
    with pytest.raises(ValueError, match="Failed to parse Flex Query XML"):
        parse_flex_xml("<invalid>")

    with pytest.raises(ValueError, match="does not contain a valid <FlexStatement>"):
        parse_flex_xml("<FlexQueryResponse></FlexQueryResponse>")
