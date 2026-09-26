"""Unit-Tests für die Flex-Query-Matching-Engine (Functional Core)."""

from decimal import Decimal

from app.services.flex_query.matcher import HistoricalTradeContext, match_flex_statement
from app.services.flex_query.models import (
    FlexBorrowFeeRecord,
    FlexCashTransactionRecord,
    FlexDividendAccrualRecord,
    ParsedFlexStatement,
)


def test_match_borrow_fee_to_short_trade() -> None:
    """Verifiziert die Zuordnung einer Hard-to-Borrow-Gebühr zu einem Short-Trade."""
    statement = ParsedFlexStatement(
        account_id="DU123456",
        from_date="2026-05-20",
        to_date="2026-05-25",
        when_generated="20260525;100000",
        borrow_fees=(
            FlexBorrowFeeRecord(
                account_id="DU123456",
                value_date="2026-05-21",
                symbol="SAIC",
                description="Hard to borrow SAIC",
                quantity=Decimal("30"),
                borrow_fee_rate=Decimal("0.38"),
                borrow_fee=Decimal("-0.03"),
                currency="USD",
                fx_rate_to_base=Decimal("0.85"),
            ),
        ),
    )

    trades = (
        HistoricalTradeContext(
            account_id="DU123456",
            trade_group_id="TG-SAIC-SHORT",
            symbol="SAIC",
            action="SELL",
            quantity=Decimal("30"),
            entry_date="2026-05-20",
            exit_date="2026-05-25",
        ),
    )

    ledger_rows = match_flex_statement(statement, trades)
    assert len(ledger_rows) == 1
    row = ledger_rows[0]
    assert row.trade_group_id == "TG-SAIC-SHORT"
    assert row.category == "BORROW_FEE"
    assert row.amount == Decimal("-0.03")
    assert row.amount_in_base == Decimal("-0.0255")
    assert row.status == "SETTLED"


def test_match_dividend_accrual_to_long_trade() -> None:
    """Verifiziert die Zuordnung eines Dividendenanspruchs (Pending) zu einem Long-Trade."""
    statement = ParsedFlexStatement(
        account_id="DU123456",
        from_date="2026-09-01",
        to_date="2026-09-15",
        when_generated="20260915;100000",
        dividend_accruals=(
            FlexDividendAccrualRecord(
                account_id="DU123456",
                symbol="NVDA",
                description="NVIDIA CORP",
                ex_date="2026-09-10",
                pay_date="2026-10-01",
                quantity=Decimal("13"),
                gross_rate=Decimal("0.25"),
                gross_amount=Decimal("3.25"),
                tax=Decimal("0.49"),
                fee=Decimal("0.0"),
                net_amount=Decimal("2.76"),
                currency="USD",
            ),
        ),
    )

    trades = (
        HistoricalTradeContext(
            account_id="DU123456",
            trade_group_id="TG-NVDA-LONG",
            symbol="NVDA",
            action="BUY",
            quantity=Decimal("13"),
            entry_date="2026-09-08",
            exit_date="2026-09-18",
        ),
    )

    ledger_rows = match_flex_statement(statement, trades)
    # Erwartet: 1x Brutto-Dividende + 1x Quellensteuer
    assert len(ledger_rows) == 2

    gross_row = next(r for r in ledger_rows if r.category == "DIVIDEND")
    assert gross_row.trade_group_id == "TG-NVDA-LONG"
    assert gross_row.amount == Decimal("3.25")
    assert gross_row.status == "PENDING"
    assert gross_row.effective_date == "2026-09-10"
    assert gross_row.settled_date == "2026-10-01"

    tax_row = next(r for r in ledger_rows if r.category == "WITHHOLDING_TAX")
    assert tax_row.trade_group_id == "TG-NVDA-LONG"
    assert tax_row.amount == Decimal("-0.49")
    assert tax_row.status == "PENDING"


def test_match_account_level_cash_transactions() -> None:
    """Verifiziert die Zuweisung von Marktdaten und Zinsen zur Account-Ebene."""
    statement = ParsedFlexStatement(
        account_id="DU123456",
        from_date="2026-09-01",
        to_date="2026-09-15",
        when_generated="20260915;100000",
        cash_transactions=(
            FlexCashTransactionRecord(
                account_id="DU123456",
                date_time="20260909;170130",
                transaction_type="Other Fees",
                description="CME (GLOBEX) (NP, L1) FOR SEP 2026",
                symbol="",
                amount=Decimal("-1.33"),
                currency="EUR",
                fx_rate_to_base=Decimal("1.0"),
            ),
            FlexCashTransactionRecord(
                account_id="DU123456",
                date_time="20260903",
                transaction_type="Broker Interest Received",
                description="EUR IBKR MANAGED SECURITIES (SYEP) INTEREST FOR AUG-2026",
                symbol="",
                amount=Decimal("0.12"),
                currency="EUR",
                fx_rate_to_base=Decimal("1.0"),
            ),
        ),
    )

    ledger_rows = match_flex_statement(statement, historical_trades=())
    assert len(ledger_rows) == 2

    cme_row = next(r for r in ledger_rows if r.category == "MARKET_DATA")
    assert cme_row.trade_group_id is None
    assert cme_row.amount == Decimal("-1.33")

    syep_row = next(r for r in ledger_rows if r.category == "SYEP_INCOME")
    assert syep_row.trade_group_id is None
    assert syep_row.amount == Decimal("0.12")


