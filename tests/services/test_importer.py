# filename: tests/services/test_importer.py
import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
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


@pytest.mark.asyncio
async def test_csv_directory_watcher_cancelled_error(
    tmp_path: Path, mock_config: Config
) -> None:
    """Verifies that csv_directory_watcher re-raises asyncio.CancelledError."""
    data_directory = tmp_path / "data"
    data_directory.mkdir()

    mock_db_conn = AsyncMock()

    async def db_factory():
        return mock_db_conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()

    with patch(
        "app.services.importer._scan_and_process_directory",
        side_effect=asyncio.CancelledError,
    ):
        with pytest.raises(asyncio.CancelledError):
            await csv_directory_watcher(
                db_factory=db_factory,
                interactive_brokers=mock_ib,
                directory_path=data_directory,
                queue=mock_queue,
                notifier=mock_notifier,
                config=mock_config,
                interval_seconds=1,
            )


@pytest.mark.asyncio
async def test_csv_directory_watcher_generic_exception(
    tmp_path: Path, mock_config: Config
) -> None:
    """Verifies that csv_directory_watcher catches generic exceptions and logs them."""
    data_directory = tmp_path / "data"
    data_directory.mkdir()

    mock_db_conn = AsyncMock()

    async def db_factory():
        return mock_db_conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()

    call_count = 0

    async def mock_scan(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Disk read error")
        raise asyncio.CancelledError()

    with patch(
        "app.services.importer._scan_and_process_directory", side_effect=mock_scan
    ):
        with pytest.raises(asyncio.CancelledError):
            await csv_directory_watcher(
                db_factory=db_factory,
                interactive_brokers=mock_ib,
                directory_path=data_directory,
                queue=mock_queue,
                notifier=mock_notifier,
                config=mock_config,
                interval_seconds=0.01,
            )


@pytest.mark.asyncio
async def test_scan_and_process_directory_edge_cases(
    tmp_path: Path, mock_config: Config
) -> None:
    """Verifies non-existent directory and non-matching file names are safely ignored."""
    from app.services.importer import _scan_and_process_directory

    mock_db_conn = AsyncMock()

    async def db_factory():
        return mock_db_conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()
    warned = set()

    # 1. Non-existent path
    non_existent = tmp_path / "does_not_exist"
    import re

    pattern = re.compile(r"^orders_\d{4}_\d{2}_\d{2}\.csv$")

    await _scan_and_process_directory(
        db_factory,
        mock_ib,
        non_existent,
        mock_queue,
        mock_notifier,
        mock_config,
        pattern,
        warned,
    )

    # 2. Non-matching file in directory
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "orders_invalid_name.csv").write_text("dummy", encoding="utf-8")

    await _scan_and_process_directory(
        db_factory,
        mock_ib,
        data_dir,
        mock_queue,
        mock_notifier,
        mock_config,
        pattern,
        warned,
    )


@pytest.mark.asyncio
async def test_run_csv_import_empty_csv_and_invalid_date_filename(
    tmp_path: Path, mock_config: Config, db
) -> None:
    """Verifies handling of empty CSV and date parsing fallback for custom filename."""
    mock_ib = MagicMock()
    mock_ib.managedAccounts.return_value = ["U12345"]
    mock_ib.isConnected.return_value = True
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()

    # Empty CSV
    empty_csv = tmp_path / "orders_2026_07_01.csv"
    empty_csv.write_text(
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n",
        encoding="utf-8",
    )
    await run_csv_import(db, mock_ib, empty_csv, mock_queue, mock_notifier, mock_config)

    # Invalid date filename pattern (e.g. month 99)
    invalid_date_csv = tmp_path / "orders_2026_99_99.csv"
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "101_Test_AAPL,ENTRY,AAPL,STK,SMART,U12345,BUY,10,LMT,150.00,DAY,Test\n"
    )
    invalid_date_csv.write_text(csv_content, encoding="utf-8")
    mock_ib.accountValues.return_value = [
        AccountValue(
            account="U12345",
            tag="NetLiquidation",
            value="100000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="AvailableFunds",
            value="100000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="TotalCashValue",
            value="100000.00",
            currency="EUR",
            modelCode="",
        ),
    ]

    await run_csv_import(
        db, mock_ib, invalid_date_csv, mock_queue, mock_notifier, mock_config
    )


