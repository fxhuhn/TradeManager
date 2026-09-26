"""
XML-Parser für Interactive Brokers Flex Query Statements.

Extrahiert typensicher Datensätze für unbundled Gebühren, Hard-to-Borrow Zinsen,
Dividenden-Accruals und Cash-Transaktionen. Ignoriert unbekannte XML-Knoten tolerant
zur Gewährleistung maximaler Schema-Stabilität.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any

import defusedxml.ElementTree as DefusedET

from app.services.flex_query.models import (
    FlexBorrowFeeRecord,
    FlexCashTransactionRecord,
    FlexDividendAccrualRecord,
    FlexTradeFeeRecord,
    ParsedFlexStatement,
)


def to_decimal(value: Any, default: Decimal = Decimal("0.0")) -> Decimal:
    """Konvertiert einen String oder numerischen Wert deterministisch in Decimal."""
    if value is None:
        return default
    value_str = str(value).strip()
    if not value_str:
        return default
    try:
        return Decimal(value_str)
    except (InvalidOperation, ValueError):
        return default


def parse_ibkr_date(date_string: str) -> str:
    """Normalisiert IBKR Datumsstrings (YYYYMMDD oder YYYYMMDD;HHMMSS) zu ISO-Format YYYY-MM-DD."""
    cleaned = date_string.strip().split(";")[0]
    if len(cleaned) == 8 and cleaned.isdigit():
        return f"{cleaned[0:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    return cleaned


def generate_external_reference_id(*parts: Any) -> str:
    """Erzeugt einen eindeutigen, deterministischen SHA-256 Hash als Idempotenz-Schlüssel."""
    content = "|".join(str(part).strip() for part in parts)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


def parse_flex_xml(xml_content: str | bytes) -> ParsedFlexStatement:
    """Parst ein IBKR Flex Statement XML und liefert ein ParsedFlexStatement zurück.

    Raises:
        ValueError: Wenn das XML strukturell ungültig ist oder kein FlexStatement enthält.
    """
    try:
        if isinstance(xml_content, str):
            root = DefusedET.fromstring(xml_content.encode("utf-8"))
        else:
            root = DefusedET.fromstring(xml_content)
    except Exception as parse_error:
        raise ValueError(
            f"Failed to parse Flex Query XML: {parse_error}"
        ) from parse_error

    statement_element = root.find(".//FlexStatement")
    if statement_element is None:
        raise ValueError("XML does not contain a valid <FlexStatement> node")

    account_id = statement_element.attrib.get("accountId", "").strip()
    from_date = parse_ibkr_date(statement_element.attrib.get("fromDate", ""))
    to_date = parse_ibkr_date(statement_element.attrib.get("toDate", ""))
    when_generated = statement_element.attrib.get("whenGenerated", "").strip()

    trade_fees: list[FlexTradeFeeRecord] = []
    borrow_fees: list[FlexBorrowFeeRecord] = []
    dividend_accruals: list[FlexDividendAccrualRecord] = []
    cash_transactions: list[FlexCashTransactionRecord] = []

    # 1. Commission Details / Trades parsing
    for trade_node in statement_element.findall(
        ".//CommissionDetail"
    ) + statement_element.findall(".//Trade"):
        record_account = trade_node.attrib.get("accountId", account_id).strip()
        trade_fees.append(
            FlexTradeFeeRecord(
                account_id=record_account,
                symbol=trade_node.attrib.get("symbol", "").strip(),
                date_time=trade_node.attrib.get("dateTime", "").strip(),
                buy_sell=trade_node.attrib.get("buySell", "").strip().upper(),
                quantity=to_decimal(trade_node.attrib.get("quantity")),
                price=to_decimal(
                    trade_node.attrib.get("price")
                    or trade_node.attrib.get("tradePrice")
                ),
                total_commission=to_decimal(
                    trade_node.attrib.get("totalCommission")
                    or trade_node.attrib.get("ibCommission")
                ),
                broker_execution_charge=to_decimal(
                    trade_node.attrib.get("brokerExecutionCharge")
                ),
                broker_clearing_charge=to_decimal(
                    trade_node.attrib.get("brokerClearingCharge")
                ),
                third_party_execution_charge=to_decimal(
                    trade_node.attrib.get("thirdPartyExecutionCharge")
                ),
                third_party_clearing_charge=to_decimal(
                    trade_node.attrib.get("thirdPartyClearingCharge")
                ),
                third_party_regulatory_charge=to_decimal(
                    trade_node.attrib.get("thirdPartyRegulatoryCharge")
                ),
                other=to_decimal(trade_node.attrib.get("other")),
                currency=trade_node.attrib.get("currency", "USD").strip(),
                fx_rate_to_base=to_decimal(
                    trade_node.attrib.get("fxRateToBase"), Decimal("1.0")
                ),
                trade_id=trade_node.attrib.get("tradeID")
                or trade_node.attrib.get("ibExecutionID"),
                order_reference=trade_node.attrib.get("orderReference")
                or trade_node.attrib.get("ibOrderID"),
                exchange=trade_node.attrib.get("exchange"),
            )
        )

    # 2. HardToBorrowDetails parsing
    for htb_node in statement_element.findall(
        ".//HardToBorrowDetail"
    ) + statement_element.findall(".//BorrowFeeDetail"):
        record_account = htb_node.attrib.get("accountId", account_id).strip()
        raw_date = htb_node.attrib.get("valueDate") or htb_node.attrib.get("date", "")
        borrow_fees.append(
            FlexBorrowFeeRecord(
                account_id=record_account,
                value_date=parse_ibkr_date(raw_date),
                symbol=htb_node.attrib.get("symbol", "").strip().upper(),
                description=htb_node.attrib.get("description", "").strip(),
                quantity=to_decimal(htb_node.attrib.get("quantity")),
                borrow_fee_rate=to_decimal(
                    htb_node.attrib.get("borrowFeeRate")
                    or htb_node.attrib.get("feeRate")
                ),
                borrow_fee=to_decimal(
                    htb_node.attrib.get("borrowFee") or htb_node.attrib.get("fee")
                ),
                currency=htb_node.attrib.get("currency", "USD").strip(),
                fx_rate_to_base=to_decimal(
                    htb_node.attrib.get("fxRateToBase"), Decimal("1.0")
                ),
            )
        )

    # 3. OpenDividendAccruals parsing
    for accrual_node in statement_element.findall(".//OpenDividendAccrual"):
        record_account = accrual_node.attrib.get("accountId", account_id).strip()
        dividend_accruals.append(
            FlexDividendAccrualRecord(
                account_id=record_account,
                symbol=accrual_node.attrib.get("symbol", "").strip().upper(),
                description=accrual_node.attrib.get("description", "").strip(),
                ex_date=parse_ibkr_date(accrual_node.attrib.get("exDate", "")),
                pay_date=parse_ibkr_date(accrual_node.attrib.get("payDate", "")),
                quantity=to_decimal(accrual_node.attrib.get("quantity")),
                gross_rate=to_decimal(accrual_node.attrib.get("grossRate")),
                gross_amount=to_decimal(accrual_node.attrib.get("grossAmount")),
                tax=to_decimal(accrual_node.attrib.get("tax")),
                fee=to_decimal(accrual_node.attrib.get("fee")),
                net_amount=to_decimal(accrual_node.attrib.get("netAmount")),
                currency=accrual_node.attrib.get("currency", "USD").strip(),
                fx_rate_to_base=to_decimal(
                    accrual_node.attrib.get("fxRateToBase"), Decimal("1.0")
                ),
            )
        )

    # 4. CashTransactions parsing
    for cash_node in statement_element.findall(".//CashTransaction"):
        record_account = cash_node.attrib.get("accountId", account_id).strip()
        cash_transactions.append(
            FlexCashTransactionRecord(
                account_id=record_account,
                date_time=cash_node.attrib.get("dateTime", "").strip(),
                transaction_type=cash_node.attrib.get("type", "").strip(),
                description=cash_node.attrib.get("description", "").strip(),
                symbol=cash_node.attrib.get("symbol", "").strip().upper(),
                amount=to_decimal(cash_node.attrib.get("amount")),
                currency=cash_node.attrib.get("currency", "USD").strip(),
                fx_rate_to_base=to_decimal(
                    cash_node.attrib.get("fxRateToBase"), Decimal("1.0")
                ),
                trade_id=cash_node.attrib.get("tradeID"),
            )
        )

    return ParsedFlexStatement(
        account_id=account_id,
        from_date=from_date,
        to_date=to_date,
        when_generated=when_generated,
        trade_fees=tuple(trade_fees),
        borrow_fees=tuple(borrow_fees),
        dividend_accruals=tuple(dividend_accruals),
        cash_transactions=tuple(cash_transactions),
    )


# Alias für semantische Einheitlichkeit
parse_flex_statement = parse_flex_xml
