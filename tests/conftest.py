import pytest
from pathlib import Path
import aiosqlite


@pytest.fixture
async def db():
    """
    Shared-Cache-URI: Erlaubt mehreren concurrent Connections Zugriff auf dieselbe
    In-Memory-Datenbank. Ideal für asynchrone Integrationstests.
    """
    conn = await aiosqlite.connect("file::memory:?cache=shared", uri=True)
    conn.row_factory = aiosqlite.Row

    # Wichtige PRAGMAs konfigurieren
    await conn.execute("PRAGMA foreign_keys=ON")

    # DDL aus der echten Migrations-Datei ausführen
    migrations_file = Path("migrations/001_initial.sql")
    if migrations_file.exists():
        sql = migrations_file.read_text(encoding="utf-8")
        for stmt in sql.split(";"):
            stmt_clean = stmt.strip()
            if stmt_clean:
                await conn.execute(stmt_clean)
        await conn.commit()

    yield conn
    await conn.close()
