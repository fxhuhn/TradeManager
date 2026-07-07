"""
Datenbankverbindung und Schema-Migrationen.

Initialisiert die SQLite-Verbindung im WAL-Modus (Write-Ahead Logging),
aktiviert Fremdschlüssel-Constraints und führt Schema-Migrationen lexikografisch aus.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger()

# Globaler DB-Pfad. Kann fuer Tests auf ":memory:" überschrieben werden
DB_PATH: Path = Path("data/trading.db")


async def get_db(
    db_path: Path = DB_PATH, timeout_seconds: float = 30.0
) -> aiosqlite.Connection:
    """
    Erstellt eine aiosqlite-Verbindung mit isolation_level=None
    (deaktiviert impliziten Autocommit-Modus, Transaktionen müssen explizit gestartet werden).
    Konfiguriert wichtige PRAGMAs wie foreign_keys und journal_mode=WAL.
    Setzt den Verbindungstimeout zur Abwehr von Lockouts unter hoher Last.
    """
    if db_path != Path(":memory:"):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(
        str(db_path), timeout=timeout_seconds, isolation_level=None
    )
    db.row_factory = aiosqlite.Row

    # PRAGMAs setzen
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA synchronous = NORMAL;")
    return db


async def verify_db_integrity(db_path: Path = DB_PATH) -> bool:
    """Prüft die Datenbank auf strukturelle Fehler mit sicherem Timeout."""
    if not db_path.exists() and db_path != Path(":memory:"):
        return True

    try:
        # 30 Sekunden Timeout zur Ausfallprävention bei Integritätsprüfung
        async with aiosqlite.connect(str(db_path), timeout=30.0) as db:
            async with db.execute("PRAGMA integrity_check;") as cursor:
                row = await cursor.fetchone()
                if row and row[0] == "ok":
                    return True
                else:
                    logger.error(
                        "DB integrity check failed",
                        result=dict(row) if row else None,
                    )
                    return False
    except Exception as exception:
        logger.error("Integrity check failed", error=str(exception))
        return False


async def run_migrations(
    db: aiosqlite.Connection, migrations_directory: Path = Path("migrations")
) -> None:
    """
    Führt alle .sql-Dateien im migrations/-Verzeichnis lexikografisch aus.
    Erfasst angewendete Migrationen in der Tabelle 'schema_version'.
    """
    async with transaction(db):
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    if not migrations_directory.exists():
        logger.warning(
            "Migrations directory does not exist", path=str(migrations_directory)
        )
        return

    sql_files = sorted(migrations_directory.glob("*.sql"))

    for sql_file in sql_files:
        try:
            version_string = sql_file.name.split("_", 1)[0]
            version = int(version_string)
        except ValueError:
            logger.error("Invalid migration file format", file=sql_file.name)
            continue

        if await _is_migration_applied(db, version):
            continue

        logger.info("Executing migration", file=sql_file.name, version=version)
        await _apply_migration_file(db, sql_file, version)


async def _is_migration_applied(db: aiosqlite.Connection, version: int) -> bool:
    """Prüft, ob eine bestimmte Migrationsversion bereits angewendet wurde."""
    async with db.execute(
        "SELECT version FROM schema_version WHERE version = ?", (version,)
    ) as cursor:
        row = await cursor.fetchone()
        return row is not None


async def _apply_migration_file(
    db: aiosqlite.Connection, sql_file: Path, version: int
) -> None:
    """Führt ein einzelnes Migrationsskript aus und verbucht die Version."""
    sql_script = sql_file.read_text(encoding="utf-8")

    # Fremdschlüssel-Prüfungen vorübergehend ausschalten für Tabellen-Rekonstruktion
    await db.execute("PRAGMA foreign_keys = OFF;")

    try:
        async with transaction(db):
            await _execute_migration_statements(db, sql_script)
            await db.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,)
            )
            logger.info("Migration successfully applied", version=version)
    finally:
        # Fremdschlüssel-Prüfungen wieder aktivieren
        await db.execute("PRAGMA foreign_keys = ON;")


async def _execute_migration_statements(
    db: aiosqlite.Connection, sql_script: str
) -> None:
    """Führt die einzelnen Statements eines SQL-Skripts nacheinander aus."""
    for statement in sql_script.split(";"):
        statement_clean = statement.strip()
        if statement_clean:
            await db.execute(statement_clean)


@asynccontextmanager
async def transaction(db: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
    """Provides an atomic BEGIN IMMEDIATE / COMMIT / ROLLBACK transaction scope.

    Acquires an immediate write-lock on the SQLite database. All statements
    executed via the yielded connection are committed on successful exit,
    or rolled back if an exception propagates.

    Usage:
        async with transaction(db) as tx:
            await tx.execute("UPDATE orders SET status = ? WHERE order_id = ?", (...))
    """
    await db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        await db.execute("COMMIT")
    except Exception:
        await db.execute("ROLLBACK")
        raise


async def run_db_backup(db_path: Path) -> None:
    """Erstellt ein tägliches Backup der angegebenen Datenbank.

    Behält nur die letzten 5 Backups.
    Verwendet SQLite VACUUM INTO für sichere Hot-Backups.
    """
    logger.info("Starting database backup", database_name=db_path.name)

    try:
        # 1. Pfade definieren
        backup_directory = db_path.parent / "backup"
        backup_directory.mkdir(parents=True, exist_ok=True)

        timestamp_string = datetime.now().strftime("%Y-%m-%d")
        backup_filename = f"{db_path.name}.{timestamp_string}"
        backup_file = backup_directory / backup_filename

        # 2. Backup erstellen (VACUUM INTO)
        if backup_file.exists():
            logger.warning(
                "Backup already exists. Overwriting...",
                filename=backup_filename,
            )
            backup_file.unlink()

        async with aiosqlite.connect(str(db_path)) as connection:
            # SICHERHEITSHINWEIS: VACUUM INTO unterstützt keine parametrisierten Abfragen.
            # Der Pfad wird aus kontrollierten Eingaben (Dateiname + Zeitstempel) erstellt.
            backup_path_string = str(backup_file.resolve())
            await connection.execute(f"VACUUM INTO '{backup_path_string}'")

        logger.info("Backup successfully created", path=str(backup_file))

        # 3. Retention-Policy: Nur die letzten 5 behalten
        all_backups = sorted(backup_directory.glob(f"{db_path.name}.*"))

        keep_count = 5
        if len(all_backups) > keep_count:
            files_to_delete = all_backups[:-keep_count]
            for file_to_delete in files_to_delete:
                try:
                    file_to_delete.unlink()
                    logger.info("Old backup deleted", filename=file_to_delete.name)
                except Exception as delete_error:
                    logger.error(
                        "Error deleting old backup file",
                        filename=file_to_delete.name,
                        error=str(delete_error),
                    )

    except Exception as exception:
        logger.exception(
            "Database backup failed",
            database_path=str(db_path),
            error=str(exception),
        )
