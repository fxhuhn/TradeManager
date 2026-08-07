# filename: tests/services/test_importer.py
import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ib_async import AccountValue

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.services.importer import (
    csv_directory_watcher,
    determine_maximum_capital_allocation,
    fetch_account_balance_metrics,
    resolve_account_id,
    run_csv_import,
)


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
        dead_order_threshold_minutes=15,
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


def test_determine_maximum_capital_allocation_total_cash() -> None:
    """Prüft, dass bei sizing_mode 'total_cash' nur der TotalCashValue zurückgegeben wird."""
    allocation = determine_maximum_capital_allocation(
        net_liquidation_value=Decimal("100000.0"),
        available_funds_value=Decimal("50000.0"),
        total_cash_value=Decimal("40000.0"),
        margin_multiplier_factor=Decimal("2.0"),
        sizing_mode="total_cash",
        allocation_limit_percentage=Decimal("0.05"),
    )
    assert allocation == Decimal("40000.0")


def test_determine_maximum_capital_allocation_margin_adjusted() -> None:
    """Prüft, dass bei margin_adjusted_capital das theoretische Limit (NLV * Margin * Limit) greift."""
    allocation = determine_maximum_capital_allocation(
        net_liquidation_value=Decimal("100000.0"),
        available_funds_value=Decimal("50000.0"),
        total_cash_value=Decimal("40000.0"),
        margin_multiplier_factor=Decimal("2.0"),
        sizing_mode="margin_adjusted_capital",
        allocation_limit_percentage=Decimal("0.05"),
    )
    # 100000 * 2.0 * 0.05 = 10000. Capped by available_funds * 2.0 = 100000.
    assert allocation == Decimal("10000.0")


def test_determine_maximum_capital_allocation_margin_adjusted_limited_by_funds() -> (
    None
):
    """Prüft, dass bei unzureichendem AvailableFunds das Limit durch AvailableFunds * Margin gedeckelt wird."""
    allocation = determine_maximum_capital_allocation(
        net_liquidation_value=Decimal("100000.0"),
        available_funds_value=Decimal("10000.0"),
        total_cash_value=Decimal("5000.0"),
        margin_multiplier_factor=Decimal("2.0"),
        sizing_mode="margin_adjusted_capital",
        allocation_limit_percentage=Decimal("0.50"),
    )
    # 100000 * 2.0 * 0.50 = 100000. Capped by available_funds * 2.0 = 20000.
    assert allocation == Decimal("20000.0")