@pytest.mark.asyncio
async def test_check_csv_dos_limits_non_existent_file(
    tmp_path: Path, mock_config: Config
) -> None:
    """Verifies that _check_csv_dos_limits returns True if file does not exist."""
    from app.services.importer import _check_csv_dos_limits

    mock_notifier = MagicMock()
    non_file = tmp_path / "no_file.csv"
    assert await _check_csv_dos_limits(non_file, mock_config, mock_notifier) is True


@pytest.mark.asyncio
async def test_process_and_upsert_group_validation_error(
    mock_config: Config, db
) -> None:
    """Verifies that process_and_upsert_group skips invalid groups and notifies."""
    from app.services.importer import _process_and_upsert_group

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_queue = asyncio.Queue()

    # Leg row with invalid bracket role
    from app.services.csv_reader import LegRow

    invalid_leg = LegRow(
        trade_group_id="group_1",
        bracket_role="INVALID_ROLE",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="U12345",
        action="BUY",
        quantity=10,
        order_type="LMT",
        target_price=Decimal("150.0"),
        tif="DAY",
        strategy_name="Test",
    )

    await _process_and_upsert_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="group_1",
        raw_legs=[invalid_leg],
        queue=mock_queue,
        notifier=mock_notifier,
        config=mock_config,
    )
    mock_notifier.send_importer_info.assert_called_once()
    assert (
        mock_notifier.send_importer_info.call_args[1]["title"] == "VALIDIERUNGSFEHLER"
    )


@pytest.mark.asyncio
async def test_dipbuyer_filtering_on_wednesday(
    tmp_path: Path, mock_config: Config, db
) -> None:
    """Verifies DipBuyer strategy filtering when current_weekday > 1 (Wednesday=2)."""
    csv_file = tmp_path / "orders_2026_07_08.csv"  # Wednesday
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "200_DipBuyer_AAPL,ENTRY,AAPL,STK,SMART,U12345,BUY,10,LMT,150.00,DAY,DipBuyer\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    mock_ib = MagicMock()
    mock_ib.managedAccounts.return_value = ["U12345"]
    mock_ib.isConnected.return_value = True
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()

    # Weekday 2 (Wednesday) - no ENTRY in DB -> should skip DipBuyer entry group entirely
    await run_csv_import(db, mock_ib, csv_file, mock_queue, mock_notifier, mock_config)

    async with db.execute("SELECT COUNT(*) FROM orders") as cursor:
        count = (await cursor.fetchone())[0]
        assert count == 0


@pytest.mark.asyncio
async def test_process_and_upsert_group_zero_allocation_and_downscaled_to_zero(
    mock_config: Config, db
) -> None:
    """Verifies handling when capital allocation or downscaled quantity is <= 0."""
    from app.services.csv_reader import LegRow
    from app.services.importer import _process_and_upsert_group

    mock_ib = MagicMock()
    mock_ib.managedAccounts.return_value = ["U12345"]
    mock_ib.accountValues.return_value = [
        AccountValue(
            account="U12345",
            tag="NetLiquidation",
            value="0.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="AvailableFunds",
            value="0.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="TotalCashValue",
            value="0.00",
            currency="EUR",
            modelCode="",
        ),
    ]
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_queue = asyncio.Queue()

    leg = LegRow(
        trade_group_id="group_zero_cap",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="U12345",
        action="BUY",
        quantity=10,
        order_type="LMT",
        target_price=Decimal("150.0"),
        tif="DAY",
        strategy_name="Test",
    )

    # 1. Zero capital allocation -> KAPITAL-FEHLER
    await _process_and_upsert_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="group_zero_cap",
        raw_legs=[leg],
        queue=mock_queue,
        notifier=mock_notifier,
        config=mock_config,
    )
    assert mock_notifier.send_importer_info.call_args[1]["title"] == "KAPITAL-FEHLER"

    # 2. Downscaled quantity <= 0 -> SIZING-FEHLER
    mock_ib.accountValues.return_value = [
        AccountValue(
            account="U12345",
            tag="NetLiquidation",
            value="100.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="AvailableFunds",
            value="100.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="TotalCashValue",
            value="100.00",
            currency="EUR",
            modelCode="",
        ),
    ]
    expensive_leg = LegRow(
        trade_group_id="group_expensive",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="U12345",
        action="BUY",
        quantity=1,
        order_type="LMT",
        target_price=Decimal("1000.0"),
        tif="DAY",
        strategy_name="Test",
    )

    await _process_and_upsert_group(
        db=db,
        interactive_brokers=mock_ib,
        trade_group_id="group_expensive",
        raw_legs=[expensive_leg],
        queue=mock_queue,
        notifier=mock_notifier,
        config=mock_config,
    )
    assert mock_notifier.send_importer_info.call_args[1]["title"] == "SIZING-FEHLER"


