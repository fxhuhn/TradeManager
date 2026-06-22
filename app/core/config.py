"""
Konfigurations-Verwaltung für das Trading-System.

Lädt globale Einstellungen und Parameter aus config.toml und Umgebungsvariablen
aus .env-Dateien in typsichere Konfigurations-Klassen.
"""

import os
import re

# Da tomllib ab Python 3.11 in der Standardbibliothek ist
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TwsConfig:
    """Konfiguration für die Verbindung zur Trader Workstation (TWS)."""

    host: str
    port: int
    client_id: int
    connection_timeout_s: float
    reconnect_initial_delay_s: float
    reconnect_max_attempts: int
    reconnect_max_delay_s: float
    request_timeout_s: float
    completed_orders_timeout_s: float
    heartbeat_interval_s: float = 60.0
    heartbeat_timeout_s: float = 15.0


@dataclass(frozen=True)
class AppConfig:
    """Konfiguration der Kernanwendung und Ausführungsparameter."""

    max_retries: int
    order_rate_limit_s: float
    dead_order_threshold_minutes: int
    alert_watcher_interval_s: int
    csv_watcher_interval_s: int
    order_sync_interval_s: int
    retry_backoff_base_s: float
    shutdown_join_timeout_s: float
    database_timeout_s: float
    max_csv_size_bytes: int
    log_file_path: str
    log_rotation_backup_count: int


@dataclass(frozen=True)
class AccountConfig:
    """Konto- und Kapitalallokationseinstellungen."""

    default_limit_pct: float
    margin_multiplier_factor: float = 2.0
    sizing_mode: str = "margin_adjusted_capital"
    max_margin_usage_pct: float = 0.80
    min_cushion_pct: float = 0.10


@dataclass(frozen=True)
class TelegramConfig:
    """Konfiguration für Telegram-Benachrichtigungen."""

    bot_token: str
    chat_id: str
    rate_limit_delay_s: float
    request_timeout_s: float


@dataclass(frozen=True)
class Config:
    """Zentrale Konfigurationsklasse für das gesamte Trading-System."""

    tws: TwsConfig
    app: AppConfig
    account: AccountConfig
    telegram: TelegramConfig
    strategy_limits: dict[str, float] = field(default_factory=dict)


