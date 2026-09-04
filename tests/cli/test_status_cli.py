"""Unit-Tests für das CLI-Diagnosetool app.cli.status."""

from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from app.cli.status import (
    SystemStatusReport,
    format_status_report,
    generate_system_status_report,
    main,
)


@pytest.mark.asyncio
async def test_generate_system_status_report_with_valid_database(
    tmp_path: Path,
) -> None:
    """Verifiziert, dass der Statusbericht korrekt aggregiert wird."""
    # Arrange
    database_file = tmp_path / "trading.db"
    archive_directory = tmp_path / "archive"
    archive_directory.mkdir()

    (archive_directory / "orders_2026_09_03.csv.bak").write_text("test")
    (archive_directory / "orders_2026_09_04.csv.err").write_text("test")

    async with aiosqlite.connect(database_file) as connection:
        await connection.execute(
            """
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                trade_group_id TEXT,
                symbol TEXT,
                bracket_role TEXT,
                status TEXT,
                transmitted_at TEXT
            )
            """
        )
        await connection.execute(
            """
            CREATE TABLE trades_settlement (
                trade_group_id TEXT PRIMARY KEY,
                net_pnl TEXT,
                total_commissions TEXT,
                settled_at TEXT
            )
            """
        )
        await connection.execute(
            "INSERT INTO orders VALUES (1, 'G1', 'AAPL', 'ENTRY', 'Filled', '2026-09-04 10:00:00')"
        )
        await connection.execute(
            "INSERT INTO orders VALUES (2, 'G2', 'MSFT', 'ENTRY', 'Error', '2026-09-04 10:05:00')"
        )
        await connection.execute(
            "INSERT INTO trades_settlement VALUES ('G1', '150.50', '2.00', datetime('now', 'localtime'))"
        )
        await connection.commit()

    # Act
    report = await generate_system_status_report(
        database_path=database_file, archive_path=archive_directory
    )

    # Assert
    assert report.db_accessible is True
    assert report.has_archived_errors is True
    assert len(report.recent_archive_files) == 2
    assert report.order_counts_by_status == {"Filled": 1, "Error": 1}
    assert len(report.recent_failed_orders) == 1
    assert report.recent_failed_orders[0]["symbol"] == "MSFT"
    assert report.today_settled_count == 1
    assert report.today_net_pnl == Decimal("150.50")
    assert report.today_total_commissions == Decimal("2.00")


@pytest.mark.asyncio
async def test_generate_system_status_report_handles_missing_database(
    tmp_path: Path,
) -> None:
    """Verifiziert, dass eine fehlende Datenbank sauber abgefangen wird."""
    # Arrange
    database_file = tmp_path / "non_existing.db"
    archive_directory = tmp_path / "archive"
    archive_directory.mkdir()

    # Act
    report = await generate_system_status_report(
        database_path=database_file, archive_path=archive_directory
    )

    # Assert
    assert report.db_accessible is False
    assert report.has_archived_errors is False


def test_format_status_report_renders_expected_output() -> None:
    """Verifiziert, dass format_status_report saubere Terminal-Zeilen generiert."""
    # Arrange
    report = SystemStatusReport(
        db_accessible=True,
        recent_archive_files=["orders_2026_09_04.csv.bak"],
        has_archived_errors=False,
        order_counts_by_status={"Filled": 5, "Cancelled": 1},
        recent_failed_orders=[
            {
                "order_id": 10,
                "trade_group_id": "G1",
                "symbol": "NVDA",
                "bracket_role": "ENTRY",
                "status": "Cancelled",
            }
        ],
        today_settled_count=2,
        today_net_pnl=Decimal("250.00"),
        today_total_commissions=Decimal("3.50"),
    )

    # Act
    formatted_output = format_status_report(report)

    # Assert
    assert "TRADEMANAGER SYSTEM-STATUSBERICHT" in formatted_output
    assert "Datenbank: 🟢 Erreichbar" in formatted_output
    assert "orders_2026_09_04.csv.bak" in formatted_output
    assert "NVDA" in formatted_output
    assert "250.00" in formatted_output


def test_main_cli_returns_nonzero_on_errors(tmp_path: Path) -> None:
    """Verifiziert, dass der CLI-Einstiegspunkt bei Fehlern Exit-Code 1 liefert."""
    # Arrange
    database_file = tmp_path / "trading.db"
    archive_directory = tmp_path / "archive"
    archive_directory.mkdir()
    (archive_directory / "orders_2026_09_04.csv.err").write_text("error")

    # Act
    exit_code = main(["--db", str(database_file), "--archive", str(archive_directory)])

    # Assert
    assert exit_code == 1
