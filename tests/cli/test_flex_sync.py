"""
Unit-Tests für das CLI-Tool app/cli/flex_sync.py.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from app.cli.flex_sync import format_flex_sync_report, run_flex_sync
from app.services.flex_query.service import ReconciliationReport


def test_format_flex_sync_report() -> None:
    report = ReconciliationReport(
        account_id="DU123456",
        from_date="2026-05-01",
        to_date="2026-09-15",
        total_parsed=42,
        inserted_count=35,
        skipped_duplicate_count=7,
        allocated_to_trades_count=10,
        account_level_count=25,
        total_trade_adjustments_base=Decimal("-12.50"),
        total_account_expenses_base=Decimal("-45.80"),
    )

    formatted = format_flex_sync_report(report)
    assert "IBKR FLEX QUERY RECONCILIATION BERICHT" in formatted
    assert "DU123456" in formatted
    assert "42" in formatted
    assert "35" in formatted
    assert "7" in formatted
    assert "$ -12.50" in formatted


@pytest.mark.asyncio
async def test_run_flex_sync_with_file(tmp_path: Path, sample_flex_xml: str) -> None:
    # Minimal config.toml in tmp_path
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[tws]
host = "127.0.0.1"
port = 7497
client_id = 1
connection_timeout_s = 5.0
reconnect_initial_delay_s = 1.0
reconnect_max_attempts = 1
reconnect_max_delay_s = 10.0
request_timeout_s = 5.0
completed_orders_timeout_s = 5.0

[app]
max_retries = 1
order_rate_limit_s = 0.02
dead_order_threshold_minutes = 15
alert_watcher_interval_s = 60
csv_watcher_interval_s = 60
order_sync_interval_s = 300
retry_backoff_base_s = 1.0
shutdown_join_timeout_s = 5.0
database_timeout_s = 5.0
max_csv_size_bytes = 1024
log_file_path = "data/logs/app.log"
log_rotation_backup_count = 1

[account]
default_limit_pct = 0.05

[telegram]
rate_limit_delay_s = 1.0
request_timeout_s = 5.0
ibkr_container_name = "ibkr"
docker_socket_path = "/var/run/docker.sock"
enable_commands = false
        """,
        encoding="utf-8",
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    sample_file = tmp_path / "statement.xml"
    sample_file.write_text(sample_flex_xml, encoding="utf-8")

    report = await run_flex_sync(
        file_path=sample_file,
        root_path=tmp_path,
    )

    assert report.account_id == "DU123456"
    assert report.inserted_count > 0


@pytest.mark.asyncio
async def test_run_flex_sync_with_notify(tmp_path: Path, sample_flex_xml: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[tws]
host = "127.0.0.1"
port = 7497
client_id = 1
connection_timeout_s = 5.0
reconnect_initial_delay_s = 1.0
reconnect_max_attempts = 1
reconnect_max_delay_s = 10.0
request_timeout_s = 5.0
completed_orders_timeout_s = 5.0

[app]
max_retries = 1
order_rate_limit_s = 0.02
dead_order_threshold_minutes = 15
alert_watcher_interval_s = 60
csv_watcher_interval_s = 60
order_sync_interval_s = 300
retry_backoff_base_s = 1.0
shutdown_join_timeout_s = 5.0
database_timeout_s = 5.0
max_csv_size_bytes = 1024
log_file_path = "data/logs/app.log"
log_rotation_backup_count = 1

[account]
default_limit_pct = 0.05

[telegram]
bot_token = "TEST_TOKEN"
chat_id = "12345"
rate_limit_delay_s = 1.0
request_timeout_s = 5.0
ibkr_container_name = "ibkr"
docker_socket_path = "/var/run/docker.sock"
enable_commands = false
        """,
        encoding="utf-8",
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=12345:ABC\nTELEGRAM_CHAT_ID=12345\n", encoding="utf-8"
    )

    sample_file = tmp_path / "statement.xml"
    sample_file.write_text(sample_flex_xml, encoding="utf-8")

    from unittest.mock import AsyncMock, patch

    with patch(
        "app.services.notifier.TelegramNotifier.send_flex_reconciliation_summary",
        new_callable=AsyncMock,
    ) as mock_send:
        report = await run_flex_sync(
            file_path=sample_file,
            notify=True,
            root_path=tmp_path,
        )
        assert report.account_id == "DU123456"
        mock_send.assert_awaited_once()
