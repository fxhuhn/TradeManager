"""Unit-Tests für die Flex-Query-Matching-Engine (Functional Core)."""

from decimal import Decimal

from app.services.flex_query.matcher import (
    HistoricalTradeContext,
    match_flex_statement,
    match_flex_trades,
)
from app.services.flex_query.models import (
    FlexBorrowFeeRecord,
    FlexCashTransactionRecord,
    FlexDividendAccrualRecord,
    FlexTradeRecord,
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


def test_match_trade_fees_with_direct_id_and_unbundled_components() -> None:
    """Verifiziert die Buchung aller unbundled Gebühren (Reg, Exch, Clearing, Other) über trade_id."""
    from app.services.flex_query.models import FlexTradeFeeRecord

    statement = ParsedFlexStatement(
        account_id="DU123456",
        from_date="2026-06-01",
        to_date="2026-06-30",
        when_generated="20260630;100000",
        trade_fees=(
            FlexTradeFeeRecord(
                account_id="DU123456",
                symbol="AAPL",
                date_time="2026-06-15",
                buy_sell="BUY",
                quantity=Decimal("100"),
                price=Decimal("150.00"),
                total_commission=Decimal("2.50"),
                broker_execution_charge=Decimal("1.00"),
                broker_clearing_charge=Decimal("0.50"),
                third_party_execution_charge=Decimal("0.30"),
                third_party_clearing_charge=Decimal("0.20"),
                third_party_regulatory_charge=Decimal("0.10"),
                other=Decimal("0.05"),
                currency="USD",
                fx_rate_to_base=Decimal("1.0"),
                trade_id="EXEC-12345",
            ),
        ),
    )

    trades = (
        HistoricalTradeContext(
            account_id="DU123456",
            trade_group_id="TG-AAPL-01",
            symbol="AAPL",
            action="BUY",
            quantity=Decimal("100"),
            entry_date="2026-06-15",
            exec_ids=frozenset({"EXEC-12345"}),
        ),
    )

    ledger_rows = match_flex_statement(statement, trades)
    # Should create: REGULATORY_FEE and EXCHANGE_FEE
    assert len(ledger_rows) == 2
    categories = {row.category for row in ledger_rows}
    assert categories == {"REGULATORY_FEE", "EXCHANGE_FEE"}
    for row in ledger_rows:
        assert row.trade_group_id == "TG-AAPL-01"
        assert row.amount < Decimal("0.0")
        assert row.status == "SETTLED"


def test_match_trade_fees_fallback_matching() -> None:
    """Verifiziert das Fallback-Matching über order_reference und Symbol/Datum."""
    from app.services.flex_query.models import FlexTradeFeeRecord

    statement = ParsedFlexStatement(
        account_id="DU123456",
        from_date="2026-06-01",
        to_date="2026-06-30",
        when_generated="20260630;100000",
        trade_fees=(
            # Match via order_reference
            FlexTradeFeeRecord(
                account_id="DU123456",
                symbol="MSFT",
                date_time="2026-06-15",
                buy_sell="BUY",
                quantity=Decimal("50"),
                price=Decimal("300.00"),
                total_commission=Decimal("1.50"),
                broker_execution_charge=Decimal("0.0"),
                broker_clearing_charge=Decimal("0.0"),
                third_party_execution_charge=Decimal("0.25"),
                third_party_clearing_charge=Decimal("0.0"),
                third_party_regulatory_charge=Decimal("0.0"),
                other=Decimal("0.0"),
                currency="USD",
                order_reference="999001",
            ),
            # Match via Symbol/Datum Fallback
            FlexTradeFeeRecord(
                account_id="DU123456",
                symbol="TSLA",
                date_time="2026-06-20",
                buy_sell="SELL",
                quantity=Decimal("20"),
                price=Decimal("200.00"),
                total_commission=Decimal("1.00"),
                broker_execution_charge=Decimal("0.0"),
                broker_clearing_charge=Decimal("0.0"),
                third_party_execution_charge=Decimal("0.0"),
                third_party_clearing_charge=Decimal("0.0"),
                third_party_regulatory_charge=Decimal("0.15"),
                other=Decimal("0.0"),
                currency="USD",
                order_reference="INVALID_NON_INT",
            ),
        ),
    )

    trades = (
        HistoricalTradeContext(
            account_id="DU123456",
            trade_group_id="TG-MSFT-01",
            symbol="MSFT",
            action="BUY",
            quantity=Decimal("50"),
            entry_date="2026-06-15",
            order_ids=frozenset({999001}),
        ),
        HistoricalTradeContext(
            account_id="DU123456",
            trade_group_id="TG-TSLA-01",
            symbol="TSLA",
            action="SELL",
            quantity=Decimal("20"),
            entry_date="2026-06-20",
        ),
    )

    ledger_rows = match_flex_statement(statement, trades)
    assert len(ledger_rows) == 2
    msft_fee = next(r for r in ledger_rows if r.symbol == "MSFT")
    tsla_fee = next(r for r in ledger_rows if r.symbol == "TSLA")
    assert msft_fee.trade_group_id == "TG-MSFT-01"
    assert msft_fee.category == "EXCHANGE_FEE"
    assert tsla_fee.trade_group_id == "TG-TSLA-01"
    assert tsla_fee.category == "REGULATORY_FEE"


def test_match_flex_trades_detects_missing_exit_and_already_reconciled() -> None:
    """Verifiziert die Erkennung von fehlenden Exit-Trades und bereits verbuchten Fills."""
    trade_liq = FlexTradeRecord(
        account_id="U12345",
        symbol="MNQ",
        date_time="2026-09-28;215900",
        buy_sell="SELL",
        quantity=Decimal("1"),
        price=Decimal("20050.25"),
        total_commission=Decimal("-0.85"),
        sec_type="FUT",
        trade_id="EXEC_LIQ_1",
        notes="L",
    )
    trade_existing = FlexTradeRecord(
        account_id="U12345",
        symbol="AAPL",
        date_time="2026-09-28;160000",
        buy_sell="SELL",
        quantity=Decimal("10"),
        price=Decimal("230.50"),
        total_commission=Decimal("-1.00"),
        trade_id="EXEC_AAPL_OLD",
    )

    historical_trades = (
        HistoricalTradeContext(
            account_id="U12345",
            trade_group_id="1570_TwoPercent_QQQ",
            symbol="MNQU6",
            action="BUY",
            quantity=Decimal("1"),
            entry_date="2026-09-28",
            parent_order_id=100,
            sec_type="FUT",
            is_settled=False,
        ),
    )
    existing_exec_ids = {"EXEC_AAPL_OLD"}

    actions = match_flex_trades(
        trades=[trade_liq, trade_existing],
        historical_trades=historical_trades,
        existing_exec_ids=existing_exec_ids,
    )

    assert len(actions) == 2
    assert actions[0].action_type == "MISSING_EXIT"
    assert actions[0].matched_trade_group_id == "1570_TwoPercent_QQQ"
    assert actions[0].matched_parent_order_id == 100
    assert actions[1].action_type == "ALREADY_RECONCILED"


def test_match_flex_trades_ignores_cancelled_or_error_entry_contexts() -> None:
    """Verifies that an incoming sell trade is NOT matched as MISSING_EXIT to a cancelled or error order."""
    trade_sell = FlexTradeRecord(
        account_id="U12345",
        symbol="STX",
        date_time="2026-09-29;094342",
        buy_sell="SELL",
        quantity=Decimal("14"),
        price=Decimal("896.74"),
        total_commission=Decimal("-1.00"),
        sec_type="STK",
        trade_id="EXEC_STX_SELL",
    )

    cancelled_context = HistoricalTradeContext(
        account_id="U12345",
        trade_group_id="950_DipBuyer_STX",
        symbol="STX",
        action="BUY",
        quantity=Decimal("7"),
        entry_date="2026-06-29",
        parent_order_id=480,
        sec_type="STK",
        is_settled=False,
        status="Cancelled",
    )

    actions = match_flex_trades(
        trades=[trade_sell],
        historical_trades=[cancelled_context],
        existing_exec_ids=set(),
    )

    assert len(actions) == 1
    assert actions[0].action_type == "UNMATCHED"
    assert actions[0].matched_trade_group_id is None