VALID_ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def load_env(environment_path: Path) -> dict[str, str]:
    """Lädt Schlüssel-Wert-Paare aus einer .env-Datei."""
    environment_variables: dict[str, str] = {}
    if environment_path.exists():
        with open(environment_path, encoding="utf-8") as file_handle:
            for line_number, raw_line in enumerate(file_handle, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    if not VALID_ENV_KEY_PATTERN.match(key):
                        raise ValueError(
                            f"Ungueltiger Schluesselname '{key}' in .env (Zeile {line_number})"
                        )
                    value = value.strip().strip('"').strip("'")
                    environment_variables[key] = value
    return environment_variables


def _parse_tws_config(
    tws_data: dict[str, object], environment_variables: dict[str, str]
) -> TwsConfig:
    tws_host = (
        os.environ.get("TWS_HOST")
        or environment_variables.get("TWS_HOST")
        or str(tws_data.get("host", "127.0.0.1"))
    )

    tws_port_raw = (
        os.environ.get("TWS_PORT")
        or environment_variables.get("TWS_PORT")
        or tws_data.get("port")
    )
    tws_port = int(tws_port_raw) if tws_port_raw is not None else 7497

    tws_client_id_raw = (
        os.environ.get("TWS_CLIENT_ID")
        or environment_variables.get("TWS_CLIENT_ID")
        or tws_data.get("client_id")
    )
    tws_client_id = int(tws_client_id_raw) if tws_client_id_raw is not None else 0

    return TwsConfig(
        host=tws_host,
        port=tws_port,
        client_id=tws_client_id,
        connection_timeout_s=float(tws_data.get("connection_timeout_s", 10.0)),
        reconnect_initial_delay_s=float(tws_data.get("reconnect_initial_delay_s", 5.0)),
        reconnect_max_attempts=int(tws_data.get("reconnect_max_attempts", 10)),
        reconnect_max_delay_s=float(tws_data.get("reconnect_max_delay_s", 120.0)),
        request_timeout_s=float(tws_data.get("request_timeout_s", 10.0)),
        completed_orders_timeout_s=float(
            tws_data.get("completed_orders_timeout_s", 15.0)
        ),
        heartbeat_interval_s=float(tws_data.get("heartbeat_interval_s", 60.0)),
        heartbeat_timeout_s=float(tws_data.get("heartbeat_timeout_s", 15.0)),
    )


def _parse_app_config(app_data: dict[str, object]) -> AppConfig:
    return AppConfig(
        max_retries=int(app_data.get("max_retries", 3)),
        order_rate_limit_s=float(app_data.get("order_rate_limit_s", 0.02)),
        dead_order_threshold_minutes=int(
            app_data.get("dead_order_threshold_minutes", 15)
        ),
        alert_watcher_interval_s=int(app_data.get("alert_watcher_interval_s", 60)),
        csv_watcher_interval_s=int(app_data.get("csv_watcher_interval_s", 60)),
        order_sync_interval_s=int(app_data.get("order_sync_interval_s", 300)),
        retry_backoff_base_s=float(app_data.get("retry_backoff_base_s", 5.0)),
        shutdown_join_timeout_s=float(app_data.get("shutdown_join_timeout_s", 15.0)),
        database_timeout_s=float(app_data.get("database_timeout_s", 30.0)),
        max_csv_size_bytes=int(app_data.get("max_csv_size_bytes", 5242880)),
        log_file_path=str(app_data.get("log_file_path", "data/app.log")),
        log_rotation_backup_count=int(app_data.get("log_rotation_backup_count", 5)),
    )


def _parse_account_config(account_data: dict[str, object]) -> AccountConfig:
    account_config = AccountConfig(
        default_limit_pct=float(account_data.get("default_limit_pct", 0.05)),
        margin_multiplier_factor=float(
            account_data.get("margin_multiplier_factor", 2.0)
        ),
        sizing_mode=str(account_data.get("sizing_mode", "margin_adjusted_capital")),
        max_margin_usage_pct=float(account_data.get("max_margin_usage_pct", 0.80)),
        min_cushion_pct=float(account_data.get("min_cushion_pct", 0.10)),
    )

    if account_config.sizing_mode not in ("margin_adjusted_capital", "total_cash"):
        raise ValueError(
            f"Ungueltiger sizing_mode: {account_config.sizing_mode}. "
            "Erlaubt sind 'margin_adjusted_capital' oder 'total_cash'."
        )
    if account_config.margin_multiplier_factor <= 0.0:
        raise ValueError("margin_multiplier_factor muss groesser als 0 sein.")
    if not (0.0 <= account_config.max_margin_usage_pct <= 1.0):
        raise ValueError("max_margin_usage_pct muss zwischen 0.0 und 1.0 liegen.")
    if not (0.0 <= account_config.min_cushion_pct <= 1.0):
        raise ValueError("min_cushion_pct muss zwischen 0.0 und 1.0 liegen.")

    return account_config


def _parse_telegram_config(
    telegram_data: dict[str, object], environment_variables: dict[str, str]
) -> TelegramConfig:
    telegram_bot_token = os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    ) or environment_variables.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID") or environment_variables.get(
        "TELEGRAM_CHAT_ID", ""
    )

    return TelegramConfig(
        bot_token=telegram_bot_token,
        chat_id=telegram_chat_id,
        rate_limit_delay_s=float(telegram_data.get("rate_limit_delay_s", 1.5)),
        request_timeout_s=float(telegram_data.get("request_timeout_s", 10.0)),
    )


def load_config(root_path: Path = Path(".")) -> Config:
    """Lädt die Konfiguration aus config.toml und .env."""
    config_toml_path = root_path / "config.toml"
    environment_path = root_path / ".env"

    if not config_toml_path.exists():
        raise FileNotFoundError(
            f"config.toml nicht gefunden unter: {config_toml_path.absolute()}"
        )

    # Laden der TOML-Konfiguration
    with open(config_toml_path, "rb") as file_handle:
        toml_data = tomllib.load(file_handle)

    environment_variables = load_env(environment_path)
    tws_config = _parse_tws_config(toml_data.get("tws", {}), environment_variables)
    app_config = _parse_app_config(toml_data.get("app", {}))
    account_config = _parse_account_config(toml_data.get("account", {}))

    telegram_config = _parse_telegram_config(
        toml_data.get("telegram", {}), environment_variables
    )

    strategy_limits = toml_data.get("strategy_limits", {})
    typed_strategy_limits = {
        strategy_name: float(limit_value)
        for strategy_name, limit_value in strategy_limits.items()
    }

    return Config(
        tws=tws_config,
        app=app_config,
        account=account_config,
        telegram=telegram_config,
        strategy_limits=typed_strategy_limits,
    )