@pytest.mark.asyncio
async def test_upsert_trade_group_legs_updates_existing_created_orders(db) -> None:
    """Verifies that _upsert_trade_group_legs updates existing Created orders in DB."""
    from app.services.csv_reader import LegRow
    from app.services.importer import _upsert_trade_group_legs

    # Insert existing ENTRY and child SL orders with status 'Created'
    await db.execute(
        """
        INSERT INTO orders (order_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status, retry_count)
        VALUES (-10, NULL, 'update_group', 'U12345', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 5, 'LMT', '140.00', 'DAY', 'Test', 'Created', 0)
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status, retry_count)
        VALUES (-11, -10, 'update_group', 'U12345', 'SL', 'AAPL', 'STK', 'SMART', 'SELL', 5, 'STP', '130.00', 'DAY', 'Test', 'Created', 0)
        """
    )
    await db.commit()

    entry_leg = LegRow(
        "update_group",
        "ENTRY",
        "AAPL",
        "STK",
        "SMART",
        "U12345",
        "BUY",
        10,
        "LMT",
        Decimal("150.00"),
        "DAY",
        "Test",
    )
    sl_leg = LegRow(
        "update_group",
        "SL",
        "AAPL",
        "STK",
        "SMART",
        "U12345",
        "SELL",
        10,
        "STP",
        Decimal("135.00"),
        "DAY",
        "Test",
    )

    mock_notifier = MagicMock()
    await _upsert_trade_group_legs(
        db, "update_group", "U12345", entry_leg, [entry_leg, sl_leg], 10, mock_notifier
    )

    async with db.execute(
        "SELECT quantity, target_price FROM orders WHERE order_id = -10"
    ) as cursor:
        row = await cursor.fetchone()
        assert row["quantity"] == 10
        assert Decimal(str(row["target_price"])) == Decimal("150.00")


@pytest.mark.asyncio
async def test_upsert_trade_group_legs_updates_existing_cancelled_entry_orders(
    db,
) -> None:
    """Verifies that _upsert_trade_group_legs updates existing Cancelled ENTRY orders back to Created status in DB."""
    from app.services.csv_reader import LegRow
    from app.services.importer import _upsert_trade_group_legs

    # Insert existing ENTRY order with status 'Cancelled'
    await db.execute(
        """
        INSERT INTO orders (order_id, parent_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name, status, retry_count)
        VALUES (1052, NULL, 'cancelled_group', 'U12345', 'ENTRY', 'GOOGL', 'STK', 'SMART', 'BUY', 17, 'LMT', '341.11', 'DAY', 'DipBuyer', 'Cancelled', 2)
        """
    )
    await db.commit()

    entry_leg = LegRow(
        "cancelled_group",
        "ENTRY",
        "GOOGL",
        "STK",
        "SMART",
        "U12345",
        "BUY",
        17,
        "LMT",
        Decimal("341.11"),
        "DAY",
        "DipBuyer",
    )

    mock_notifier = MagicMock()
    await _upsert_trade_group_legs(
        db, "cancelled_group", "U12345", entry_leg, [entry_leg], 17, mock_notifier
    )

    async with db.execute(
        "SELECT status, retry_count FROM orders WHERE order_id = 1052"
    ) as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Created"
        assert row["retry_count"] == 0


@pytest.mark.asyncio
async def test_upsert_trade_group_legs_standalone_exit_error_handling(db) -> None:
    """Verifies that non-standalone-exit errors are re-raised by _upsert_trade_group_legs."""
    from app.services.csv_reader import LegRow
    from app.services.importer import _upsert_trade_group_legs

    exit_leg = LegRow(
        "standalone",
        "EXIT",
        "AAPL",
        "STK",
        "SMART",
        "U12345",
        "SELL",
        10,
        "LMT",
        Decimal("160.00"),
        "DAY",
        "Test",
    )
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()

    # Attempt standalone exit with no ENTRY in DB -> raises ValueError("Standalone exit order imported...")
    with pytest.raises(ValueError, match="Standalone exit order imported"):
        await _upsert_trade_group_legs(
            db, "standalone", "U12345", None, [exit_leg], 10, mock_notifier
        )


