"""
CLI-Tool zur manuellen oder dateibasierten Synchronisation von IBKR Flex Statements.

Ermöglicht den Import von XML-Dateien oder den direkten Abruf via Web Service
inklusive Zuweisung zu historischen Trades in der SQLite-Datenbank.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.core.config import load_config
from app.core.db import get_db, run_migrations
from app.services.flex_query.service import (
    FlexReconciliationService,
    ReconciliationReport,
)


def format_flex_sync_report(report: ReconciliationReport) -> str:
    """Formatiert das Reconciliation-Ergebnis für die Terminal-Ausgabe."""
    lines = [
        "=" * 60,
        "  IBKR FLEX QUERY RECONCILIATION BERICHT",
        "=" * 60,
        f"  Konto:                 {report.account_id}",
        f"  Zeitraum:              {report.from_date} bis {report.to_date}",
        "-" * 60,
        f"  Datensätze analysiert: {report.total_parsed}",
        f"  Neu verbucht:          {report.inserted_count}",
        f"  Ignoriert (Duplikate): {report.skipped_duplicate_count}",
        "-" * 60,
        f"  Trade-Allokationen:    {report.allocated_to_trades_count} Buchungen",
        f"  Summe Trade-Kosten:    $ {report.total_trade_adjustments_base:,.2f}",
        f"  Konto-Nebenkosten:     {report.account_level_count} Buchungen",
        f"  Summe Konto-Kosten:    $ {report.total_account_expenses_base:,.2f}",
        "=" * 60,
    ]
    return "\n".join(lines)


async def run_flex_sync(
    file_path: Path | None = None,
    token: str | None = None,
    query_id: str | None = None,
    notify: bool = False,
    root_path: Path = Path("."),
) -> ReconciliationReport:
    """Führt die Synchronisation entweder via Datei oder Web Service aus."""
    config = load_config(root_path)

    notifier = None
    if notify and config.telegram.bot_token and config.telegram.chat_id:
        from app.services.notifier import TelegramNotifier

        notifier = TelegramNotifier(config)

    db = await get_db(root_path / "data" / "trading.db")
    try:
        await run_migrations(db)
        service = FlexReconciliationService(
            db=db, config=config.flex_query, notifier=notifier
        )

        if file_path:
            if not file_path.exists():
                raise FileNotFoundError(
                    f"Flex-Statement-Datei nicht gefunden: {file_path}"
                )
            xml_content = file_path.read_text(encoding="utf-8")
            report = await service.reconcile_from_xml(xml_content)
            if notifier and notifier.is_active:
                await notifier.send_flex_reconciliation_summary(report)
        else:
            report = await service.sync_and_reconcile(token=token, query_id=query_id)

        return report
    finally:
        await db.close()


def main() -> None:
    """CLI-Einstiegspunkt."""
    parser = argparse.ArgumentParser(
        description="IBKR Flex Query Synchronisation & Nebenkosten-Allokation"
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Pfad zu einer lokalen Flex Query XML-Datei (Offline-Import).",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Optionaler IBKR Flex Service Token (überschreibt .env/config).",
    )
    parser.add_argument(
        "--query-id",
        type=str,
        default=None,
        help="Optionale IBKR Flex Query ID (überschreibt .env/config).",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        default=False,
        help="Sendet den generierten Reconciliation-Bericht an Telegram.",
    )

    args = parser.parse_args()

    try:
        report = asyncio.run(
            run_flex_sync(
                file_path=args.file,
                token=args.token,
                query_id=args.query_id,
                notify=args.notify,
            )
        )
        print(format_flex_sync_report(report))
        sys.exit(0)
    except Exception as error:
        print(f"\n❌ Fehler bei der Flex-Synchronisation: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
