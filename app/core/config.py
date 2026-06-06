"""
Konfigurations-Verwaltung für das Trading-System.

Lädt globale Einstellungen und Parameter aus config.toml und Umgebungsvariablen
aus .env-Dateien in typsichere Konfigurations-Klassen.
"""

import os

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


@dataclass(frozen=True)
class AppConfig:
    """Konfiguration der Kernanwendung und Ausführungsparameter."""

    max_retries: int
    order_rate_limit_s: float
    dead_order_threshold_min: int
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


def load_env(environment_path: Path) -> dict[str, str]:
    """Lädt Schlüssel-Wert-Paare aus einer .env-Datei."""
    environment_variables: dict[str, str] = {}
    if environment_path.exists():
        with open(environment_path, encoding="utf-8") as file_handle:
            for raw_line in file_handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    environment_variables[key] = value
    return environment_variables


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

    tws_data = toml_data.get("tws", {})
    app_data = toml_data.get("app", {})
    account_data = toml_data.get("account", {})
    strategy_limits = toml_data.get("strategy_limits", {})
    telegram_data = toml_data.get("telegram", {})

    tws_config = TwsConfig(
        host=tws_data.get("host", "127.0.0.1"),
        port=int(tws_data.get("port", 7497)),
        client_id=int(tws_data.get("client_id", 0)),
        connection_timeout_s=float(tws_data.get("connection_timeout_s", 10.0)),
        reconnect_initial_delay_s=float(tws_data.get("reconnect_initial_delay_s", 5.0)),
        reconnect_max_attempts=int(tws_data.get("reconnect_max_attempts", 10)),
        reconnect_max_delay_s=float(tws_data.get("reconnect_max_delay_s", 120.0)),
        request_timeout_s=float(tws_data.get("request_timeout_s", 10.0)),
        completed_orders_timeout_s=float(
            tws_data.get("completed_orders_timeout_s", 15.0)
        ),
    )

    app_config = AppConfig(
        max_retries=int(app_data.get("max_retries", 3)),
        order_rate_limit_s=float(app_data.get("order_rate_limit_s", 0.02)),
        dead_order_threshold_min=int(app_data.get("dead_order_threshold_min", 15)),
        alert_watcher_interval_s=int(app_data.get("alert_watcher_interval_s", 60)),
        csv_watcher_interval_s=int(app_data.get("csv_watcher_interval_s", 60)),
        order_sync_interval_s=int(app_data.get("order_sync_interval_s", 300)),
        retry_backoff_base_s=float(app_data.get("retry_backoff_base_s", 5.0)),
        shutdown_join_timeout_s=float(app_data.get("shutdown_join_timeout_s", 15.0)),
        database_timeout_s=float(app_data.get("database_timeout_s", 30.0)),
        max_csv_size_bytes=int(app_data.get("max_csv_size_bytes", 5242880)),
        log_file_path=app_data.get("log_file_path", "data/app.log"),
        log_rotation_backup_count=int(app_data.get("log_rotation_backup_count", 5)),
    )

    account_config = AccountConfig(
        default_limit_pct=float(account_data.get("default_limit_pct", 0.05))
    )

    # Laden der .env Variablen
    environment_variables = load_env(environment_path)

    # Prämisse: Reale Umgebungsvariablen haben Vorrang vor der .env-Datei
    telegram_bot_token = os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    ) or environment_variables.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID") or environment_variables.get(
        "TELEGRAM_CHAT_ID", ""
    )

    telegram_config = TelegramConfig(
        bot_token=telegram_bot_token,
        chat_id=telegram_chat_id,
        rate_limit_delay_s=float(telegram_data.get("rate_limit_delay_s", 1.5)),
        request_timeout_s=float(telegram_data.get("request_timeout_s", 10.0)),
    )

    # Strategy limits dict keys must be typed float
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