@pytest.mark.asyncio
async def test_fetch_account_balance_metrics_edge_cases() -> None:
    """Verifies cache filtering with mismatched account, ValueError parsing, and exception in summary."""
    mock_ib = MagicMock()
    # Cache with wrong account, valid tag, and invalid float tag
    mock_ib.accountValues.return_value = [
        AccountValue(
            account="OTHER_ACC",
            tag="NetLiquidation",
            value="100000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="NetLiquidation",
            value="invalid_number",
            currency="EUR",
            modelCode="",
        ),
    ]

    mock_ib.accountSummaryAsync = AsyncMock(
        side_effect=RuntimeError("TWS socket broken")
    )

    metrics = await fetch_account_balance_metrics(mock_ib, "U12345")
    assert metrics.net_liquidation_value == Decimal("0.0")
    assert metrics.available_funds_value == Decimal("0.0")
    assert metrics.total_cash_value == Decimal("0.0")


def test_calculate_downscaled_quantity_edge_cases() -> None:
    """Verifies calculate_downscaled_quantity edge cases for None price and within-limit allocation."""
    from app.services.importer import calculate_downscaled_quantity

    assert calculate_downscaled_quantity(50, None, Decimal("1000.0")) == 50
    assert calculate_downscaled_quantity(50, Decimal("0.0"), Decimal("1000.0")) == 50
    assert calculate_downscaled_quantity(10, Decimal("100.0"), Decimal("2000.0")) == 10


@pytest.mark.asyncio
async def test_dipbuyer_exit_without_db_entry_on_wednesday(
    tmp_path: Path, mock_config: Config, db
) -> None:
    """Verifies skipping DipBuyer exit legs on Wednesday when no ENTRY exists in DB."""
    csv_file = tmp_path / "orders_2026_07_08.csv"  # Wednesday
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "300_DipBuyer_TSLA,ENTRY,TSLA,STK,SMART,U12345,BUY,10,LMT,200.00,DAY,DipBuyer\n"
        "300_DipBuyer_TSLA,EXIT,TSLA,STK,SMART,U12345,SELL,10,LMT,220.00,DAY,DipBuyer\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    mock_ib = MagicMock()
    mock_ib.managedAccounts.return_value = ["U12345"]
    mock_ib.isConnected.return_value = True
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()

    # Weekday 2 (Wednesday) - no ENTRY in DB -> filter removes ENTRY, then checks DB for ENTRY for EXIT, doesn't find it, skips.
    await run_csv_import(db, mock_ib, csv_file, mock_queue, mock_notifier, mock_config)

    async with db.execute("SELECT COUNT(*) FROM orders") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_custom_strategy_limits_in_config(
    tmp_path: Path, mock_config: Config, db
) -> None:
    """Verifies that strategy limits configured in config.strategy_limits are applied."""
    import dataclasses

    custom_config = dataclasses.replace(
        mock_config, strategy_limits={"CustomStrategy": 0.10}
    )

    csv_file = tmp_path / "orders_2026_07_06.csv"
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "400_CustomStrategy_NVDA,ENTRY,NVDA,STK,SMART,U12345,BUY,10,LMT,100.00,DAY,CustomStrategy\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    mock_ib = MagicMock()
    mock_ib.managedAccounts.return_value = ["U12345"]
    mock_ib.isConnected.return_value = True
    mock_ib.accountValues.return_value = [
        AccountValue(
            account="U12345",
            tag="NetLiquidation",
            value="100000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="AvailableFunds",
            value="100000.00",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U12345",
            tag="TotalCashValue",
            value="100000.00",
            currency="EUR",
            modelCode="",
        ),
    ]
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()

    await run_csv_import(
        db, mock_ib, csv_file, mock_queue, mock_notifier, custom_config
    )

    async with db.execute(
        "SELECT quantity FROM orders WHERE trade_group_id = '400_CustomStrategy_NVDA'"
    ) as cursor:
        assert (await cursor.fetchone())["quantity"] == 10


@pytest.mark.asyncio
async def test_process_daily_csv_file_rename_failure(
    tmp_path: Path, mock_config: Config
) -> None:
    """Verifies error handling when renaming a failed CSV to .err fails."""
    from app.services.importer import _process_daily_csv_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    test_csv = data_dir / "orders_2026_06_01.csv"
    test_csv.write_text("invalid,content", encoding="utf-8")

    mock_db_conn = AsyncMock()

    async def db_factory():
        return mock_db_conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_queue = asyncio.Queue()

    with patch(
        "app.services.importer.run_csv_import",
        side_effect=ValueError("Test import error"),
    ):
        with patch.object(Path, "rename", side_effect=OSError("Permission denied")):
            await _process_daily_csv_file(
                db_factory=db_factory,
                interactive_brokers=mock_ib,
                csv_file=test_csv,
                queue=mock_queue,
                notifier=mock_notifier,
                config=mock_config,
            )


