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
        dead_order_threshold_min=15,
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
    is_valid, err_msg = validate_group("TG123", legs)
    assert is_valid
    assert err_msg == ""


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
    mock_notifier.send_message = AsyncMock()

    # 3. Execution ausführen
    await process_trade_group(db, mock_ib, "TG_NO_POS", mock_notifier, test_config)

    # 4. Verifikation: Exit-Order in DB muss auf Cancelled gesetzt sein
    async with db.execute("SELECT status FROM orders WHERE order_id = 2") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Cancelled"

    # IB placeOrder darf nicht aufgerufen worden sein
    mock_ib.placeOrder.assert_not_called()
    mock_notifier.send_message.assert_called_once()
    assert "Keine offene Position" in mock_notifier.send_message.call_args[0][0]


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
    assert mock_notifier.send_message.call_count == 2
    sent_messages = [
        call_args[0][0] for call_args in mock_notifier.send_message.call_args_list
    ]
    assert any("reduziert" in msg for msg in sent_messages)
    assert any("ORDER GESENDET" in msg for msg in sent_messages)


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
