"""Unit-Tests für die erweiterten AlertWatcher- und Notifier-Funktionen."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from app.services.alert_watcher import (
    AlertState,
    check_archived_error_files,
    check_hanging_orders,
)
from app.services.notifier import TelegramNotifier


@pytest.mark.asyncio
async def test_alert_state_file_and_hanging_order_reporting() -> None:
    """Verifiziert das Merken von gemeldeten Dateien und hängenden Orders in AlertState."""
    # Arrange
    state = AlertState()

    # Act & Assert
    assert not state.is_file_reported("test.err")
    state.mark_file_reported("test.err")
    assert state.is_file_reported("test.err")

    assert not state.is_hanging_order_reported(999)
    state.mark_hanging_order_reported(999)
    assert state.is_hanging_order_reported(999)


@pytest.mark.asyncio
async def test_check_archived_error_files_triggers_alert(tmp_path: Path) -> None:
    """Verifiziert, dass check_archived_error_files bei neuen .err-Dateien alarmiert."""
    # Arrange
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    err_file = archive_dir / "orders_2026_09_04.csv.err"
    err_file.write_text("test error content")

    state = AlertState()
    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.send_archived_error_alert = AsyncMock(return_value=True)

    # Act
    await check_archived_error_files(
        archive_dir=archive_dir, notifier=mock_notifier, state=state
    )

    # Assert
    mock_notifier.send_archived_error_alert.assert_called_once_with(
        "orders_2026_09_04.csv.err"
    )
    assert state.is_file_reported("orders_2026_09_04.csv.err")

    # Zweiter Aufruf darf keinen weiteren Alert senden (Entprellung)
    mock_notifier.send_archived_error_alert.reset_mock()
    await check_archived_error_files(
        archive_dir=archive_dir, notifier=mock_notifier, state=state
    )
    mock_notifier.send_archived_error_alert.assert_not_called()


@pytest.mark.asyncio
async def test_check_hanging_orders_triggers_alert(
    tmp_path: Path,
) -> None:
    """Verifiziert, dass check_hanging_orders bei Orders im Status 'Created' alarmiert."""
    # Arrange
    database_file = tmp_path / "test.db"
    async with aiosqlite.connect(database_file) as connection:
        connection.row_factory = aiosqlite.Row
        await connection.execute(
            """
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                trade_group_id TEXT,
                symbol TEXT,
                status TEXT
            )
            """
        )
        await connection.execute(
            "INSERT INTO orders VALUES (101, 'G_HANG', 'AAPL', 'Created')"
        )
        await connection.commit()

        state = AlertState()
        mock_notifier = MagicMock(spec=TelegramNotifier)
        mock_notifier.send_message = AsyncMock(return_value=True)

        # Act
        await check_hanging_orders(
            db=connection, notifier=mock_notifier, state=state, threshold_minutes=10
        )

        # Assert
        mock_notifier.send_message.assert_called_once()
        sent_message = mock_notifier.send_message.call_args[0][0]
        assert "HÄNGENDE ORDER" in sent_message
        assert "AAPL" in sent_message
        assert state.is_hanging_order_reported(101)

        # Zweiter Durchlauf darf nicht noch einmal senden
        mock_notifier.send_message.reset_mock()
        await check_hanging_orders(
            db=connection, notifier=mock_notifier, state=state, threshold_minutes=10
        )
        mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notifier_send_archived_error_alert_and_daily_summary() -> None:
    """Verifiziert die neuen Formatierungsmethoden im TelegramNotifier."""
    # Arrange
    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.send_message = AsyncMock(return_value=True)

    # Act 1: send_archived_error_alert
    success_error_alert = await notifier.send_archived_error_alert(
        file_name="orders_2026_09_04.csv.err", details="Manuelle Prüfung erforderlich"
    )

    # Assert 1
    assert success_error_alert is True
    call_text = notifier.send_message.call_args[0][0]
    assert "ARCHIVIERTE FEHLERDATEI ENTDECKT" in call_text
    assert "orders_2026_09_04.csv.err" in call_text

    # Act 2: send_daily_summary
    success_summary = await notifier.send_daily_summary(
        date_str="2026-09-04",
        total_orders=10,
        filled_orders=8,
        cancelled_orders=2,
        net_pnl=Decimal("350.25"),
        commissions=Decimal("5.00"),
        file_status="Erfolgreich (.bak)",
    )

    # Assert 2
    assert success_summary is True
    summary_text = notifier.send_message.call_args[0][0]
    assert "TAGESABSCHLUSS-BERICHT" in summary_text
    assert "2026-09-04" in summary_text
    assert "350.25" in summary_text