@pytest.mark.asyncio
async def test_process_daily_csv_file_with_cancelled_orders_renames_to_err(
    tmp_path: Path, mock_config: Config, db: aiosqlite.Connection
) -> None:
    """Verifies that if orders from the CSV are Cancelled (e.g. reauth expiry), the file is renamed to .err."""
    from app.services.importer import _process_daily_csv_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    test_csv = data_dir / "orders_2026_09_02.csv"
    test_csv.write_text("dummy,content", encoding="utf-8")

    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (3001, 'TG_EXPIRED_GROUP', 'ACC1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'Cancelled')
        """
    )
    await db.commit()

    async def db_factory():
        conn = await aiosqlite.connect("file::memory:?cache=shared", uri=True)
        conn.row_factory = aiosqlite.Row
        return conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    with patch(
        "app.services.importer.run_csv_import",
        new_callable=AsyncMock,
        return_value=["TG_EXPIRED_GROUP"],
    ):
        await _process_daily_csv_file(
            db_factory=db_factory,
            interactive_brokers=mock_ib,
            csv_file=test_csv,
            queue=mock_queue,
            notifier=mock_notifier,
            config=mock_config,
        )

    assert not test_csv.exists()
    err_csv = data_dir / "archive" / "orders_2026_09_02.csv.err"
    assert err_csv.exists()
    mock_notifier.send_importer_info.assert_called_once()
    call_kwargs = mock_notifier.send_importer_info.call_args[1]
    assert call_kwargs["title"] == "DATEI VERFALLEN"
    assert "Verfallen" in call_kwargs["status"]


@pytest.mark.asyncio
async def test_process_daily_csv_file_with_successful_orders_renames_to_bak(
    tmp_path: Path, mock_config: Config, db: aiosqlite.Connection
) -> None:
    """Verifies that if all orders from the CSV are successfully placed, the file is renamed to .bak."""
    from app.services.importer import _process_daily_csv_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    test_csv = data_dir / "orders_2026_09_02.csv"
    test_csv.write_text("dummy,content", encoding="utf-8")

    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (3002, 'TG_SUCCESS_GROUP', 'ACC1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 150.0, 'Submitted')
        """
    )
    await db.commit()

    async def db_factory():
        conn = await aiosqlite.connect("file::memory:?cache=shared", uri=True)
        conn.row_factory = aiosqlite.Row
        return conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    with patch(
        "app.services.importer.run_csv_import",
        new_callable=AsyncMock,
        return_value=["TG_SUCCESS_GROUP"],
    ):
        await _process_daily_csv_file(
            db_factory=db_factory,
            interactive_brokers=mock_ib,
            csv_file=test_csv,
            queue=mock_queue,
            notifier=mock_notifier,
            config=mock_config,
        )

    assert not test_csv.exists()
    bak_csv = data_dir / "archive" / "orders_2026_09_02.csv.bak"
    assert bak_csv.exists()
    mock_notifier.send_importer_info.assert_called_once()
    call_kwargs = mock_notifier.send_importer_info.call_args[1]
    assert call_kwargs["title"] == "DATEI IMPORTIERT"
    assert call_kwargs["status"] == "Erfolgreich"