@pytest.mark.asyncio
async def test_fetch_account_balance_metrics_from_cache() -> None:
    """Prüft das Laden der Kontowerte aus dem Cache."""
    mock_ib = MagicMock()
    mock_ib.accountValues.return_value = [
        AccountValue(
            account="U123",
            tag="NetLiquidation",
            value="80000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U123",
            tag="AvailableFunds",
            value="50000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U123",
            tag="TotalCashValue",
            value="35000.00",
            currency="EUR",
            modelCode="",
        ),
    ]

    metrics = await fetch_account_balance_metrics(mock_ib, "U123")
    assert metrics.net_liquidation_value == Decimal("80000.00")
    assert metrics.available_funds_value == Decimal("50000.00")
    assert metrics.total_cash_value == Decimal("35000.00")
    mock_ib.reqAccountSummary.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_account_balance_metrics_from_summary() -> None:
    """Prüft das Laden der Kontowerte per Fallback via accountSummaryAsync."""
    mock_ib = MagicMock()
    # Leerer Cache
    mock_ib.accountValues.return_value = []

    # Mock für accountSummaryAsync
    async def mock_account_summary(account: str = "") -> list[AccountValue]:
        return [
            AccountValue(
                account="U123",
                tag="NetLiquidation",
                value="90000.0",
                currency="EUR",
                modelCode="",
            ),
            AccountValue(
                account="U123",
                tag="AvailableFunds",
                value="60000.0",
                currency="EUR",
                modelCode="",
            ),
            AccountValue(
                account="U123",
                tag="TotalCashValue",
                value="40000.0",
                currency="EUR",
                modelCode="",
            ),
        ]

    mock_ib.accountSummaryAsync = mock_account_summary

    metrics = await fetch_account_balance_metrics(mock_ib, "U123")
    assert metrics.net_liquidation_value == Decimal("90000.0")
    assert metrics.available_funds_value == Decimal("60000.0")
    assert metrics.total_cash_value == Decimal("40000.0")


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

    mock_interactive_brokers = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    # 3. run_csv_import mocken (simuliert erfolgreichen Import)
    with patch(
        "app.services.importer.run_csv_import", new_callable=AsyncMock
    ) as mock_import:
        # Start des Watchers in einem Hintergrund-Task
        watcher_task = asyncio.create_task(
            csv_directory_watcher(
                db_factory=db_factory,
                interactive_brokers=mock_interactive_brokers,
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
        backup_csv = data_directory / "archive" / "orders_2026_06_01.csv.bak"
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

    mock_interactive_brokers = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
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
                interactive_brokers=mock_interactive_brokers,
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
        error_csv = data_directory / "archive" / "orders_2026_06_01.csv.err"
        assert error_csv.exists()

        # Notifier sollte alarmiert haben
        mock_notifier.send_importer_info.assert_called_once()
        kwargs = mock_notifier.send_importer_info.call_args[1]
        assert kwargs["title"] == "IMPORT-FEHLER"
        assert "Sizing ergab Qty" in kwargs["details"]


@pytest.mark.asyncio
async def test_csv_directory_watcher_disconnected_postpones(
    tmp_path: Path, mock_config: Config
) -> None:
    """
    Prüft, dass der Watcher bei TWS-Verbindungsverlust das Einlesen verschiebt,
    die Datei nicht anfasst und nur einmalig alarmiert.
    """
    # 1. Temporäres Verzeichnis und Test-Datei anlegen
    data_directory = tmp_path / "data"
    data_directory.mkdir()

    test_csv = data_directory / "orders_2026_06_01.csv"
    test_csv.write_text("dummy,content", encoding="utf-8")

    # 2. Mocks erstellen
    mock_db_conn = AsyncMock()
    mock_db_conn.close = AsyncMock()

    async def db_factory():
        return mock_db_conn

    # Verbindung als getrennt simulieren
    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.isConnected.return_value = False

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    # 3. Import-Aufruf mocken
    with patch(
        "app.services.importer.run_csv_import", new_callable=AsyncMock
    ) as mock_import:
        # Start des Watchers mit kurzem Intervall von 1 Sekunde
        watcher_task = asyncio.create_task(
            csv_directory_watcher(
                db_factory=db_factory,
                interactive_brokers=mock_interactive_brokers,
                directory_path=data_directory,
                queue=mock_queue,
                notifier=mock_notifier,
                config=mock_config,
                interval_seconds=1,
            )
        )

        # 2.5 Sekunden warten (der Loop läuft ca. 2 bis 3 Mal)
        await asyncio.sleep(2.5)
        watcher_task.cancel()

        try:
            await watcher_task
        except asyncio.CancelledError:
            pass

        # 4. Assertions
        # Import darf niemals aufgerufen worden sein
        mock_import.assert_not_called()

        # Die Datei darf weder gelöscht noch umbenannt sein
        assert test_csv.exists()

        # Notifier darf nur exakt EINMAL alarmiert haben (Throttling)
        mock_notifier.send_importer_info.assert_called_once()

        # Details der gesendeten Meldung prüfen
        kwargs = mock_notifier.send_importer_info.call_args[1]
        assert kwargs["title"] == "IMPORT PAUSIERT"
        assert kwargs["status"] == "Wartend"
        assert kwargs["emoji"] == "⏳"


@pytest.mark.asyncio
async def test_run_csv_import_handles_standalone_exit_gracefully(
    tmp_path: Path, mock_config: Config, db
) -> None:
    """Prüft, dass run_csv_import bei einem Standalone-Exit die Exception abfängt und fortfährt."""
    csv_file = tmp_path / "orders_2026_07_06.csv"
    # Ein Standalone Exit (kein ENTRY in DB)
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "974_DipBuyer_STLD,EXIT,STLD,STK,SMART,U19605236,SELL,27,LMT,227.46,DAY,DipBuyer\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.managedAccounts.return_value = ["U19605236"]
    mock_interactive_brokers.isConnected.return_value = True

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    from datetime import datetime as real_datetime

    # Mock datetime to a Monday (July 6, 2026) to make the test weekday-independent
    mock_now = real_datetime(2026, 7, 6, 12, 0, 0)
    with patch("app.services.importer.datetime") as mock_datetime:
        mock_datetime.side_effect = real_datetime
        mock_datetime.now.return_value = mock_now

        # run_csv_import aufrufen
        await run_csv_import(
            db=db,
            interactive_brokers=mock_interactive_brokers,
            csv_path=csv_file,
            queue=mock_queue,
            notifier=mock_notifier,
            config=mock_config,
        )

    # Verifizieren, dass der Notifier über das fehlgeschlagene Standalone Exit informiert wurde
    mock_notifier.send_importer_info.assert_called_once()
    kwargs = mock_notifier.send_importer_info.call_args[1]
    assert kwargs["status"] == "Fehlgeschlagen"
    assert "Standalone exit order" in kwargs["details"]


@pytest.mark.asyncio
async def test_run_csv_import_sends_telegram_on_downscaling(
    tmp_path: Path, mock_config: Config, db
) -> None:
    """Verifies that run_csv_import triggers a downscaling alert if capital sizing limits the quantity."""
    csv_file = tmp_path / "orders_2026_07_06.csv"
    # An entry with high quantity (100) and target price 100.0. Total cost = 10,000.00.
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "1001_DipBuyer_GLW,ENTRY,GLW,STK,SMART,U19605236,BUY,100,LMT,100.00,DAY,DipBuyer\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    # Mock TWS so capital sizing limits allocation to e.g. 5,000.00 (which will downscale 100 to 50)
    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.managedAccounts.return_value = ["U19605236"]
    mock_interactive_brokers.isConnected.return_value = True

    # 5% of 100,000 NLV = 5,000 allocation.
    mock_interactive_brokers.accountValues.return_value = [
        AccountValue(
            account="U19605236",
            tag="NetLiquidation",
            value="50000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U19605236",
            tag="AvailableFunds",
            value="80000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U19605236",
            tag="TotalCashValue",
            value="60000.00",
            currency="EUR",
            modelCode="",
        ),
    ]

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    from datetime import datetime as real_datetime

    # Mock datetime to a Monday (July 6, 2026) to make the test weekday-independent
    mock_now = real_datetime(2026, 7, 6, 12, 0, 0)
    with patch("app.services.importer.datetime") as mock_datetime:
        mock_datetime.side_effect = real_datetime
        mock_datetime.now.return_value = mock_now

        await run_csv_import(
            db=db,
            interactive_brokers=mock_interactive_brokers,
            csv_path=csv_file,
            queue=mock_queue,
            notifier=mock_notifier,
            config=mock_config,
        )

    # Check that a notification for downscaling was sent
    mock_notifier.send_importer_info.assert_called_once()
    kwargs = mock_notifier.send_importer_info.call_args[1]
    assert kwargs["status"] == "Menge Reduziert"
    assert kwargs["title"] == "KAPITAL-SIZING"
    assert "von 100 auf 50 Stück" in kwargs["details"]


@pytest.mark.asyncio
async def test_run_csv_import_aligns_standalone_exit_quantity(
    tmp_path: Path, mock_config: Config, db
) -> None:
    """Verifies that a standalone exit quantity is aligned with the existing ENTRY order quantity in the DB."""
    # 1. Insert an existing ENTRY order with quantity 5 (e.g. downscaled from 6)
    await db.execute(
        """
        INSERT INTO orders (order_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status, retry_count)
        VALUES (123, NULL, '1028_TwoPercent_SXRV.DE', 'U19605236', 'ENTRY', 'SXRV.DE', 'STK', 'SMART', 'BUY', 5, 'LMT', '1474.00', 'DAY', 'TwoPercent', 'Filled', 0)
        """
    )
    await db.commit()

    # 2. Import an exit file with exit quantity 6
    csv_file = tmp_path / "orders_2026_07_06_exit.csv"
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "1028_TwoPercent_SXRV.DE,EXIT,SXRV.DE,STK,SMART,U19605236,SELL,6,LMT,1500.00,DAY,TwoPercent\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    mock_interactive_brokers = MagicMock()
    mock_interactive_brokers.managedAccounts.return_value = ["U19605236"]
    mock_interactive_brokers.isConnected.return_value = True

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    from datetime import datetime as real_datetime

    # Mock datetime to a Monday (July 6, 2026) to make the test weekday-independent
    mock_now = real_datetime(2026, 7, 6, 12, 0, 0)
    with patch("app.services.importer.datetime") as mock_datetime:
        mock_datetime.side_effect = real_datetime
        mock_datetime.now.return_value = mock_now

        await run_csv_import(
            db=db,
            interactive_brokers=mock_interactive_brokers,
            csv_path=csv_file,
            queue=mock_queue,
            notifier=mock_notifier,
            config=mock_config,
        )

    # 3. Check that the newly imported exit order has quantity 5 (aligned), not 6 (from CSV)
    async with db.execute(
        "SELECT quantity FROM orders WHERE trade_group_id = '1028_TwoPercent_SXRV.DE' AND bracket_role = 'EXIT'"
    ) as cursor:
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["quantity"] == 5

    # Check that a notification for exit alignment was sent
    mock_notifier.send_importer_info.assert_called_once()
    kwargs = mock_notifier.send_importer_info.call_args[1]
    assert kwargs["status"] == "Exit-Menge Angepasst"
    assert kwargs["title"] == "EXIT-SIZING"
    assert "Reduziert von 6 auf 5" in kwargs["details"]


def test_resolve_account_id_fallbacks() -> None:
    """Verifies resolve_account_id handles empty accounts list and fallback account assignment."""
    mock_ib = MagicMock()
    mock_ib.managedAccounts.return_value = []
    assert resolve_account_id(mock_ib, "U12345") == "U12345"

    mock_ib.managedAccounts.return_value = ["U99999"]
    assert resolve_account_id(mock_ib, "U99999") == "U99999"
    assert resolve_account_id(mock_ib, "U11111") == "U99999"


@pytest.mark.asyncio
async def test_check_csv_dos_limits_rejects_large_files(
    tmp_path: Path, mock_config: Config
) -> None:
    """Verifies _check_csv_dos_limits rejects files larger than max_csv_size_bytes."""
    import dataclasses

    from app.services.importer import _check_csv_dos_limits

    large_csv = tmp_path / "orders_large.csv"
    large_csv.write_bytes(b"x" * 1024)

    small_app_config = dataclasses.replace(mock_config.app, max_csv_size_bytes=500)
    config = dataclasses.replace(mock_config, app=small_app_config)

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()

    result = await _check_csv_dos_limits(large_csv, config, mock_notifier)
    assert result is False
    mock_notifier.send_importer_info.assert_called_once()
    assert "DoS-Schutz" in mock_notifier.send_importer_info.call_args[1]["status"]
