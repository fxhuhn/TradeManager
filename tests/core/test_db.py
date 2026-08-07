# filename: tests/core/test_db.py
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from app.core.db import (
    get_db,
    run_db_backup,
    run_migrations,
    transaction,
    verify_db_integrity,
)


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


@pytest.mark.asyncio
async def test_get_db_initializes_pragmas_and_creates_directory(tmp_path: Path) -> None:
    """Verifies that get_db creates parent directories, configures WAL mode, and enables foreign keys."""
    # Arrange
    db_file = tmp_path / "sub_dir" / "trading_test.db"

    # Act
    db = await get_db(db_file)

    # Assert
    assert db_file.parent.exists()
    async with db.execute("PRAGMA foreign_keys;") as cursor:
        fk_row = await cursor.fetchone()
        assert fk_row is not None and fk_row[0] == 1

    async with db.execute("PRAGMA journal_mode;") as cursor:
        jm_row = await cursor.fetchone()
        assert jm_row is not None and jm_row[0].lower() == "wal"

    await db.close()


@pytest.mark.asyncio
async def test_verify_db_integrity_outcomes(tmp_path: Path) -> None:
    """Verifies verify_db_integrity for non-existent files, valid databases, and corrupted databases."""
    # 1. Non-existent file returns True
    non_existent_file = tmp_path / "does_not_exist.db"
    assert await verify_db_integrity(non_existent_file) is True

    # 2. Valid database file returns True
    valid_db_file = tmp_path / "valid.db"
    async with aiosqlite.connect(str(valid_db_file)) as conn:
        await conn.execute("CREATE TABLE sample (id INT);")
        await conn.commit()
    assert await verify_db_integrity(valid_db_file) is True

    # 3. Corrupted database file returns False
    corrupt_db_file = tmp_path / "corrupt.db"
    corrupt_db_file.write_bytes(b"CORRUPTED INVALID SQLITE FILE HEADER CONTENT")
    assert await verify_db_integrity(corrupt_db_file) is False


@pytest.mark.asyncio
async def test_run_migrations_executes_sql_files_idempotently(tmp_path: Path) -> None:
    """Verifies run_migrations executes migrations in version order, handles invalid file names, and is idempotent."""
    # Arrange
    db_file = tmp_path / "migration_test.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    # Create migration files
    (migrations_dir / "001_initial_schema.sql").write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);", encoding="utf-8"
    )
    (migrations_dir / "002_add_email.sql").write_text(
        "ALTER TABLE users ADD COLUMN email TEXT;", encoding="utf-8"
    )
    (migrations_dir / "invalid_migration.sql").write_text(
        "INVALID FORMAT FILE", encoding="utf-8"
    )

    db = await get_db(db_file)

    # Act 1: Run migrations first time

    await run_migrations(db, migrations_dir)

    # Assert 1: Tables created and version recorded
    async with db.execute(
        "SELECT version FROM schema_version ORDER BY version"
    ) as cursor:
        versions = [row["version"] for row in await cursor.fetchall()]
        assert versions == [1, 2]

    async with db.execute("PRAGMA table_info(users)") as cursor:
        columns = [row["name"] for row in await cursor.fetchall()]
        assert "id" in columns
        assert "name" in columns
        assert "email" in columns

    # Act 2: Run migrations second time (idempotency)
    await run_migrations(db, migrations_dir)

    # Assert 2: No duplicate execution
    async with db.execute(
        "SELECT version FROM schema_version ORDER BY version"
    ) as cursor:
        versions_after = [row["version"] for row in await cursor.fetchall()]
        assert versions_after == [1, 2]

    # Act 3: Run migrations with non-existent directory
    missing_dir = tmp_path / "non_existent_migrations"
    await run_migrations(db, missing_dir)

    await db.close()


@pytest.mark.asyncio
async def test_transaction_context_manager_rolls_back_on_error(tmp_path: Path) -> None:
    """Verifies that transaction context manager rolls back changes if an exception occurs."""
    # Arrange
    db_file = tmp_path / "tx_test.db"
    db = await get_db(db_file)
    await db.execute("CREATE TABLE accounts (id INT PRIMARY KEY, balance REAL);")

    # Act
    with pytest.raises(RuntimeError, match="Transaction abort simulation"):
        async with transaction(db):
            await db.execute("INSERT INTO accounts (id, balance) VALUES (1, 100.0);")
            raise RuntimeError("Transaction abort simulation")

    # Assert: Inserted row was rolled back
    async with db.execute("SELECT COUNT(*) as cnt FROM accounts;") as cursor:
        row = await cursor.fetchone()
        assert row is not None and row["cnt"] == 0

    await db.close()


@pytest.mark.asyncio
async def test_run_db_backup_handles_exception_gracefully(tmp_path: Path) -> None:
    """Verifies that run_db_backup handles runtime exceptions (e.g. read-only destination) without crashing."""
    # Arrange
    invalid_db_path = tmp_path / "non_existent_folder_xyz" / "test.db"

    # Patch datetime to prevent issues
    mocked_time = datetime(2026, 7, 7, 10, 0, 0)
    with patch("app.core.db.datetime") as mock_datetime:
        mock_datetime.now.return_value = mocked_time
        # Force exception inside backup by making parent directory creation fail or raising exception
        with patch.object(
            Path, "mkdir", side_effect=PermissionError("Permission Denied")
        ):
            # Act & Assert
            try:
                await run_db_backup(invalid_db_path)
            except Exception as exc:
                pytest.fail(f"run_db_backup raised unexpected exception: {exc}")


@pytest.mark.asyncio
async def test_verify_db_integrity_returns_false_on_integrity_error_result(
    tmp_path: Path,
) -> None:
    """Verifies verify_db_integrity returns False when PRAGMA integrity_check result is not ok."""
    valid_db_file = tmp_path / "integrity_bad.db"
    async with aiosqlite.connect(str(valid_db_file)) as conn:
        await conn.execute("CREATE TABLE sample (id INT);")
        await conn.commit()

    class MockCursor:
        async def __aenter__(self) -> "MockCursor":
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
            pass

        async def fetchone(self) -> dict[str, str]:
            return {"integrity_check": "error: corrupt"}

    class MockConnection:
        async def __aenter__(self) -> "MockConnection":
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
            pass

        def execute(self, sql: str) -> MockCursor:
            return MockCursor()

    with patch("aiosqlite.connect", return_value=MockConnection()):
        result = await verify_db_integrity(valid_db_file)
        assert result is False
