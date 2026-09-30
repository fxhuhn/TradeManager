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
    match_flex_trades,
)
from app.services.flex_query.models import FlexTradeRecord
from app.services.flex_query.parser import (
    parse_flex_statement,
    parse_ibkr_date,
)
from app.trading.order_builder import normalize_routing_exchange
from app.trading.settlement import settle_trade_group

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
    reconciled_trades_count: int = 0
    settled_trades_count: int = 0


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
                MAX(DATE(ts.settled_at)) AS exit_date,
                MIN(CASE WHEN o.bracket_role = 'ENTRY' THEN o.order_id ELSE NULL END) AS parent_order_id,
                MAX(o.sec_type) AS sec_type,
                MAX(o.strategy_name) AS strategy_name,
                (ts.trade_group_id IS NOT NULL) AS is_settled,
                GROUP_CONCAT(DISTINCT e.exec_id) AS exec_ids_str,
                COALESCE(MAX(CASE WHEN o.bracket_role = 'ENTRY' THEN o.status ELSE NULL END), 'Filled') AS entry_status
            FROM orders o
            LEFT JOIN executions e ON o.order_id = e.order_id
            LEFT JOIN trades_settlement ts
                ON o.account_id = ts.account_id AND o.trade_group_id = ts.trade_group_id
            WHERE (
                (o.bracket_role = 'ENTRY' AND o.status IN ('Filled', 'Submitted', 'PreSubmitted'))
                OR e.exec_id IS NOT NULL
                OR ts.settled_at IS NOT NULL
            )
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
                parent_order_id = int(row[7]) if row[7] is not None else None
                sec_type = str(row[8] or "STK")
                strategy_name = str(row[9]) if row[9] else None
                is_settled = bool(row[10])
                raw_exec_ids = str(row[11] or "")
                exec_ids = tuple(
                    x.strip() for x in raw_exec_ids.split(",") if x.strip()
                )
                entry_status = str(row[12] or "Filled")

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
                            exec_ids=exec_ids,
                            parent_order_id=parent_order_id,
                            sec_type=sec_type,
                            strategy_name=strategy_name,
                            is_settled=is_settled,
                            status=entry_status,
                        )
                    )
        return tuple(historical_trades)

    async def reconcile_trades(
        self,
        trades: tuple[FlexTradeRecord, ...],
        historical_trades: tuple[HistoricalTradeContext, ...],
    ) -> tuple[int, int]:
        """Gleicht FlexTradeRecords gegen die lokale Datenbank ab und bucht fehlende Fills."""
        if not trades:
            return 0, 0

        existing_exec_ids: set[str] = set()
        async with self._db.execute("SELECT exec_id FROM executions") as cursor:
            async for row in cursor:
                if row[0]:
                    existing_exec_ids.add(str(row[0]).strip())

        actions = match_flex_trades(
            trades=trades,
            historical_trades=historical_trades,
            existing_exec_ids=existing_exec_ids,
        )

        reconciled_trades_count = 0
        settled_trades_count = 0

        for action in actions:
            if action.action_type != "MISSING_EXIT":
                continue

            trade = action.trade
            trade_group_id = action.matched_trade_group_id
            if not trade_group_id:
                continue

            logger.warning(
                "Flex Query Reconciliation: Fehlende EXIT-Ausführung erkannt. Buche Trade nach.",
                extra={
                    "trade_group_id": trade_group_id,
                    "symbol": trade.symbol,
                    "exec_id": trade.trade_id,
                    "price": float(trade.price),
                    "qty": float(trade.quantity),
                },
            )

            async with transaction(self._db):
                # 1. Offene Child-Orders (SL, TP, EXIT) stornieren
                await self._db.execute(
                    """
                    UPDATE orders
                    SET status = 'Cancelled'
                    WHERE trade_group_id = ? AND bracket_role IN ('SL', 'TP', 'EXIT')
                      AND status IN ('Created', 'Submitted', 'PreSubmitted')
                    """,
                    (trade_group_id,),
                )

                # 2. Prüfen, ob bereits eine EXIT-Order existiert
                query_exit = "SELECT order_id FROM orders WHERE trade_group_id = ? AND bracket_role = 'EXIT' LIMIT 1"
                exit_order_id: int | None = None
                async with self._db.execute(query_exit, (trade_group_id,)) as cursor:
                    exit_row = await cursor.fetchone()
                    if exit_row:
                        exit_order_id = int(exit_row[0])
                        await self._db.execute(
                            "UPDATE orders SET status = 'Filled' WHERE order_id = ?",
                            (exit_order_id,),
                        )

                # Falls keine EXIT-Order existiert, synthetische EXIT-Order anlegen
                if exit_order_id is None:
                    exit_order_id = -1 * int(
                        abs(hash(f"FLEX_EXIT_{trade_group_id}_{trade.account_id}"))
                        % 100000000
                    )
                    await self._db.execute(
                        """
                        INSERT INTO orders (
                            order_id, perm_id, parent_id, trade_group_id, account_id,
                            bracket_role, symbol, sec_type, exchange, action, quantity,
                            order_type, target_price, tif, strategy_name, status, transmitted_at
                        ) VALUES (?, NULL, ?, ?, ?, 'EXIT', ?, ?, ?, ?, ?, 'MKT', ?, 'DAY', ?, 'Filled', CURRENT_TIMESTAMP)
                        """,
                        (
                            exit_order_id,
                            action.matched_parent_order_id,
                            trade_group_id,
                            trade.account_id,
                            action.symbol or trade.symbol,
                            action.sec_type,
                            normalize_routing_exchange(
                                action.sec_type or trade.sec_type,
                                trade.exchange,
                            ),
                            trade.buy_sell,
                            int(trade.quantity),
                            str(trade.price),
                            action.strategy_name,
                        ),
                    )

                # 3. Execution eintragen
                exec_id = (
                    trade.trade_id or f"FLEX_EXEC_{trade_group_id}_{exit_order_id}"
                )
                await self._db.execute(
                    """
                    INSERT INTO executions (
                        exec_id, order_id, price, qty, commission, currency, executed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (exec_id) DO NOTHING
                    """,
                    (
                        exec_id,
                        exit_order_id,
                        str(trade.price),
                        str(trade.quantity),
                        str(abs(trade.total_commission)),
                        trade.currency,
                        parse_ibkr_date(trade.date_time),
                    ),
                )
                reconciled_trades_count += 1
                existing_exec_ids.add(exec_id)

            # 4. Settlement anstoßen
            settled = await settle_trade_group(
                self._db,
                trade_group_id=trade_group_id,
                account_id=trade.account_id,
                notifier=self._notifier,
            )
            if settled:
                settled_trades_count += 1

        return reconciled_trades_count, settled_trades_count

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

        # Trades abgleichen (fehlende Ausführungen nachbuchen & abrechnen)
        reconciled_trades_count, settled_trades_count = await self.reconcile_trades(
            parsed.trades, historical_trades
        )

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
            reconciled_trades_count=reconciled_trades_count,
            settled_trades_count=settled_trades_count,
        )

        logger.info(
            "Flex Query Reconciliation abgeschlossen: %d neue Einträge verbucht (%d Duplikate übersprungen). Trade-Allokationen: %d (%.2f Base), Account-Ausgaben: %d (%.2f Base), Trades abgeglichen: %d, abgewickelt: %d",
            report.inserted_count,
            report.skipped_duplicate_count,
            report.allocated_to_trades_count,
            report.total_trade_adjustments_base,
            report.account_level_count,
            report.total_account_expenses_base,
            report.reconciled_trades_count,
            report.settled_trades_count,
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
