from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from app.core.config import (
    AccountConfig,
    AppConfig,
    Config,
    FuturesConfig,
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
        docker_socket_path="/nonexistent/docker.sock",
    )
    futures = FuturesConfig(
        asset_mapping={"QQQ": "MNQ", "SPY": "MES", "IWM": "M2K", "DIA": "MYM"},
        enabled_strategies=("bouncebandit", "spxtrend"),
    )
    return Config(
        tws=tws,
        app=app,
        account=account,
        telegram=telegram,
        futures=futures,
        strategy_limits={},
    )


SAMPLE_ANONYMOUS_FLEX_XML = """<FlexQueryResponse queryName="Anonymous Sample Flex" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="DU123456" fromDate="20260101" toDate="20260917" period="YearToDate" whenGenerated="20260918;093919">
<CashTransactions>
<CashTransaction accountId="DU123456" dateTime="20260707" type="Withholding Tax" description="WITHHOLDING @ 20% ON CREDIT INT" symbol="" amount="-1.11" currency="EUR" fxRateToBase="1" />
<CashTransaction accountId="DU123456" dateTime="20260130;202000" type="Dividends" description="MAA CASH DIVIDEND" symbol="MAA" amount="47.43" currency="USD" fxRateToBase="0.84387" />
<CashTransaction accountId="DU123456" dateTime="20260909;170130" type="Other Fees" description="CME (GLOBEX) FOR SEP 2026" symbol="" amount="-1.33" currency="EUR" fxRateToBase="1" />
<CashTransaction accountId="DU123456" dateTime="20260106" type="Broker Interest Paid" description="USD DEBIT INT FOR DEC-2025" symbol="" amount="-3.29" currency="USD" fxRateToBase="0.85555" />
<CashTransaction accountId="DU123456" dateTime="20260706" type="Broker Interest Received" description="EUR CREDIT INT FOR JUN-2026" symbol="" amount="5.57" currency="EUR" fxRateToBase="1" />
</CashTransactions>
<InterestAccruals>
<InterestAccrualsCurrency accountId="DU123456" currency="EUR" fromDate="20260101" toDate="20260917" startingAccrualBalance="0" interestAccrued="5.93" accrualReversal="-5.93" endingAccrualBalance="0" />
</InterestAccruals>
<HardToBorrowDetails>
<HardToBorrowDetail accountId="DU123456" valueDate="20260521" symbol="SAIC" description="SCIENCE APPLICATIONS INTE" quantity="30" borrowFeeRate="0.3804" borrowFee="-0.03" currency="USD" />
<HardToBorrowDetail accountId="DU123456" valueDate="20260522" symbol="SAIC" description="SCIENCE APPLICATIONS INTE" quantity="30" borrowFeeRate="0.4181" borrowFee="-0.03" currency="USD" />
<HardToBorrowDetail accountId="DU123456" valueDate="20260523" symbol="SAIC" description="SCIENCE APPLICATIONS INTE" quantity="30" borrowFeeRate="0.4181" borrowFee="-0.03" currency="USD" />
<HardToBorrowDetail accountId="DU123456" valueDate="20260524" symbol="SAIC" description="SCIENCE APPLICATIONS INTE" quantity="30" borrowFeeRate="0.4181" borrowFee="-0.03" currency="USD" />
<HardToBorrowDetail accountId="DU123456" valueDate="20260525" symbol="SAIC" description="SCIENCE APPLICATIONS INTE" quantity="30" borrowFeeRate="0.4181" borrowFee="-0.03" currency="USD" />
</HardToBorrowDetails>
<OpenDividendAccruals>
<OpenDividendAccrual accountId="DU123456" symbol="NVDA" description="NVIDIA CORP" exDate="20260910" payDate="20261001" quantity="13" grossRate="0.25" grossAmount="3.25" tax="0.49" fee="0" netAmount="2.76" currency="USD" />
</OpenDividendAccruals>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>
"""


@pytest.fixture
def sample_flex_xml() -> str:
    """Gibt das synthetische anonymisierte Flex-Statement XML zurück."""
    return SAMPLE_ANONYMOUS_FLEX_XML
