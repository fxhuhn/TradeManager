"""
Datenmodelle für aus IBKR Flex Queries extrahierte Rohdaten.

Definiert typsichere, unveränderliche Datenstrukturen für Einzelberichte
aus den Sektionen Commission Details, HardToBorrowDetails, OpenDividendAccruals
und CashTransactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FlexTradeFeeRecord:
    """Repräsentiert die unbundled Gebühren einer Order/Ausführung aus Commission Details."""

    account_id: str
    symbol: str
    date_time: str
    buy_sell: str
    quantity: Decimal
    price: Decimal
    total_commission: Decimal
    broker_execution_charge: Decimal
    broker_clearing_charge: Decimal
    third_party_execution_charge: Decimal
    third_party_clearing_charge: Decimal
    third_party_regulatory_charge: Decimal
    other: Decimal
    currency: str
    fx_rate_to_base: Decimal = Decimal("1.0")
    trade_id: str | None = None
    order_reference: str | None = None
    exchange: str | None = None


@dataclass(frozen=True)
class FlexBorrowFeeRecord:
    """Repräsentiert tägliche Hard-to-Borrow Leihgebühren für Short-Positionen."""

    account_id: str
    value_date: str
    symbol: str
    description: str
    quantity: Decimal
    borrow_fee_rate: Decimal
    borrow_fee: Decimal
    currency: str
    fx_rate_to_base: Decimal = Decimal("1.0")


@dataclass(frozen=True)
class FlexDividendAccrualRecord:
    """Repräsentiert offene, noch nicht ausgezahlte Dividendenansprüche (Pending)."""

    account_id: str
    symbol: str
    description: str
    ex_date: str
    pay_date: str
    quantity: Decimal
    gross_rate: Decimal
    gross_amount: Decimal
    tax: Decimal
    fee: Decimal
    net_amount: Decimal
    currency: str
    fx_rate_to_base: Decimal = Decimal("1.0")


@dataclass(frozen=True)
class FlexCashTransactionRecord:
    """Repräsentiert tatsächliche Geldbewegungen (Dividenden, Quellensteuern, Zinsen, Spesen)."""

    account_id: str
    date_time: str
    transaction_type: str
    description: str
    symbol: str
    amount: Decimal
    currency: str
    fx_rate_to_base: Decimal = Decimal("1.0")
    trade_id: str | None = None


@dataclass(frozen=True)
class ParsedFlexStatement:
    """Kapselt den gesamten extrahierten Inhalt einer Flex-Statement-XML-Antwort."""

    account_id: str
    from_date: str
    to_date: str
    when_generated: str
    trade_fees: tuple[FlexTradeFeeRecord, ...] = ()
    borrow_fees: tuple[FlexBorrowFeeRecord, ...] = ()
    dividend_accruals: tuple[FlexDividendAccrualRecord, ...] = ()
    cash_transactions: tuple[FlexCashTransactionRecord, ...] = ()
