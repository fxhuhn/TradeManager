from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from app.core.config import (
    AccountConfig,
    AppConfig,
    Config,
    TelegramConfig,
    TwsConfig,
)


@pytest.fixture
async def db() -> AsyncIterator[aiosqlite.Connection]:
    """
    Shared-Cache-URI: Erlaubt mehreren concurrent Connections Zugriff auf dieselbe
    In-Memory-Datenbank. Ideal für asynchrone Integrationstests.
    """
    connection = await aiosqlite.connect("file::memory:?cache=shared", uri=True)
    connection.row_factory = aiosqlite.Row

    # Wichtige PRAGMAs konfigurieren
    await connection.execute("PRAGMA foreign_keys=ON")

    # DDL aus allen Migrations-Dateien ausführen
    migrations_dir = Path("migrations")
    if migrations_dir.exists():
        for migrations_file in sorted(migrations_dir.glob("*.sql")):
            sql = migrations_file.read_text(encoding="utf-8")
            for stmt in sql.split(";"):
                stmt_clean = stmt.strip()
                if stmt_clean:
                    await connection.execute(stmt_clean)
        await connection.commit()

    yield connection
    await connection.close()


@pytest.fixture
def test_config() -> Config:
    """Erstellt eine Standard-Testkonfiguration ohne Abhängigkeit von einer lokalen config.toml."""
    tws = TwsConfig(
        host="127.0.0.1",
        port=7496,
        client_id=0,
        connection_timeout_s=10.0,
        reconnect_initial_delay_s=5.0,
        reconnect_max_attempts=10,
        reconnect_max_delay_s=120.0,
        request_timeout_s=10.0,
        completed_orders_timeout_s=15.0,
        heartbeat_interval_s=60.0,
        heartbeat_timeout_s=15.0,
    )
    app = AppConfig(
        max_retries=3,
        order_rate_limit_s=0.0,
        dead_order_threshold_minutes=15,
        alert_watcher_interval_s=60,
        csv_watcher_interval_s=60,
        order_sync_interval_s=1,
        retry_backoff_base_s=5.0,
        shutdown_join_timeout_s=15.0,
        database_timeout_s=30.0,
        max_csv_size_bytes=5242880,
        log_file_path="data/app.log",
        log_rotation_backup_count=5,
    )
    account = AccountConfig(
        default_limit_pct=0.05,
        margin_multiplier_factor=2.0,
        sizing_mode="margin_adjusted_capital",
        max_margin_usage_pct=0.80,
        min_cushion_pct=0.10,
    )
    telegram = TelegramConfig(
        bot_token="test_token",
        chat_id="test_chat",
        rate_limit_delay_s=0.0,
        request_timeout_s=10.0,
    )
    return Config(
        tws=tws, app=app, account=account, telegram=telegram, strategy_limits={}
    )
