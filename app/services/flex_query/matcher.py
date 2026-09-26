"""
Zuordnungs- und Allokations-Engine für Flex Query Datensätze (Functional Core).

Matcht geparste Flex-Statement-Einträge deterministisch gegen lokale Trade-Kontexte
(nach Symbol, Ausführungs-IDs und Haltedauer) oder allokiert sie auf Account-Ebene.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.core.models import CashLedgerRow
from app.services.flex_query.models import ParsedFlexStatement
from app.services.flex_query.parser import (
    generate_external_reference_id,
    parse_ibkr_date,
)


@dataclass(frozen=True)
class HistoricalTradeContext:
    """Kapselt den historischen Kontext einer lokalen Trade-Gruppe für das Matching."""

    account_id: str
    trade_group_id: str
    symbol: str
    action: str  # BUY (Long) oder SELL (Short)
    quantity: Decimal = Decimal("0.0")
    entry_date: str = ""  # YYYY-MM-DD
    exit_date: str | None = None  # YYYY-MM-DD (None wenn noch offen)
    order_ids: tuple[int, ...] = ()
    perm_ids: tuple[int, ...] = ()
    exec_ids: tuple[str, ...] = ()


def _find_matching_trade(
    symbol: str,
    target_date: str,
    action: str,
    trades: Sequence[HistoricalTradeContext],
) -> HistoricalTradeContext | None:
    """Findet eine Trade-Gruppe, die das gegebene Symbol am target_date im angegebenen Modus hielt."""
    clean_symbol = symbol.strip().upper()
    for trade in trades:
        if trade.symbol.strip().upper() != clean_symbol:
            continue
        if trade.action.strip().upper() != action.strip().upper():
            continue
        # Zeitfenster prüfen: entry_date <= target_date <= exit_date
        if trade.entry_date <= target_date:
            if trade.exit_date is None or target_date <= trade.exit_date:
                return trade
    return None


def _is_capital_transfer(tx_type: str, desc_lower: str) -> bool:
    """Ermittelt, ob es sich um eine reine Eigenkapital-Einlage oder -Entnahme handelt.

    Externe Einzahlungen und Auszahlungen (z. B. Electronic Fund Transfers, Deposits,
    Withdrawals) sind keine Handelskosten oder Betriebsausgaben und dürfen nicht im
    cash_ledger verbucht werden.
    """
    transfer_keywords = (
        "deposit",
        "withdrawal",
        "electronic fund transfer",
        "cash receipts",
        "fund transfer",
        "capital transfer",
        "transfers",
    )
    return any(keyword in tx_type for keyword in transfer_keywords) or any(
        keyword in desc_lower for keyword in transfer_keywords
    )


def match_flex_statement(
    statement: ParsedFlexStatement,
    historical_trades: Sequence[HistoricalTradeContext],
) -> list[CashLedgerRow]:
    """Wandelt ein ParsedFlexStatement in normalisierte CashLedgerRow-Einträge um.

    Args:
        statement: Das geparste Flex Query Statement.
        historical_trades: Liste aller bekannten historischen und aktiven Trade-Kontexte.

    Returns:
        list[CashLedgerRow]: Liste normalisierter Buchungssätze für cash_ledger.
    """
    ledger_rows: list[CashLedgerRow] = []

    # 1. HardToBorrowDetails zuordnen
    for htb in statement.borrow_fees:
        matched_trade = _find_matching_trade(
            symbol=htb.symbol,
            target_date=htb.value_date,
            action="SELL",  # Nur Short-Trades zahlen Borrow-Fees
            trades=historical_trades,
        )
        trade_group_id = matched_trade.trade_group_id if matched_trade else None
        ref_id = generate_external_reference_id(
            htb.account_id,
            htb.value_date,
            htb.symbol,
            "BORROW_FEE",
            htb.borrow_fee,
        )
        ledger_rows.append(
            CashLedgerRow(
                account_id=htb.account_id,
                trade_group_id=trade_group_id,
                symbol=htb.symbol,
                category="BORROW_FEE",
                description=htb.description or f"Hard-to-Borrow fee for {htb.symbol}",
                amount=htb.borrow_fee,
                currency=htb.currency,
                fx_rate_to_base=htb.fx_rate_to_base,
                amount_in_base=htb.borrow_fee * htb.fx_rate_to_base,
                status="SETTLED",
                effective_date=htb.value_date,
                settled_date=htb.value_date,
                source="FLEX_QUERY",
                external_reference_id=ref_id,
            )
        )

    # 2. OpenDividendAccruals zuordnen (Status: PENDING)
    for accrual in statement.dividend_accruals:
        matched_trade = _find_matching_trade(
            symbol=accrual.symbol,
            target_date=accrual.ex_date,
            action="BUY",  # Dividendenansprüche entstehen bei Long-Positionen
            trades=historical_trades,
        )
        trade_group_id = matched_trade.trade_group_id if matched_trade else None

        # Brutto-Dividende buchen
        ref_gross = generate_external_reference_id(
            accrual.account_id,
            accrual.ex_date,
            accrual.symbol,
            "DIVIDEND_ACCRUAL_GROSS",
            accrual.gross_amount,
        )
        ledger_rows.append(
            CashLedgerRow(
                account_id=accrual.account_id,
                trade_group_id=trade_group_id,
                symbol=accrual.symbol,
                category="DIVIDEND",
                description=f"Accrued gross dividend for {accrual.symbol} (Ex: {accrual.ex_date})",
                amount=accrual.gross_amount,
                currency=accrual.currency,
                fx_rate_to_base=accrual.fx_rate_to_base,
                amount_in_base=accrual.gross_amount * accrual.fx_rate_to_base,
                status="PENDING",
                effective_date=accrual.ex_date,
                settled_date=accrual.pay_date,
                source="FLEX_QUERY",
                external_reference_id=ref_gross,
            )
        )

        # Quellensteuer als Gegenposten buchen (falls > 0)
        if accrual.tax > Decimal("0.0"):
            ref_tax = generate_external_reference_id(
                accrual.account_id,
                accrual.ex_date,
                accrual.symbol,
                "DIVIDEND_ACCRUAL_TAX",
                accrual.tax,
            )
            tax_amount = -accrual.tax
            ledger_rows.append(
                CashLedgerRow(
                    account_id=accrual.account_id,
                    trade_group_id=trade_group_id,
                    symbol=accrual.symbol,
                    category="WITHHOLDING_TAX",
                    description=f"Accrued dividend withholding tax for {accrual.symbol}",
                    amount=tax_amount,
                    currency=accrual.currency,
                    fx_rate_to_base=accrual.fx_rate_to_base,
                    amount_in_base=tax_amount * accrual.fx_rate_to_base,
                    status="PENDING",
                    effective_date=accrual.ex_date,
                    settled_date=accrual.pay_date,
                    source="FLEX_QUERY",
                    external_reference_id=ref_tax,
                )
            )

    # 3. CashTransactions zuordnen
    for tx in statement.cash_transactions:
        clean_date = parse_ibkr_date(tx.date_time)
        tx_type = tx.transaction_type.strip().lower()
        desc_lower = tx.description.lower()
        symbol = tx.symbol if tx.symbol else None

        # Externe Kapitaltransfers (Einlagen/Entnahmen) strikt ignorieren
        if _is_capital_transfer(tx_type, desc_lower):
            continue

        trade_group_id = None
        category: str | None = None

        if "dividend" in tx_type:
            category = "DIVIDEND"
            if symbol:
                matched = _find_matching_trade(
                    symbol, clean_date, "BUY", historical_trades
                )
                trade_group_id = matched.trade_group_id if matched else None
        elif "withholding tax" in tx_type:
            category = "WITHHOLDING_TAX"
            if symbol:
                matched = _find_matching_trade(
                    symbol, clean_date, "BUY", historical_trades
                )
                trade_group_id = matched.trade_group_id if matched else None
        elif "payment in lieu" in tx_type or "payment in lieu" in desc_lower:
            category = "PAYMENT_IN_LIEU"
            if symbol:
                matched = _find_matching_trade(
                    symbol, clean_date, "SELL", historical_trades
                )
                trade_group_id = matched.trade_group_id if matched else None
        elif "broker interest paid" in tx_type:
            category = "INTEREST_DEBIT"
        elif "broker interest received" in tx_type:
            if "syep" in desc_lower or "managed securities" in desc_lower:
                category = "SYEP_INCOME"
            else:
                category = "INTEREST_CREDIT"
        elif "other fees" in tx_type or "fee" in tx_type:
            if (
                "cme" in desc_lower
                or "globex" in desc_lower
                or "market data" in desc_lower
            ):
                category = "MARKET_DATA"
            else:
                category = "OTHER_FEE"

        # Transaktionen ohne bekannten Gebühren- oder Ertrags-Typ überspringen
        if category is None:
            continue

        ref_id = generate_external_reference_id(
            tx.account_id,
            tx.date_time,
            tx.transaction_type,
            tx.symbol,
            tx.amount,
            tx.currency,
            tx.fx_rate_to_base,
            tx.description,
        )

        ledger_rows.append(
            CashLedgerRow(
                account_id=tx.account_id,
                trade_group_id=trade_group_id,
                symbol=symbol,
                category=category,
                description=tx.description,
                amount=tx.amount,
                currency=tx.currency,
                fx_rate_to_base=tx.fx_rate_to_base,
                amount_in_base=tx.amount * tx.fx_rate_to_base,
                status="SETTLED",
                effective_date=clean_date,
                settled_date=clean_date,
                source="FLEX_QUERY",
                external_reference_id=ref_id,
            )
        )

    # 4. Commission Details / Trade Fees zuordnen
    for fee_record in statement.trade_fees:
        clean_date = parse_ibkr_date(fee_record.date_time)
        matched_trade_group: str | None = None

        # Suche nach übereinstimmender Order- oder Exec-ID
        for trade in historical_trades:
            if fee_record.trade_id and fee_record.trade_id in trade.exec_ids:
                matched_trade_group = trade.trade_group_id
                break
            if fee_record.order_reference:
                try:
                    ref_int = int(fee_record.order_reference)
                    if ref_int in trade.order_ids or ref_int in trade.perm_ids:
                        matched_trade_group = trade.trade_group_id
                        break
                except ValueError:
                    pass

        # Falls keine direkte ID gematcht wurde, Fallback über Symbol und Datum
        if not matched_trade_group and fee_record.symbol:
            matched = _find_matching_trade(
                symbol=fee_record.symbol,
                target_date=clean_date,
                action=fee_record.buy_sell,
                trades=historical_trades,
            )
            matched_trade_group = matched.trade_group_id if matched else None

        # Regulatorische Gebühren buchen
        if fee_record.third_party_regulatory_charge != Decimal("0.0"):
            ref_reg = generate_external_reference_id(
                fee_record.account_id,
                fee_record.date_time,
                fee_record.symbol,
                "REGULATORY_FEE",
                fee_record.third_party_regulatory_charge,
            )
            reg_amount = -abs(fee_record.third_party_regulatory_charge)
            ledger_rows.append(
                CashLedgerRow(
                    account_id=fee_record.account_id,
                    trade_group_id=matched_trade_group,
                    symbol=fee_record.symbol,
                    category="REGULATORY_FEE",
                    description=f"Regulatory fee for {fee_record.symbol}",
                    amount=reg_amount,
                    currency=fee_record.currency,
                    fx_rate_to_base=fee_record.fx_rate_to_base,
                    amount_in_base=reg_amount * fee_record.fx_rate_to_base,
                    status="SETTLED",
                    effective_date=clean_date,
                    settled_date=clean_date,
                    source="FLEX_QUERY",
                    external_reference_id=ref_reg,
                )
            )

        # Börsengebühren buchen
        if fee_record.third_party_execution_charge != Decimal("0.0"):
            ref_exch = generate_external_reference_id(
                fee_record.account_id,
                fee_record.date_time,
                fee_record.symbol,
                "EXCHANGE_FEE",
                fee_record.third_party_execution_charge,
            )
            exch_amount = -abs(fee_record.third_party_execution_charge)
            ledger_rows.append(
                CashLedgerRow(
                    account_id=fee_record.account_id,
                    trade_group_id=matched_trade_group,
                    symbol=fee_record.symbol,
                    category="EXCHANGE_FEE",
                    description=f"Exchange fee for {fee_record.symbol}",
                    amount=exch_amount,
                    currency=fee_record.currency,
                    fx_rate_to_base=fee_record.fx_rate_to_base,
                    amount_in_base=exch_amount * fee_record.fx_rate_to_base,
                    status="SETTLED",
                    effective_date=clean_date,
                    settled_date=clean_date,
                    source="FLEX_QUERY",
                    external_reference_id=ref_exch,
                )
            )

    return ledger_rows