@pytest.mark.asyncio
async def test_process_daily_csv_file_ignores_cancelled_orders_from_previous_days(
    tmp_path: Path, mock_config: Config, db: aiosqlite.Connection
) -> None:
    """Verifies that historical cancelled orders from prior days do NOT cause .err archive."""
    from app.services.importer import _process_daily_csv_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    test_csv = data_dir / "orders_2026_09_04.csv"
    test_csv.write_text("dummy,content", encoding="utf-8")

    # Order from previous day that was cancelled (e.g. DAY order expired yesterday)
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status, transmitted_at)
        VALUES (3003, 'TG_SWING_POS', 'ACC1', 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'LMT', 160.0, 'Cancelled', '2026-09-03 05:00:00')
        """
    )
    # Order from today that was successfully presubmitted
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status, transmitted_at)
        VALUES (3004, 'TG_SWING_POS', 'ACC1', 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'MOC', 0.0, 'PreSubmitted', '2026-09-04 05:00:30')
        """
    )
    await db.commit()

    async def db_factory():
        conn = await aiosqlite.connect("file::memory:?cache=shared", uri=True)
        conn.row_factory = aiosqlite.Row
        return conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    with patch(
        "app.services.importer.run_csv_import",
        new_callable=AsyncMock,
        return_value=["TG_SWING_POS"],
    ):
        await _process_daily_csv_file(
            db_factory=db_factory,
            interactive_brokers=mock_ib,
            csv_file=test_csv,
            queue=mock_queue,
            notifier=mock_notifier,
            config=mock_config,
        )

    assert not test_csv.exists()
    bak_csv = data_dir / "archive" / "orders_2026_09_04.csv.bak"
    assert bak_csv.exists()
    err_csv = data_dir / "archive" / "orders_2026_09_04.csv.err"
    assert not err_csv.exists()
    mock_notifier.send_importer_info.assert_called_once()
    call_kwargs = mock_notifier.send_importer_info.call_args[1]
    assert call_kwargs["title"] == "DATEI IMPORTIERT"
    assert call_kwargs["status"] == "Erfolgreich"


@pytest.mark.asyncio
async def test_run_csv_import_only_returns_queued_trade_groups(
    tmp_path: Path, mock_config: Config, db: aiosqlite.Connection
) -> None:
    """Verifies that run_csv_import returns only trade groups that were actually queued."""
    from app.services.importer import run_csv_import

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Friday 2026-09-04
    test_csv = data_dir / "orders_2026_09_04.csv"
    test_csv.write_text(
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name,currency\n"
        "1438_DipBuyer_PTC,ENTRY,PTC,STK,SMART,U19605236,BUY,41,LMT,145.00,DAY,DipBuyer,\n"
        "1438_DipBuyer_PTC,TP,PTC,STK,SMART,U19605236,SELL,41,LOC,154.28,DAY,DipBuyer,\n",
        encoding="utf-8",
    )

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    # DipBuyer on Friday without existing DB entry is skipped and not queued
    imported_ids = await run_csv_import(
        db=db,
        interactive_brokers=mock_ib,
        csv_path=test_csv,
        queue=mock_queue,
        notifier=mock_notifier,
        config=mock_config,
    )

    assert imported_ids == []
    assert mock_queue.empty()


@pytest.mark.asyncio
async def test_process_daily_csv_file_treats_expired_transmitted_day_orders_as_success(
    tmp_path: Path, mock_config: Config, db: aiosqlite.Connection
) -> None:
    """Verifies that an order transmitted today and cancelled at EOD (normal market expiry) is NOT an error."""
    from app.services.importer import _process_daily_csv_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    test_csv = data_dir / "orders_2026_09_04.csv"
    test_csv.write_text("dummy,content", encoding="utf-8")

    # Order that was transmitted today, but expired at EOD because market conditions were not met
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status, transmitted_at)
        VALUES (3005, 'TG_COND_EXPIRED', 'ACC1', 'EXIT', 'MSFT', 'STK', 'SMART', 'SELL', 5, 'LMT', 500.0, 'Cancelled', '2026-09-04 07:00:30')
        """
    )
    await db.commit()

    async def db_factory():
        conn = await aiosqlite.connect("file::memory:?cache=shared", uri=True)
        conn.row_factory = aiosqlite.Row
        return conn

    mock_ib = MagicMock()
    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock(return_value=True)
    mock_queue = asyncio.Queue()

    with patch(
        "app.services.importer.run_csv_import",
        new_callable=AsyncMock,
        return_value=["TG_COND_EXPIRED"],
    ):
        await _process_daily_csv_file(
            db_factory=db_factory,
            interactive_brokers=mock_ib,
            csv_file=test_csv,
            queue=mock_queue,
            notifier=mock_notifier,
            config=mock_config,
        )

    assert not test_csv.exists()
    bak_csv = data_dir / "archive" / "orders_2026_09_04.csv.bak"
    assert bak_csv.exists()
    err_csv = data_dir / "archive" / "orders_2026_09_04.csv.err"
    assert not err_csv.exists()
    mock_notifier.send_importer_info.assert_called_once()
    call_kwargs = mock_notifier.send_importer_info.call_args[1]
    assert call_kwargs["title"] == "DATEI IMPORTIERT"
    assert call_kwargs["status"] == "Erfolgreich"