def test_match_ignores_capital_transfers_deposits_and_withdrawals() -> None:
    """Verifiziert, dass Eigenkapital-Einlagen und -Entnahmen nicht im cash_ledger landen."""
    statement = ParsedFlexStatement(
        account_id="DU123456",
        from_date="2026-06-01",
        to_date="2026-06-30",
        when_generated="20260630;100000",
        cash_transactions=(
            FlexCashTransactionRecord(
                account_id="DU123456",
                date_time="20260601",
                transaction_type="Deposits/Withdrawals",
                description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS",
                symbol="",
                amount=Decimal("20000.00"),
                currency="EUR",
                fx_rate_to_base=Decimal("1.0"),
            ),
            FlexCashTransactionRecord(
                account_id="DU123456",
                date_time="20260601",
                transaction_type="Electronic Fund Transfers",
                description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS",
                symbol="",
                amount=Decimal("10000.00"),
                currency="EUR",
                fx_rate_to_base=Decimal("1.0"),
            ),
            FlexCashTransactionRecord(
                account_id="DU123456",
                date_time="20260615",
                transaction_type="Deposits/Withdrawals",
                description="WIRE WITHDRAWAL",
                symbol="",
                amount=Decimal("-5000.00"),
                currency="EUR",
                fx_rate_to_base=Decimal("1.0"),
            ),
            FlexCashTransactionRecord(
                account_id="DU123456",
                date_time="20260609;170130",
                transaction_type="Other Fees",
                description="CME (GLOBEX) FOR JUN 2026",
                symbol="",
                amount=Decimal("-1.33"),
                currency="EUR",
                fx_rate_to_base=Decimal("1.0"),
            ),
        ),
    )

    ledger_rows = match_flex_statement(statement, historical_trades=())
    # Nur die echte Marktdatengebühr darf übernommen werden
    assert len(ledger_rows) == 1
    assert ledger_rows[0].category == "MARKET_DATA"
    assert ledger_rows[0].amount == Decimal("-1.33")


def test_match_real_other_fee_recorded() -> None:
    """Verifiziert, dass echte sonstige Broker-Gebühren als OTHER_FEE verbucht werden."""
    statement = ParsedFlexStatement(
        account_id="DU123456",
        from_date="2026-06-01",
        to_date="2026-06-30",
        when_generated="20260630;100000",
        cash_transactions=(
            FlexCashTransactionRecord(
                account_id="DU123456",
                date_time="20260630",
                transaction_type="Other Fees",
                description="MONTHLY ACCOUNT INACTIVITY FEE",
                symbol="",
                amount=Decimal("-15.00"),
                currency="USD",
                fx_rate_to_base=Decimal("0.90"),
            ),
        ),
    )

    ledger_rows = match_flex_statement(statement, historical_trades=())
    assert len(ledger_rows) == 1
    assert ledger_rows[0].category == "OTHER_FEE"
    assert ledger_rows[0].amount == Decimal("-15.00")
    assert ledger_rows[0].description == "MONTHLY ACCOUNT INACTIVITY FEE"


def test_match_unrecognized_cash_transaction_skipped() -> None:
    """Verifiziert, dass unbekannte Cash-Transaktionen ohne Gebühren-Typ übersprungen werden."""
    statement = ParsedFlexStatement(
        account_id="DU123456",
        from_date="2026-06-01",
        to_date="2026-06-30",
        when_generated="20260630;100000",
        cash_transactions=(
            FlexCashTransactionRecord(
                account_id="DU123456",
                date_time="20260630",
                transaction_type="Unknown Special Flow",
                description="MISCELLANEOUS CORPORATE FLOW",
                symbol="",
                amount=Decimal("123.45"),
                currency="USD",
                fx_rate_to_base=Decimal("1.0"),
            ),
        ),
    )

    ledger_rows = match_flex_statement(statement, historical_trades=())
    assert len(ledger_rows) == 0
