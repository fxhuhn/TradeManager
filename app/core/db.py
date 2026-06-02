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
                        "DB-Integritaetspruefung fehlgeschlagen",
                        result=dict(row) if row else None,
                    )
                    return False
    except Exception as exception:
        logger.error("Integritaetspruefung verunglueckt", error=str(exception))
        return False


async def run_migrations(
    db: aiosqlite.Connection, migrations_dir: Path = Path("migrations")
) -> None:
    """
    Führt alle .sql-Dateien im migrations/-Verzeichnis lexikografisch aus.
    Erfasst angewendete Migrationen in der Tabelle 'schema_version'.
    """
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await db.execute("COMMIT")
    except Exception:
        await db.execute("ROLLBACK")
        raise

    if not migrations_dir.exists():
        logger.warning(
            "Migrationsverzeichnis existiert nicht", path=str(migrations_dir)
        )
        return

    sql_files = sorted(migrations_dir.glob("*.sql"))

    for sql_file in sql_files:
        try:
            version_str = sql_file.name.split("_", 1)[0]
            version = int(version_str)
        except ValueError:
            logger.error("Ungueltiges Migrationsdateiformat", file=sql_file.name)
            continue

        async with db.execute(
            "SELECT version FROM schema_version WHERE version = ?", (version,)
        ) as cursor:
            if await cursor.fetchone():
                continue

        logger.info("Führe Migration aus", file=sql_file.name, version=version)
        sql_script = sql_file.read_text(encoding="utf-8")

        await db.execute("BEGIN IMMEDIATE")
        try:
            for statement in sql_script.split(";"):
                statement_clean = statement.strip()
                if statement_clean:
                    await db.execute(statement_clean)

            await db.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,)
            )
            await db.execute("COMMIT")
            logger.info("Migration erfolgreich angewendet", version=version)
        except Exception as exception:
            await db.execute("ROLLBACK")
            logger.error("Fehler bei Migration", version=version, error=str(exception))
            raise exception


async def safe_execute_transaction(
    db: aiosqlite.Connection, sql: str, params: tuple = ()
) -> None:
    """
    Hilfsfunktion zur sicheren Ausführung einer einzelnen manipulierenden Anweisung
    im BEGIN IMMEDIATE Block (erwirbt sofort Write-Lock).
    """
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(sql, params)
        await db.execute("COMMIT")
    except Exception as exception:
        await db.execute("ROLLBACK")
        raise exception
