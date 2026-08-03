import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.core.models import LegRow
from app.services.csv_reader import validate_group
from app.services.importer import run_csv_import
from app.trading.worker import process_trade_group


@pytest.fixture
def test_config() -> Config:
    """Erstellt eine Testkonfiguration."""
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
    account = AccountConfig(default_limit_pct=0.05)
    telegram = TelegramConfig(
        bot_token="test_token",
        chat_id="test_chat",
        rate_limit_delay_s=0.0,
        request_timeout_s=10.0,
    )
    return Config(
        tws=tws, app=app, account=account, telegram=telegram, strategy_limits={}
    )


@pytest.mark.asyncio
async def test_validate_group_without_entry_allowed() -> None:
    """
    Prüft, dass validate_group reine Exit/SL/TP Gruppen erlaubt,
    wenn kein ENTRY vorhanden ist (für Ausstiege an Folgetagen).
    """
    legs = [
        LegRow(
            trade_group_id="TG123",
            bracket_role="EXIT",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="U1",
            action="SELL",
            quantity=10,
            order_type="MKT",
            target_price=None,
            tif="DAY",
            strategy_name="S1",
        )
    ]
    is_valid, error_message = validate_group("TG123", legs)
    assert is_valid
    assert error_message == ""


@pytest.mark.asyncio
async def test_process_trade_group_exit_cancelled_if_no_position(
    db, test_config: Config
) -> None:
    """
    Prüft, dass eine Post-Fill Exit-Order storniert wird,
    wenn kein Depotbestand für das Symbol vorhanden ist.
    """
    # 1. Datenbank-Setup: Ein ausgeführter ENTRY und ein noch offener EXIT (Created)
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (1, 'TG_NO_POS', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (2, 'TG_NO_POS', 'ACC_1', 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'MKT', 'Created')
        """
    )
    await db.commit()

    # 2. IB-Mocking: Keine Positionen vorhanden
    mock_ib = MagicMock()
    mock_ib.positions.return_value = []
    mock_ib.client.getReqId.return_value = 100

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_notifier.send_message = AsyncMock()

    # 3. Execution ausführen
    await process_trade_group(db, mock_ib, "TG_NO_POS", mock_notifier, test_config)

    # 4. Verifikation: Exit-Order in DB muss auf Cancelled gesetzt sein
    async with db.execute("SELECT status FROM orders WHERE order_id = 2") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Cancelled"

    # IB placeOrder darf nicht aufgerufen worden sein
    mock_ib.placeOrder.assert_not_called()
    mock_notifier.send_importer_info.assert_called_once()
    assert (
        "Keine offene Position"
        in mock_notifier.send_importer_info.call_args[1]["details"]
    )


@pytest.mark.asyncio
async def test_process_trade_group_exit_quantity_reduced(
    db, test_config: Config
) -> None:
    """
    Prüft, dass die Menge der Exit-Order reduziert wird,
    wenn der Depotbestand kleiner ist als die geplante Exit-Menge.
    """
    # 1. Datenbank-Setup: Ein ausgeführter ENTRY und ein noch offener EXIT (Created) mit Qty=10
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (10, 'TG_RED_POS', 'ACC_1', 'ENTRY', 'MSFT', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (11, 'TG_RED_POS', 'ACC_1', 'EXIT', 'MSFT', 'STK', 'SMART', 'SELL', 10, 'MKT', 'Created')
        """
    )
    await db.commit()

    # 2. IB-Mocking: Nur 4 Stücke im Depot vorhanden
    mock_position = MagicMock()
    mock_position.account = "ACC_1"
    mock_position.contract.symbol = "MSFT"
    mock_position.position = 4.0

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]
    mock_ib.client.getReqId.return_value = 101

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_notifier.send_bracket_order_submitted = AsyncMock()
    mock_notifier.send_message = AsyncMock()

    # 3. Execution ausführen
    await process_trade_group(db, mock_ib, "TG_RED_POS", mock_notifier, test_config)

    # 4. Verifikation: Exit-Order in DB muss auf Qty=4 angepasst und Submitted sein
    async with db.execute(
        "SELECT status, quantity FROM orders WHERE order_id = 101"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Submitted"
        assert row["quantity"] == 4

    # IB placeOrder muss mit Qty=4.0 aufgerufen worden sein
    mock_ib.placeOrder.assert_called_once()
    called_order = mock_ib.placeOrder.call_args[0][1]
    assert called_order.totalQuantity == 4.0
    mock_notifier.send_importer_info.assert_called_once()
    assert "reduziert" in mock_notifier.send_importer_info.call_args[1]["details"]
    mock_notifier.send_bracket_order_submitted.assert_called_once()


@pytest.mark.asyncio
async def test_run_csv_import_with_existing_filled_entry(
    db, test_config: Config, tmp_path: Path
) -> None:
    """
    Prüft, dass der CSV-Importer eine nachträgliche EXIT-Order
    zu einer bereits ausgeführten ENTRY-Order hinzufügen kann, ohne die Gruppe abzubrechen.
    """
    # 1. Vorhandene gefüllte ENTRY-Order in DB eintragen
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (20, 'TG_CROSS_DAY', 'ACC_1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 5, 'LMT', 150.0, 'Filled')
        """
    )
    await db.commit()

    # 2. Temp-CSV Datei erstellen, die reinen EXIT-Auftrag enthält
    temp_csv = tmp_path / "temp_test_import.csv"
    temp_csv.write_text(
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "TG_CROSS_DAY,EXIT,AAPL,STK,SMART,ACC_1,SELL,5,MKT,0.0,DAY,TurnoverTiming\n",
        encoding="utf-8",
    )

    try:
        # 3. Mocks für IB & Queue & Notifier
        mock_ib = MagicMock()
        mock_notifier = MagicMock()
        mock_notifier.send_message = AsyncMock()
        queue = asyncio.Queue()

        # 4. Import ausführen
        await run_csv_import(db, mock_ib, temp_csv, queue, mock_notifier, test_config)

        # 5. Verifikation: In der DB muss jetzt eine EXIT-Order mit parent_id = 20 liegen
        async with db.execute(
            "SELECT order_id, parent_id, status FROM orders WHERE trade_group_id = 'TG_CROSS_DAY' AND bracket_role = 'EXIT'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row["parent_id"] == 20
            assert row["status"] == "Created"

        # Queue muss die trade_group_id enthalten
        assert queue.qsize() == 1
        assert await queue.get() == "TG_CROSS_DAY"

    finally:
        if temp_csv.exists():
            temp_csv.unlink()


@pytest.mark.asyncio
async def test_process_trade_group_exit_matching_with_dot_de_suffix(
    db, test_config: Config
) -> None:
    """Verifiziert, dass SXRV.DE in der DB gegen eine IB-Position SXRV gematcht wird."""
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (30, 'TG_SXRV_DE', 'ACC_GERMANY', 'ENTRY', 'SXRV.DE', 'STK', 'SMART', 'BUY', 5, 'LMT', 1400.0, 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, target_price, status)
        VALUES (31, 'TG_SXRV_DE', 'ACC_GERMANY', 'EXIT', 'SXRV.DE', 'STK', 'SMART', 'SELL', 5, 'MKT', 0.0, 'Created')
        """
    )
    await db.commit()

    mock_position = MagicMock()
    mock_position.account = "ACC_GERMANY"
    mock_position.contract.symbol = "SXRV"
    mock_position.position = 5.0

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]
    mock_ib.client.getReqId.return_value = 301

    mock_notifier = MagicMock()
    mock_notifier.send_importer_info = AsyncMock()
    mock_notifier.send_bracket_order_submitted = AsyncMock()
    mock_notifier.send_message = AsyncMock()

    await process_trade_group(db, mock_ib, "TG_SXRV_DE", mock_notifier, test_config)

    async with db.execute(
        "SELECT status, quantity FROM orders WHERE order_id = 301"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Submitted"
        assert row["quantity"] == 5

    mock_ib.placeOrder.assert_called_once()
    called_contract = mock_ib.placeOrder.call_args[0][0]
    assert called_contract.symbol == "SXRV"
    assert called_contract.primaryExchange == "IBIS2"


@pytest.mark.asyncio
async def test_reconcile_broker_positions_recovers_unassigned_position(db) -> None:
    """
    Prüft, dass reconcile_broker_positions bei einer Diskrepanz zwischen Broker
    und DB synthetische ENTRY-Orders (strategy_name=None) und Executions anlegt.
    """
    from decimal import Decimal

    from app.trading.recovery import reconcile_broker_positions

    # 1. IB-Mocking: 15 Aktien AKAM im Broker vorhanden
    mock_position = MagicMock()
    mock_position.account = "U19605236"
    mock_position.contract.symbol = "AKAM"
    mock_position.contract.currency = "USD"
    mock_position.position = 15.0
    mock_position.avgCost = 133.48

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]

    mock_notifier = MagicMock()
    mock_notifier.send_unassigned_position_recovered = AsyncMock()

    # 2. Reconcile ausführen
    await reconcile_broker_positions(db, mock_ib, mock_notifier)

    # 3. Verifikation in orders: Synthetischer ENTRY-Order-Eintrag
    async with db.execute(
        "SELECT order_id, trade_group_id, symbol, action, quantity, bracket_role, status, strategy_name FROM orders WHERE symbol = 'AKAM'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["order_id"] < 0
        assert row["trade_group_id"] == "UNASSIGNED_AKAM_U19605236"
        assert row["action"] == "BUY"
        assert row["quantity"] == 15
        assert row["bracket_role"] == "ENTRY"
        assert row["status"] == "Filled"
        assert row["strategy_name"] is None

    # 4. Verifikation in executions: Ausführungseintrag
    async with db.execute(
        "SELECT exec_id, order_id, price, qty, currency FROM executions WHERE order_id = ?",
        (row["order_id"],),
    ) as cursor:
        exec_row = await cursor.fetchone()
        assert exec_row is not None
        assert exec_row["exec_id"] == f"RECOVERED_POS_AKAM_{abs(row['order_id'])}"
        assert Decimal(str(exec_row["price"])) == Decimal("133.48")
        assert Decimal(str(exec_row["qty"])) == Decimal("15.0")
        assert exec_row["currency"] == "USD"

    # 5. Notifier Verifikation
    mock_notifier.send_unassigned_position_recovered.assert_called_once_with(
        symbol="AKAM",
        quantity=Decimal("15"),
        avg_cost=Decimal("133.48"),
        account_id="U19605236",
    )


@pytest.mark.asyncio
async def test_reconcile_broker_positions_skips_when_synced(db) -> None:
    """
    Prüft, dass reconcile_broker_positions keine neuen Orders/Executions anlegt,
    wenn die DB-Stückzahl bereits exakt mit dem Broker übereinstimmt.
    """

    from app.trading.recovery import reconcile_broker_positions

    # 1. DB-Setup: Bereits 10 Aktien ALAB in DB verzeichnet
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (100, 'TG_ALAB', 'U19605236', 'ENTRY', 'ALAB', 'STK', 'SMART', 'BUY', 10, 'MKT', 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO executions (exec_id, order_id, price, qty, commission, currency, executed_at)
        VALUES ('EXEC_ALAB_1', 100, '400.00', '10.0', '0.0', 'USD', '2026-08-01')
        """
    )
    await db.commit()

    # 2. IB-Mocking: Exakt 10 Aktien ALAB im Broker
    mock_position = MagicMock()
    mock_position.account = "U19605236"
    mock_position.contract.symbol = "ALAB"
    mock_position.contract.currency = "USD"
    mock_position.position = 10.0
    mock_position.avgCost = 400.00

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]

    mock_notifier = MagicMock()
    mock_notifier.send_unassigned_position_recovered = AsyncMock()

    # 3. Reconcile ausführen
    await reconcile_broker_positions(db, mock_ib, mock_notifier)

    # 4. Verifikation: Keine neue Order oder Execution für ALAB
    async with db.execute(
        "SELECT COUNT(*) as count FROM orders WHERE symbol = 'ALAB'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row["count"] == 1  # Nur die bestehende Order #100

    mock_notifier.send_unassigned_position_recovered.assert_not_called()
