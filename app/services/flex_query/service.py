"""
Service zur Orchestrierung des Abgleichs (Reconciliation) von IBKR Flex Statements
mit der lokalen Transaktions- und Positionsdatenbank.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.db import transaction
from app.services.flex_query.matcher import (
    HistoricalTradeContext,
    match_flex_statement,
)
from app.services.flex_query.parser import parse_flex_statement

if TYPE_CHECKING:
    import aiosqlite

    from app.core.config import FlexQueryConfig
    from app.services.flex_query.client import FlexWebServiceClient
    from app.services.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationReport:
    """Zusammenfassung des Flex-Query-Reconciliation-Laufs."""

    account_id: str
    from_date: str
    to_date: str
    total_parsed: int
    inserted_count: int
    skipped_duplicate_count: int
    allocated_to_trades_count: int
    account_level_count: int
    total_trade_adjustments_base: Decimal
    total_account_expenses_base: Decimal


class FlexReconciliationService:
    """Imperative Shell: Verbindet SQLite DB, Web Client, Parser und Matcher."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        config: FlexQueryConfig,
        client: FlexWebServiceClient | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._client = client
        self._notifier = notifier

    async def fetch_historical_trades(self) -> tuple[HistoricalTradeContext, ...]:
        """Lädt alle bekannten Trade-Gruppen aus der Datenbank zur Zuordnung.

        Sucht nach ENTRY-Orders und aggregiert das minimale Ausführungsdatum (Entry)
        sowie das optionale Settlement-Datum (Exit).
        """
        query = """
            SELECT
                o.account_id,
                o.trade_group_id,
                o.symbol,
                o.action,
                COALESCE(MAX(o.quantity), 0) AS quantity,
                MIN(DATE(COALESCE(e.executed_at, o.transmitted_at))) AS entry_date,
                MAX(DATE(ts.settled_at)) AS exit_date
            FROM orders o
            LEFT JOIN executions e ON o.order_id = e.order_id
            LEFT JOIN trades_settlement ts
                ON o.account_id = ts.account_id AND o.trade_group_id = ts.trade_group_id
            WHERE o.bracket_role = 'ENTRY'
               OR e.exec_id IS NOT NULL
               OR ts.settled_at IS NOT NULL
            GROUP BY o.account_id, o.trade_group_id, o.symbol, o.action
            HAVING entry_date IS NOT NULL
        """
        historical_trades: list[HistoricalTradeContext] = []
        async with self._db.execute(query) as cursor:
            async for row in cursor:
                account_id = str(row[0] or "")
                trade_group_id = str(row[1] or "")
                symbol = str(row[2] or "")
                action = str(row[3] or "BUY").upper()
                quantity = Decimal(str(row[4] or "0"))
                entry_date = str(row[5] or "")
                exit_date = str(row[6]) if row[6] else None

                if trade_group_id and symbol and entry_date:
                    historical_trades.append(
                        HistoricalTradeContext(
                            account_id=account_id,
                            trade_group_id=trade_group_id,
                            symbol=symbol,
                            action=action,
                            quantity=quantity,
                            entry_date=entry_date,
                            exit_date=exit_date,
                        )
                    )
        return tuple(historical_trades)

    async def reconcile_from_xml(self, xml_content: str) -> ReconciliationReport:
        """Führt den Abgleich für ein übergebenes Flex-Statement-XML durch."""
        parsed = parse_flex_statement(xml_content)
        historical_trades = await self.fetch_historical_trades()
        ledger_rows = match_flex_statement(parsed, historical_trades=historical_trades)

        total_parsed = (
            len(parsed.trade_fees)
            + len(parsed.borrow_fees)
            + len(parsed.dividend_accruals)
            + len(parsed.cash_transactions)
        )

        inserted_count = 0
        skipped_duplicate_count = 0
        allocated_to_trades_count = 0
        account_level_count = 0
        total_trade_adjustments_base = Decimal("0.0")
        total_account_expenses_base = Decimal("0.0")

        insert_sql = """
            INSERT INTO cash_ledger (
                account_id,
                trade_group_id,
                symbol,
                category,
                description,
                amount,
                currency,
                fx_rate_to_base,
                amount_in_base,
                status,
                effective_date,
                settled_date,
                source,
                external_reference_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (account_id, external_reference_id, category, status) DO NOTHING
        """

        async with transaction(self._db):
            for row in ledger_rows:
                cursor = await self._db.execute(
                    insert_sql,
                    (
                        row.account_id,
                        row.trade_group_id,
                        row.symbol,
                        row.category,
                        row.description,
                        str(row.amount),
                        row.currency,
                        str(row.fx_rate_to_base),
                        str(row.amount_in_base),
                        row.status,
                        row.effective_date,
                        row.settled_date,
                        row.source,
                        row.external_reference_id,
                    ),
                )
                if cursor.rowcount > 0:
                    inserted_count += 1
                    if row.trade_group_id is not None:
                        allocated_to_trades_count += 1
                        total_trade_adjustments_base += row.amount_in_base
                    else:
                        account_level_count += 1
                        total_account_expenses_base += row.amount_in_base
                else:
                    skipped_duplicate_count += 1

        report = ReconciliationReport(
            account_id=parsed.account_id,
            from_date=parsed.from_date,
            to_date=parsed.to_date,
            total_parsed=total_parsed,
            inserted_count=inserted_count,
            skipped_duplicate_count=skipped_duplicate_count,
            allocated_to_trades_count=allocated_to_trades_count,
            account_level_count=account_level_count,
            total_trade_adjustments_base=total_trade_adjustments_base,
            total_account_expenses_base=total_account_expenses_base,
        )

        logger.info(
            "Flex Query Reconciliation abgeschlossen: %d neue Einträge verbucht (%d Duplikate übersprungen). Trade-Allokationen: %d (%.2f Base), Account-Ausgaben: %d (%.2f Base)",
            report.inserted_count,
            report.skipped_duplicate_count,
            report.allocated_to_trades_count,
            report.total_trade_adjustments_base,
            report.account_level_count,
            report.total_account_expenses_base,
        )

        return report

    async def sync_and_reconcile(
        self,
        token: str | None = None,
        query_id: str | None = None,
    ) -> ReconciliationReport:
        """Ruft das Statement via Web Service ab und führt den Abgleich durch."""
        if not self._client:
            from app.services.flex_query.client import FlexWebServiceClient

            self._client = FlexWebServiceClient(self._config)

        xml_content = await self._client.fetch_statement(token=token, query_id=query_id)
        report = await self.reconcile_from_xml(xml_content)

        if self._notifier and self._notifier.is_active:
            try:
                await self._notifier.send_flex_reconciliation_summary(report)
            except Exception as notify_err:
                logger.error(
                    "Fehler beim Senden der Flex-Zusammenfassung an Telegram: %s",
                    notify_err,
                )

        return report
