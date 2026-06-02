import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.services.importer import csv_directory_watcher


@pytest.fixture
def mock_config() -> Config:
    """Erstellt ein Mock-Konfigurationsobjekt fuer die Tests."""
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
    )
    app = AppConfig(
        max_retries=3,
        order_rate_limit_s=0.02,
        dead_order_threshold_min=15,
        alert_watcher_interval_s=60,
        csv_watcher_interval_s=1,  # Schnelles Intervall fuer Tests
        order_sync_interval_s=300,
        retry_backoff_base_s=5.0,
        shutdown_join_timeout_s=15.0,
        database_timeout_s=30.0,
        max_csv_size_bytes=5242880,
        log_file_path="data/app.log",
        log_rotation_backup_count=5,
    )
    account = AccountConfig(default_limit_pct=0.05)
    telegram = TelegramConfig(
        bot_token="test_token",
        chat_id="test_chat",
        rate_limit_delay_s=1.5,
        request_timeout_s=10.0,
    )
    return Config(
        tws=tws, app=app, account=account, telegram=telegram, strategy_limits={}
    )


@pytest.mark.asyncio
async def test_csv_directory_watcher_success_rename(
    tmp_path: Path, mock_config: Config
) -> None:
    """
    Prueft, dass der Watcher eine neue orders_YYYY_MM_DD.csv erkennt,
    verarbeitet und nach .csv.bak umbenennt.
    """
    # 1. Temporaeres Verzeichnis und Test-Datei anlegen
    data_directory = tmp_path / "data"
    data_directory.mkdir()

    test_csv = data_directory / "orders_2026_06_01.csv"
    test_csv.write_text("dummy,content", encoding="utf-8")

    # 2. Mocks erstellen
    mock_db_conn = AsyncMock()
    mock_db_conn.close = AsyncMock()

    async def db_factory():
        return mock_db_conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    # 3. run_csv_import mocken (simuliert erfolgreichen Import)
    with patch(
        "app.services.importer.run_csv_import", new_callable=AsyncMock
    ) as mock_import:
        # Start des Watchers in einem Hintergrund-Task
        watcher_task = asyncio.create_task(
            csv_directory_watcher(
                db_factory=db_factory,
                ib=mock_ib,
                directory_path=data_directory,
                queue=mock_queue,
                notifier=mock_notifier,
                config=mock_config,
                interval_seconds=1,  # Kurzer Sleep fuer Testdurchlauf
            )
        )

        # Dem Watcher Zeit geben, die Datei zu finden und zu verarbeiten
        await asyncio.sleep(1.5)
        watcher_task.cancel()

        try:
            await watcher_task
        except asyncio.CancelledError:
            pass

        # 4. Assertions
        mock_import.assert_called_once()

        # Die Original-Datei sollte nicht mehr existieren
        assert not test_csv.exists()

        # Die Backup-Datei sollte existieren
        backup_csv = data_directory / "orders_2026_06_01.csv.bak"
        assert backup_csv.exists()
        assert backup_csv.read_text(encoding="utf-8") == "dummy,content"


@pytest.mark.asyncio
async def test_csv_directory_watcher_error_rename(
    tmp_path: Path, mock_config: Config
) -> None:
    """
    Prueft, dass der Watcher bei einem Importfehler die Datei
    nach .csv.err umbenennt und eine Fehlermeldung versendet.
    """
    # 1. Temporaeres Verzeichnis und Test-Datei anlegen
    data_directory = tmp_path / "data"
    data_directory.mkdir()

    test_csv = data_directory / "orders_2026_06_01.csv"
    test_csv.write_text("invalid,content", encoding="utf-8")

    # 2. Mocks erstellen
    mock_db_conn = AsyncMock()
    mock_db_conn.close = AsyncMock()

    async def db_factory():
        return mock_db_conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    # 3. run_csv_import mocken, so dass es eine Exception wirft
    with patch(
        "app.services.importer.run_csv_import", new_callable=AsyncMock
    ) as mock_import:
        mock_import.side_effect = ValueError("Sizing ergab Qty <= 0")

        # Start des Watchers in einem Hintergrund-Task
        watcher_task = asyncio.create_task(
            csv_directory_watcher(
                db_factory=db_factory,
                ib=mock_ib,
                directory_path=data_directory,
                queue=mock_queue,
                notifier=mock_notifier,
                config=mock_config,
                interval_seconds=1,
            )
        )

        await asyncio.sleep(1.5)
        watcher_task.cancel()

        try:
            await watcher_task
        except asyncio.CancelledError:
            pass

        # 4. Assertions
        mock_import.assert_called_once()

        # Die Original-Datei sollte nicht mehr existieren
        assert not test_csv.exists()

        # Die Fehler-Datei sollte existieren
        error_csv = data_directory / "orders_2026_06_01.csv.err"
        assert error_csv.exists()

        # Notifier sollte alarmiert haben
        mock_notifier.send_message.assert_called_once()
        notification_text = mock_notifier.send_message.call_args[0][0]
        assert "IMPORT-FEHLER" in notification_text
        assert "Sizing ergab Qty" in notification_text
