from pathlib import Path

import aiosqlite
import pytest


@pytest.fixture
async def db():
    """
    Shared-Cache-URI: Erlaubt mehreren concurrent Connections Zugriff auf dieselbe
    In-Memory-Datenbank. Ideal für asynchrone Integrationstests.
    """
    connection = await aiosqlite.connect("file::memory:?cache=shared", uri=True)
    connection.row_factory = aiosqlite.Row

    # Wichtige PRAGMAs konfigurieren
    await connection.execute("PRAGMA foreign_keys=ON")

    # DDL aus der echten Migrations-Datei ausführen
    migrations_file = Path("migrations/001_initial.sql")
    if migrations_file.exists():
        sql = migrations_file.read_text(encoding="utf-8")
        for stmt in sql.split(";"):
            stmt_clean = stmt.strip()
            if stmt_clean:
                await connection.execute(stmt_clean)
        await connection.commit()

    yield connection
    await connection.close()
