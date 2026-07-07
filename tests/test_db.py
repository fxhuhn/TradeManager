# filename: tests/test_db.py
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from app.core.db import run_db_backup


@pytest.mark.asyncio
async def test_run_db_backup_creates_valid_hot_backup_successfully(
    tmp_path: Path,
) -> None:
    """Verifies that run_db_backup creates a valid SQLite backup containing all data."""
    # Arrange
    db_path = tmp_path / "trading.db"

    # Setup dummy database with a table and a record
    async with aiosqlite.connect(str(db_path)) as connection:
        await connection.execute("CREATE TABLE test_table (id INTEGER, val TEXT);")
        await connection.execute(
            "INSERT INTO test_table (id, val) VALUES (1, 'test_value');"
        )
        await connection.commit()

    # Mock datetime to return a fixed date
    mocked_time = datetime(2026, 7, 7, 10, 0, 0)
    with patch("app.core.db.datetime") as mock_datetime:
        mock_datetime.now.return_value = mocked_time

        # Act
        await run_db_backup(db_path)

    # Assert
    backup_file = tmp_path / "backup" / "trading.db.2026-07-07"
    assert backup_file.exists()

    # Verify backup contains correct table structure and data
    async with aiosqlite.connect(str(backup_file)) as backup_connection:
        async with backup_connection.execute("SELECT * FROM test_table;") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 1
            assert rows[0][1] == "test_value"


@pytest.mark.asyncio
async def test_run_db_backup_overwrites_existing_backup_for_same_day(
    tmp_path: Path,
) -> None:
    """Verifies that if a backup file already exists for today, it is overwritten with the new state."""
    # Arrange
    db_path = tmp_path / "trading.db"
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    backup_file = backup_dir / "trading.db.2026-07-07"

    # Write old dummy data to the pre-existing backup file
    backup_file.write_text("pre-existing corrupt database content", encoding="utf-8")

    # Setup active database with valid structure
    async with aiosqlite.connect(str(db_path)) as connection:
        await connection.execute("CREATE TABLE orders (id INTEGER);")
        await connection.commit()

    mocked_time = datetime(2026, 7, 7, 12, 0, 0)
    with patch("app.core.db.datetime") as mock_datetime:
        mock_datetime.now.return_value = mocked_time

        # Act
        await run_db_backup(db_path)

    # Assert
    assert backup_file.exists()
    # Confirm it is no longer the text file, but a valid SQLite DB file
    async with aiosqlite.connect(str(backup_file)) as backup_connection:
        async with backup_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ) as cursor:
            tables = await cursor.fetchall()
            assert "orders" in [table[0] for table in tables]


@pytest.mark.asyncio
async def test_run_db_backup_retains_only_five_most_recent_backups(
    tmp_path: Path,
) -> None:
    """Verifies that only the last 5 backups are kept according to the retention policy."""
    # Arrange
    db_path = tmp_path / "trading.db"
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    # Create 6 historical backup files with different dates
    dates = [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-04",
        "2026-07-05",
        "2026-07-06",
    ]
    for d in dates:
        (backup_dir / f"trading.db.{d}").write_text(
            "sqlite placeholder", encoding="utf-8"
        )

    # Set up active database
    async with aiosqlite.connect(str(db_path)) as connection:
        await connection.execute("CREATE TABLE test (id INTEGER);")
        await connection.commit()

    # Mock datetime to today's date
    mocked_time = datetime(2026, 7, 7, 10, 0, 0)
    with patch("app.core.db.datetime") as mock_datetime:
        mock_datetime.now.return_value = mocked_time

        # Act
        await run_db_backup(db_path)

    # Assert
    # We started with 6, added 1 (2026-07-07) -> total 7.
    # Retention policy should delete the two oldest (2026-07-01 and 2026-07-02)
    # and keep exactly 5: 07-03, 07-04, 07-05, 07-06, 07-07.
    remaining_backups = sorted([f.name for f in backup_dir.glob("trading.db.*")])
    assert len(remaining_backups) == 5
    assert "trading.db.2026-07-01" not in remaining_backups
    assert "trading.db.2026-07-02" not in remaining_backups
    assert remaining_backups == [
        "trading.db.2026-07-03",
        "trading.db.2026-07-04",
        "trading.db.2026-07-05",
        "trading.db.2026-07-06",
        "trading.db.2026-07-07",
    ]


@pytest.mark.asyncio
async def test_run_db_backup_continues_on_unlink_error_during_cleanup(
    tmp_path: Path,
) -> None:
    """Verifies that an unlink permission error on one backup does not crash the function and other files are deleted."""
    # Arrange
    db_path = tmp_path / "trading.db"
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    # Create 6 historical backups
    dates = [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-04",
        "2026-07-05",
        "2026-07-06",
    ]
    for d in dates:
        (backup_dir / f"trading.db.{d}").write_text(
            "sqlite placeholder", encoding="utf-8"
        )

    # Set up active database
    async with aiosqlite.connect(str(db_path)) as connection:
        await connection.execute("CREATE TABLE test (id INTEGER);")
        await connection.commit()

    # Mock datetime to today's date
    mocked_time = datetime(2026, 7, 7, 10, 0, 0)

    # Patch Path.unlink to raise PermissionError when trying to delete the oldest backup (2026-07-01),
    # but succeed for others.
    original_unlink = Path.unlink

    def mock_unlink(self_path: Path, *args, **kwargs) -> None:
        if self_path.name == "trading.db.2026-07-01":
            raise PermissionError("Permission Denied")
        original_unlink(self_path, *args, **kwargs)

    with (
        patch("app.core.db.datetime") as mock_datetime,
        patch.object(Path, "unlink", autospec=True, side_effect=mock_unlink),
    ):
        mock_datetime.now.return_value = mocked_time

        # Act
        await run_db_backup(db_path)

    # Assert
    remaining_backups = sorted([f.name for f in backup_dir.glob("trading.db.*")])
    # The oldest (2026-07-01) couldn't be deleted, but 2026-07-02 SHOULD have been deleted.
    # Total remaining should be 6: 07-01 (failed to delete), and 07-03, 07-04, 07-05, 07-06, 07-07.
    assert "trading.db.2026-07-02" not in remaining_backups
    assert "trading.db.2026-07-01" in remaining_backups
    assert len(remaining_backups) == 6
