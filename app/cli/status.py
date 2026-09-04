"""
CLI-Diagnosetool zur schnellen Statusabfrage des TradeManagers.

Liest lokal den aktuellen Zustand aus der SQLite-Datenbank und dem
CSV-Archivverzeichnis aus und gibt eine strukturierte Übersicht aus.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class AccountMetricsReport:
    """Kontokennzahlen für den Statusbericht."""

    account_id: str
    net_liquidation: Decimal
    total_cash_value: Decimal
    available_funds: Decimal
    maint_margin_req: Decimal
    cushion_pct: Decimal
    buying_power: Decimal
    updated_at: str


@dataclass(frozen=True)
class SystemStatusReport:
    """Zusammenfassung des operativen System- und Archivzustands."""

    db_accessible: bool
    recent_archive_files: list[str] = field(default_factory=list)
    has_archived_errors: bool = False
    order_counts_by_status: dict[str, int] = field(default_factory=dict)
    recent_failed_orders: list[dict[str, object]] = field(default_factory=list)
    today_settled_count: int = 0
    today_net_pnl: Decimal = Decimal("0.00")
    today_total_commissions: Decimal = Decimal("0.00")
    account_metrics: AccountMetricsReport | None = None


async def generate_system_status_report(
    database_path: Path = Path("data/trading.db"),
    archive_path: Path = Path("data/orders/archive"),
) -> SystemStatusReport:
    """Erstellt einen vollständigen Statusbericht aus Datenbank und Dateisystem.

    Args:
        database_path: Pfad zur SQLite-Datenbankdatei.
        archive_path: Pfad zum CSV-Archivverzeichnis.

    Returns:
        SystemStatusReport mit aggregierten Zuständen.
    """
    recent_files: list[str] = []
    has_errors: bool = False
    if archive_path.exists() and archive_path.is_dir():
        all_files = sorted(
            archive_path.iterdir(),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for item in all_files[:5]:
            recent_files.append(item.name)
            if item.name.endswith(".err"):
                has_errors = True

    if not database_path.exists():
        return SystemStatusReport(
            db_accessible=False,
            recent_archive_files=recent_files,
            has_archived_errors=has_errors,
        )

    order_counts: dict[str, int] = {}
    failed_orders: list[dict[str, object]] = []
    today_settled_count = 0
    today_net_pnl = Decimal("0.00")
    today_total_commissions = Decimal("0.00")

    try:
        async with aiosqlite.connect(database_path) as db:
            db.row_factory = aiosqlite.Row

            # 1. Orders nach Status aggregieren
            count_query = "SELECT status, COUNT(*) AS count FROM orders GROUP BY status"
            async with db.execute(count_query) as cursor:
                async for row in cursor:
                    order_counts[str(row["status"])] = int(row["count"])

            # 2. Letzte fehlgeschlagene oder stornierte Orders abrufen
            failed_query = """
                SELECT order_id, trade_group_id, symbol, bracket_role, status, transmitted_at
                FROM orders
                WHERE status IN ('Error', 'Cancelled')
                ORDER BY order_id DESC
                LIMIT 5
            """
            async with db.execute(failed_query) as cursor:
                async for row in cursor:
                    failed_orders.append(
                        {
                            "order_id": row["order_id"],
                            "trade_group_id": row["trade_group_id"],
                            "symbol": row["symbol"],
                            "bracket_role": row["bracket_role"],
                            "status": row["status"],
                            "transmitted_at": row["transmitted_at"],
                        }
                    )

            # 3. Heutige Settlements aggregieren
            settlement_query = """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(CAST(net_pnl AS REAL)), 0.0) AS total_pnl,
                       COALESCE(SUM(CAST(total_commissions AS REAL)), 0.0) AS total_fees
                FROM trades_settlement
                WHERE date(settled_at) = date('now', 'localtime')
            """
            async with db.execute(settlement_query) as cursor:
                settlement_row = await cursor.fetchone()
                if settlement_row:
                    today_settled_count = int(settlement_row["count"])
                    today_net_pnl = Decimal(str(settlement_row["total_pnl"]))
                    today_total_commissions = Decimal(str(settlement_row["total_fees"]))

            # 4. Aktuelle Kontowerte und Margin abrufen
            account_metrics_report: AccountMetricsReport | None = None
            metrics_query = """
                SELECT account_id, net_liquidation, total_cash_value, available_funds,
                       maint_margin_req, cushion_pct, buying_power, updated_at
                FROM account_metrics
                ORDER BY updated_at DESC
                LIMIT 1
            """
            try:
                async with db.execute(metrics_query) as cursor:
                    metrics_row = await cursor.fetchone()
                    if metrics_row:
                        account_metrics_report = AccountMetricsReport(
                            account_id=str(metrics_row["account_id"]),
                            net_liquidation=Decimal(
                                str(metrics_row["net_liquidation"])
                            ),
                            total_cash_value=Decimal(
                                str(metrics_row["total_cash_value"])
                            ),
                            available_funds=Decimal(
                                str(metrics_row["available_funds"])
                            ),
                            maint_margin_req=Decimal(
                                str(metrics_row["maint_margin_req"])
                            ),
                            cushion_pct=Decimal(str(metrics_row["cushion_pct"])),
                            buying_power=Decimal(str(metrics_row["buying_power"])),
                            updated_at=str(metrics_row["updated_at"]),
                        )
            except Exception:
                account_metrics_report = None

        return SystemStatusReport(
            db_accessible=True,
            recent_archive_files=recent_files,
            has_archived_errors=has_errors,
            order_counts_by_status=order_counts,
            recent_failed_orders=failed_orders,
            today_settled_count=today_settled_count,
            today_net_pnl=today_net_pnl,
            today_total_commissions=today_total_commissions,
            account_metrics=account_metrics_report,
        )
    except Exception:
        return SystemStatusReport(
            db_accessible=False,
            recent_archive_files=recent_files,
            has_archived_errors=has_errors,
        )


def format_status_report(report: SystemStatusReport) -> str:
    """Formatiert den Statusbericht in eine lesbare Terminal-Ausgabe.

    Args:
        report: Die zu formatierende SystemStatusReport-Instanz.

    Returns:
        Formatierte Textausgabe.
    """
    lines: list[str] = [
        "============================================================",
        "📊 TRADEMANAGER SYSTEM-STATUSBERICHT",
        "============================================================",
    ]

    # DB-Status
    db_indicator = (
        "🟢 Erreichbar" if report.db_accessible else "🔴 Nicht erreichbar / Fehler"
    )
    lines.append(f"Datenbank: {db_indicator}")

    # Kontostand & Margin
    if report.account_metrics:
        m = report.account_metrics
        cushion_icon = (
            "🟢"
            if m.cushion_pct >= Decimal("20.0")
            else ("🟡" if m.cushion_pct >= Decimal("10.0") else "🔴")
        )
        lines.append(f"\n💼 Kontostand & Margin (Stand: {m.updated_at}):")
        lines.append(f"  - Net Liquidation (Equity) : $ {m.net_liquidation:,.2f}")
        lines.append(f"  - Genutzte Margin (Maint)  : $ {m.maint_margin_req:,.2f}")
        lines.append(f"  - Freie Mittel (Available) : $ {m.available_funds:,.2f}")
        lines.append(
            f"  - Konto-Cushion            : {cushion_icon} {m.cushion_pct:.1f}%"
        )
        lines.append(f"  - Buying Power             : $ {m.buying_power:,.2f}")

    # Archiv-Dateien
    lines.append("\n📁 Letzte Archiv-Dateien:")
    if report.recent_archive_files:
        for file_name in report.recent_archive_files:
            icon = "🚨" if file_name.endswith(".err") else "✅"
            lines.append(f"  {icon} {file_name}")
    else:
        lines.append("  (Keine Archiv-Dateien gefunden)")

    # Order-Statistik
    lines.append("\n📋 Orders nach Status:")
    if report.order_counts_by_status:
        for status, count in sorted(report.order_counts_by_status.items()):
            lines.append(f"  - {status:<15}: {count}")
    else:
        lines.append("  (Keine Orders in Datenbank)")

    # Letzte Fehlgeschlagene Orders
    lines.append("\n⚠️ Letzte stornierte / fehlerhafte Orders:")
    if report.recent_failed_orders:
        for item in report.recent_failed_orders:
            lines.append(
                f"  - [{item['status']}] ID: {item['order_id']} | "
                f"{item['symbol']} ({item['bracket_role']}) | Gruppe: {item['trade_group_id']}"
            )
    else:
        lines.append("  (Keine fehlerhaften oder stornierten Orders)")

    # Settlements
    lines.append("\n💰 Heutige Abrechnung (Settlement):")
    pnl_indicator = "🟢" if report.today_net_pnl >= Decimal("0.00") else "🔴"
    lines.append(f"  - Abgeschlossene Gruppen : {report.today_settled_count}")
    lines.append(
        f"  - Realisierter Net PnL   : {pnl_indicator} $ {report.today_net_pnl:,.2f}"
    )
    lines.append(
        f"  - Kommissionen gesamt    : $ {report.today_total_commissions:,.2f}"
    )
    lines.append("============================================================")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Haupt-Einstiegspunkt für das CLI-Diagnosetool.

    Args:
        argv: Optionale Kommandozeilen-Argumente.

    Returns:
        Statuscode (0 = OK, 1 = Fehlerzustand erkannt).
    """
    parser = argparse.ArgumentParser(description="TradeManager Status CLI")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/trading.db"),
        help="Pfad zur SQLite-Datenbank",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path("data/orders/archive"),
        help="Pfad zum Archiv-Verzeichnis",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(
        generate_system_status_report(database_path=args.db, archive_path=args.archive)
    )
    output = format_status_report(report)
    print(output)

    if not report.db_accessible or report.has_archived_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
