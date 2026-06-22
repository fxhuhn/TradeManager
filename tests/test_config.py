"""Tests for TWS configuration parsing and environment variable overrides."""

import os
from pathlib import Path
from unittest.mock import patch

from app.core.config import load_config


def test_config_parsing_from_toml(tmp_path: Path) -> None:
    """Verifies that TWS configuration is parsed correctly from config.toml."""
    config_content = """
    [tws]
    host = "10.0.0.1"
    port = 9999
    client_id = 42
    connection_timeout_s = 5.0
    reconnect_initial_delay_s = 2.0
    reconnect_max_attempts = 5
    reconnect_max_delay_s = 60.0
    request_timeout_s = 5.0
    completed_orders_timeout_s = 10.0
    heartbeat_interval_s = 30.0
    heartbeat_timeout_s = 10.0

    [app]
    max_retries = 3
    order_rate_limit_s = 0.02
    dead_order_threshold_minutes = 15
    alert_watcher_interval_s = 60
    csv_watcher_interval_s = 60
    order_sync_interval_s = 300
    retry_backoff_base_s = 5.0
    shutdown_join_timeout_s = 15.0
    database_timeout_s = 30.0
    max_csv_size_bytes = 5242880
    log_file_path = "data/app.log"
    log_rotation_backup_count = 5

    [account]
    default_limit_pct = 0.05
    margin_multiplier_factor = 2.0
    sizing_mode = "margin_adjusted_capital"
    max_margin_usage_pct = 0.80
    min_cushion_pct = 0.10
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content, encoding="utf-8")

    # Ensure no TWS env vars are present during this test
    with patch.dict(os.environ, {}, clear=False):
        # Remove TWS env keys from os.environ if present to avoid leaking
        for key in ["TWS_HOST", "TWS_PORT", "TWS_CLIENT_ID"]:
            os.environ.pop(key, None)

        config = load_config(tmp_path)

        assert config.tws.host == "10.0.0.1"
        assert config.tws.port == 9999
        assert config.tws.client_id == 42


def test_config_parsing_env_overrides(tmp_path: Path) -> None:
    """Verifies TWS configuration properties are overridden by environment variables."""
    config_content = """
    [tws]
    host = "10.0.0.1"
    port = 9999
    client_id = 42
    connection_timeout_s = 5.0
    reconnect_initial_delay_s = 2.0
    reconnect_max_attempts = 5
    reconnect_max_delay_s = 60.0
    request_timeout_s = 5.0
    completed_orders_timeout_s = 10.0
    heartbeat_interval_s = 30.0
    heartbeat_timeout_s = 10.0

    [app]
    max_retries = 3
    order_rate_limit_s = 0.02
    dead_order_threshold_minutes = 15
    alert_watcher_interval_s = 60
    csv_watcher_interval_s = 60
    order_sync_interval_s = 300
    retry_backoff_base_s = 5.0
    shutdown_join_timeout_s = 15.0
    database_timeout_s = 30.0
    max_csv_size_bytes = 5242880
    log_file_path = "data/app.log"
    log_rotation_backup_count = 5

    [account]
    default_limit_pct = 0.05
    margin_multiplier_factor = 2.0
    sizing_mode = "margin_adjusted_capital"
    max_margin_usage_pct = 0.80
    min_cushion_pct = 0.10
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content, encoding="utf-8")

    env_overrides = {
        "TWS_HOST": "192.168.1.100",
        "TWS_PORT": "8888",
        "TWS_CLIENT_ID": "7",
    }

    with patch.dict(os.environ, env_overrides):
        config = load_config(tmp_path)

        assert config.tws.host == "192.168.1.100"
        assert config.tws.port == 8888
        assert config.tws.client_id == 7


def test_config_parsing_dotenv_overrides(tmp_path: Path) -> None:
    """Verifies TWS configuration properties are overridden by values in .env."""
    config_content = """
    [tws]
    host = "10.0.0.1"
    port = 9999
    client_id = 42
    connection_timeout_s = 5.0
    reconnect_initial_delay_s = 2.0
    reconnect_max_attempts = 5
    reconnect_max_delay_s = 60.0
    request_timeout_s = 5.0
    completed_orders_timeout_s = 10.0
    heartbeat_interval_s = 30.0
    heartbeat_timeout_s = 10.0

    [app]
    max_retries = 3
    order_rate_limit_s = 0.02
    dead_order_threshold_minutes = 15
    alert_watcher_interval_s = 60
    csv_watcher_interval_s = 60
    order_sync_interval_s = 300
    retry_backoff_base_s = 5.0
    shutdown_join_timeout_s = 15.0
    database_timeout_s = 30.0
    max_csv_size_bytes = 5242880
    log_file_path = "data/app.log"
    log_rotation_backup_count = 5

    [account]
    default_limit_pct = 0.05
    margin_multiplier_factor = 2.0
    sizing_mode = "margin_adjusted_capital"
    max_margin_usage_pct = 0.80
    min_cushion_pct = 0.10
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content, encoding="utf-8")

    dotenv_content = """
    TWS_HOST=172.16.1.5
    TWS_PORT=5000
    TWS_CLIENT_ID=3
    """
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(dotenv_content, encoding="utf-8")

    with patch.dict(os.environ, {}, clear=False):
        for key in ["TWS_HOST", "TWS_PORT", "TWS_CLIENT_ID"]:
            os.environ.pop(key, None)

        config = load_config(tmp_path)

        assert config.tws.host == "172.16.1.5"
        assert config.tws.port == 5000
        assert config.tws.client_id == 3
