"""Integrationstest für den Mehrtages-Lebenszyklus (Multi-Day Lifecycle).

Simuliert aufeinanderfolgende Handelstage (Tag 1 -> Tag 2), um sicherzustellen,
dass abgelaufene/stornierte Orders aus Vortagen keine fehlerhaften .err-Archivierungen
am Folgetag verursachen.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.cli.status import generate_system_status_report
from app.core.config import Config
from app.services.importer import _process_daily_csv_file


@pytest.mark.asyncio
async def test_multiday_lifecycle_succession(
    tmp_path: Path, test_config: Config, db: aiosqlite.Connection
) -> None:
    """Simuliert Tag 1 (mit Stornierung zum EOD) gefolgt von Tag 2 (erfolgreiche Orders)."""
    # Arrange
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = data_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    csv_day1 = data_dir / "orders_2026_09_03.csv"
    csv_day1.write_text("trade_group_id,symbol\nTG_DAY1,AAPL\n", encoding="utf-8")

    csv_day2 = data_dir / "orders_2026_09_04.csv"
    csv_day2.write_text("trade_group_id,symbol\nTG_DAY2,MSFT\n", encoding="utf-8")

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    async def db_factory() -> aiosqlite.Connection:
        connection = await aiosqlite.connect("file::memory:?cache=shared", uri=True)
        connection.row_factory = aiosqlite.Row
        return connection

    # --- TAG 1 DURCHLAUF ---
    with patch(
        "app.services.importer.run_csv_import",
        new_callable=AsyncMock,
        return_value=["TG_DAY1"],
    ):
        # Order wird platziert
        await db.execute(
            """
            INSERT INTO orders (
                order_id, trade_group_id, account_id, bracket_role, symbol,
                sec_type, exchange, action, quantity, order_type, target_price,
                status, transmitted_at
            )
            VALUES (
                1001, 'TG_DAY1', 'U123456', 'ENTRY', 'AAPL',
                'STK', 'SMART', 'BUY', 50, 'LMT', 150.0,
                'PreSubmitted', '2026-09-03 09:30:00'
            )
            """
        )
        await db.commit()

        await _process_daily_csv_file(
            db_factory=db_factory,
            interactive_brokers=mock_ib,
            csv_file=csv_day1,
            queue=mock_queue,
            notifier=mock_notifier,
            config=test_config,
        )

    # Verifikation Tag 1 Archivierung
    assert not csv_day1.exists()
    assert (archive_dir / "orders_2026_09_03.csv.bak").exists()

    # --- EOD TAG 1: Order verfällt zum Börsenschluss (Cancelled) ---
    await db.execute("UPDATE orders SET status = 'Cancelled' WHERE order_id = 1001")
    await db.commit()

    # --- TAG 2 DURCHLAUF ---
    with patch(
        "app.services.importer.run_csv_import",
        new_callable=AsyncMock,
        return_value=["TG_DAY2"],
    ):
        # Order von Tag 2 wird platziert
        await db.execute(
            """
            INSERT INTO orders (
                order_id, trade_group_id, account_id, bracket_role, symbol,
                sec_type, exchange, action, quantity, order_type, target_price,
                status, transmitted_at
            )
            VALUES (
                1002, 'TG_DAY2', 'U123456', 'ENTRY', 'MSFT',
                'STK', 'SMART', 'BUY', 30, 'LMT', 300.0,
                'PreSubmitted', '2026-09-04 09:30:00'
            )
            """
        )
        await db.commit()

        await _process_daily_csv_file(
            db_factory=db_factory,
            interactive_brokers=mock_ib,
            csv_file=csv_day2,
            queue=mock_queue,
            notifier=mock_notifier,
            config=test_config,
        )

    # --- VERIFIKATION TAG 2 ---
    assert not csv_day2.exists()
    assert (archive_dir / "orders_2026_09_04.csv.bak").exists()
    assert not (archive_dir / "orders_2026_09_04.csv.err").exists()

    # --- STATUS-BERICHT PRÜFUNG ---
    status_report = await generate_system_status_report(
        database_path=tmp_path
        / "non_existing.db",  # Shared in-memory wird hier umgangen
        archive_path=archive_dir,
    )
    assert status_report.has_archived_errors is False
    assert len(status_report.recent_archive_files) == 2
