"""Unit- und Regressionstests für Error-Signatur, Notfall-Alarmierung und EOD-Stornofilterung.

Test-Matrix:
| Szenario | Parameter | Erwartetes Verhalten |
| :--- | :--- | :--- |
| ib_async ErrorEvent Signatur | (reqId, errorCode, errorString, contract) | Callback wird fehlerfrei ohne TypeError ausgeführt |
| Abwärtskompatible Signatur | (reqId, errorCode, errorString) | Callback wird fehlerfrei ausgeführt |
| Unbehandelte Ausnahme in on_error | Simulierter Fehler in classify_error_code | Notfall-Telegram-Alarm wird gesendet, kein Crash |
| Unbehandelte Ausnahme in _process_error | Simulierter DB-/Netzwerkfehler | Notfall-Telegram-Alarm wird gesendet |
| Storno vor Marktschluss ohne EOD-Reason | 14:00 NY, reason='Manual cancel' | send_order_failed Alarm wird ausgelöst |
| Storno nahe Marktschluss | 15:56 NY, reason='' | Alarm wird unterdrückt (Factor 1 Zeit) |
| Storno nach Marktschluss | 16:05 NY, reason='' | Alarm wird unterdrückt (Factor 1 Zeit) |
| Storno mit GTD/EOD-Reason | 14:00 NY, reason='Order expired' | Alarm wird unterdrückt (Factor 2 Reason) |
| Storno mit OCA-Reason | 14:00 NY, reason='One-Cancels-All order cancelled' | Alarm wird unterdrückt (Factor 2 Reason) |
| Doppelte Storno-Events | Erst on_error(202), dann orderStatusEvent('Cancelled') | Genau ein Alarm bzw. eine Unterdrückung |
| Deutscher Markt Cutoff | 17:24 Berlin vs 17:26 Berlin | Vor 17:25 aktiv, ab 17:25 unterdrückt |
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
from ib_async import Contract

from app.core.config import AccountConfig, AppConfig, Config, TelegramConfig, TwsConfig
from app.trading.callbacks import TwsCallbacksManager


@pytest.fixture
def test_config() -> Config:
    """Erstellt ein Test-Konfigurationsobjekt."""
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
        rate_limit_delay_s=1.5,
        request_timeout_s=10.0,
    )
    return Config(
        tws=tws, app=app, account=account, telegram=telegram, strategy_limits={}
    )


@pytest.fixture
async def in_memory_db() -> aiosqlite.Connection:
    """Initialisiert eine InMemory-SQLite-Datenbank mit Orders-Schema."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            perm_id INTEGER,
            parent_id INTEGER,
            trade_group_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            bracket_role TEXT NOT NULL,
            symbol TEXT NOT NULL,
            sec_type TEXT NOT NULL,
            exchange TEXT NOT NULL,
            action TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            order_type TEXT NOT NULL,
            target_price TEXT,
            tif TEXT,
            strategy_name TEXT,
            status TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            transmitted_at TIMESTAMP
        )
        """
    )
    await db.commit()
    yield db
    await db.close()


@pytest.fixture
def mock_notifier() -> MagicMock:
    """Erstellt einen gemockten TelegramNotifier."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=True)
    notifier.send_order_failed = AsyncMock(return_value=True)
    notifier.send_order_filled = AsyncMock(return_value=True)
    notifier.send_broker_connection_status = AsyncMock(return_value=True)
    notifier.send_loc_execution_anomaly = AsyncMock(return_value=True)
    return notifier


@pytest.fixture
def callbacks_manager(
    in_memory_db: aiosqlite.Connection,
    mock_notifier: MagicMock,
    test_config: Config,
) -> TwsCallbacksManager:
    """Instanziiert den TwsCallbacksManager für Tests."""
    mock_ib = MagicMock()
    trigger_settlement = AsyncMock()
    handle_retriable = AsyncMock()
    run_recovery = AsyncMock()
    run_reconnect = AsyncMock()

    async def db_factory() -> aiosqlite.Connection:
        return in_memory_db

    return TwsCallbacksManager(
        db_factory=db_factory,
        interactive_brokers=mock_ib,
        notifier=mock_notifier,
        config=test_config,
        trigger_settlement_callback=trigger_settlement,
        handle_retriable_error_callback=handle_retriable,
        run_recovery_callback=run_recovery,
        run_reconnect_callback=run_reconnect,
    )


# ==============================================================================
# 1. Tests für ib_async errorEvent Signatur (4 Parameter)
# ==============================================================================


@pytest.mark.asyncio
async def test_on_error_accepts_four_arguments_from_ib_error_event(
    callbacks_manager: TwsCallbacksManager,
) -> None:
    """Verifiziert, dass on_error exakt die 4 Argumente von ib.errorEvent ohne TypeError verarbeitet."""
    # Arrange
    req_id = 1484
    error_code = 201
    error_string = "The time-in-force OPG is invalid for this combination of exchange and security type"
    mock_contract = Contract()
    mock_contract.symbol = "MNQ"
    mock_contract.secType = "FUT"

    # Act & Assert - darf keinen TypeError auslösen
    callbacks_manager.on_error(req_id, error_code, error_string, mock_contract)


@pytest.mark.asyncio
async def test_on_error_accepts_three_arguments_backward_compatibility(
    callbacks_manager: TwsCallbacksManager,
) -> None:
    """Verifiziert die Abwärtskompatibilität, falls contract weggelassen wird."""
    # Arrange
    req_id = 1438
    error_code = 202
    error_string = "Order Canceled - reason:"

    # Act & Assert - darf keinen TypeError auslösen
    callbacks_manager.on_error(req_id, error_code, error_string)


@pytest.mark.asyncio
async def test_on_error_unhandled_exception_sends_emergency_alert(
    callbacks_manager: TwsCallbacksManager,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass bei einer unerwarteten Ausnahme in on_error ein Notfall-Telegram-Alarm gesendet wird."""
    # Arrange
    with patch(
        "app.trading.callbacks.classify_error_code",
        side_effect=RuntimeError("Simulierter Systemabsturz"),
    ):
        # Act
        callbacks_manager.on_error(1001, 500, "Severe broker glitch", None)
        await asyncio.sleep(0.05)

    # Assert
    mock_notifier.send_message.assert_awaited_once()
    sent_text = mock_notifier.send_message.await_args[0][0]
    assert "KRITISCHER SYSTEMFEHLER IN ON_ERROR" in sent_text
    assert "Simulierter Systemabsturz" in sent_text


@pytest.mark.asyncio
async def test_process_error_unhandled_exception_sends_emergency_alert(
    callbacks_manager: TwsCallbacksManager,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass eine Ausnahme im asynchronen _process_error einen Notfall-Alarm sendet."""
    # Arrange
    from app.trading.error_codes import ErrorClass

    with patch.object(
        callbacks_manager,
        "_cancel_order_in_db",
        side_effect=ValueError("Corrupted DB state"),
    ):
        # Act
        await callbacks_manager._process_error(
            request_id=1234,
            error_code=202,
            error_string="Order Canceled",
            error_class=ErrorClass.CANCEL,
        )

    # Assert
    mock_notifier.send_message.assert_awaited_once()
    sent_text = mock_notifier.send_message.await_args[0][0]
    assert "KRITISCHER FEHLER BEI FEHLERVERARBEITUNG" in sent_text
    assert "Corrupted DB state" in sent_text


# ==============================================================================
# 2. Tests für Two-Factor EOD / OCA Stornofilterung
# ==============================================================================


def test_is_near_or_after_market_close_us_equities(
    callbacks_manager: TwsCallbacksManager,
) -> None:
    """Prüft die Zeitschwelle für US-Aktien (Cutoff 15:45 New York)."""
    # Arrange
    ny_tz = ZoneInfo("America/New_York")
    time_before = datetime(2026, 9, 11, 15, 44, 0, tzinfo=ny_tz)
    time_exact = datetime(2026, 9, 11, 15, 45, 0, tzinfo=ny_tz)
    time_after = datetime(2026, 9, 11, 16, 5, 0, tzinfo=ny_tz)

    # Act & Assert
    assert not callbacks_manager._is_near_or_after_market_close("AAPL", time_before)
    assert callbacks_manager._is_near_or_after_market_close("AAPL", time_exact)
    assert callbacks_manager._is_near_or_after_market_close("AAPL", time_after)


def test_is_near_or_after_market_close_german_equities(
    callbacks_manager: TwsCallbacksManager,
) -> None:
    """Prüft die Zeitschwelle für deutsche Aktien (.DE, Cutoff 17:15 Berlin)."""
    # Arrange
    berlin_tz = ZoneInfo("Europe/Berlin")
    time_before = datetime(2026, 9, 11, 17, 14, 0, tzinfo=berlin_tz)
    time_exact = datetime(2026, 9, 11, 17, 15, 0, tzinfo=berlin_tz)
    time_after = datetime(2026, 9, 11, 17, 35, 0, tzinfo=berlin_tz)

    # Act & Assert
    assert not callbacks_manager._is_near_or_after_market_close("SAP.DE", time_before)
    assert callbacks_manager._is_near_or_after_market_close("SAP.DE", time_exact)
    assert callbacks_manager._is_near_or_after_market_close("SAP.DE", time_after)


def test_is_eod_or_oca_reason_detection(callbacks_manager: TwsCallbacksManager) -> None:
    """Prüft die Erkennung von EOD-, GTD- und OCA-Schlüsselwörtern in Reason-Strings."""
    # Arrange & Act & Assert
    assert callbacks_manager._is_eod_or_oca_reason("Order expired by TWS")
    assert callbacks_manager._is_eod_or_oca_reason("Time in force expired")
    assert callbacks_manager._is_eod_or_oca_reason("Cancelled by GTD order expiration")
    assert callbacks_manager._is_eod_or_oca_reason("One-Cancels-All sibling filled")
    assert callbacks_manager._is_eod_or_oca_reason("OCA group cancellation triggered")
    assert not callbacks_manager._is_eod_or_oca_reason("")
    assert not callbacks_manager._is_eod_or_oca_reason("User cancelled")
    assert not callbacks_manager._is_eod_or_oca_reason("Insufficient margin funds")


@pytest.mark.asyncio
async def test_cancellation_intraday_unexpected_sends_alert(
    callbacks_manager: TwsCallbacksManager,
    in_memory_db: aiosqlite.Connection,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass eine unvorhergesehene untertägige Stornierung einen Alarm auslöst."""
    # Arrange
    await in_memory_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (2001, 'TG_INTRADAY', 'U12345', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Submitted')
        """
    )
    await in_memory_db.commit()

    with patch.object(
        callbacks_manager,
        "_is_near_or_after_market_close",
        return_value=False,
    ):
        # Act
        await callbacks_manager._cancel_order_in_db(
            request_id=2001,
            error_code=202,
            error_string="Order Canceled - reason: Broker rejected execution",
        )

    # Assert
    mock_notifier.send_order_failed.assert_awaited_once()
    call_kwargs = mock_notifier.send_order_failed.await_args.kwargs
    assert call_kwargs["order_id"] == 2001
    assert call_kwargs["symbol"] == "AAPL"
    assert call_kwargs["is_fatal"] is False
    assert "Broker rejected execution" in call_kwargs["reason"]


@pytest.mark.asyncio
async def test_cancellation_at_market_close_suppresses_alert(
    callbacks_manager: TwsCallbacksManager,
    in_memory_db: aiosqlite.Connection,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass eine Stornierung nahe Marktschluss unterdrückt wird (Factor 1)."""
    # Arrange
    await in_memory_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (2002, 'TG_EOD', 'U12345', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Submitted')
        """
    )
    await in_memory_db.commit()

    with patch.object(
        callbacks_manager,
        "_is_near_or_after_market_close",
        return_value=True,
    ):
        # Act
        await callbacks_manager._cancel_order_in_db(
            request_id=2002,
            error_code=202,
            error_string="Order Canceled - reason:",
        )

    # Assert - Alarm darf NICHT gesendet werden
    mock_notifier.send_order_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_with_expired_reason_suppresses_alert(
    callbacks_manager: TwsCallbacksManager,
    in_memory_db: aiosqlite.Connection,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass eine Stornierung mit 'expired'-Begründung unterdrückt wird (Factor 2)."""
    # Arrange
    await in_memory_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (2003, 'TG_EXPIRED', 'U12345', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Submitted')
        """
    )
    await in_memory_db.commit()

    with patch.object(
        callbacks_manager,
        "_is_near_or_after_market_close",
        return_value=False,
    ):
        # Act
        await callbacks_manager._cancel_order_in_db(
            request_id=2003,
            error_code=202,
            error_string="Order Canceled - reason: Time In Force Expired",
        )

    # Assert - Alarm darf NICHT gesendet werden
    mock_notifier.send_order_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_status_cancelled_event_triggers_and_deduplicates(
    callbacks_manager: TwsCallbacksManager,
    in_memory_db: aiosqlite.Connection,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass ein orderStatusEvent('Cancelled') alarmiert, aber nicht dupliziert."""
    # Arrange
    await in_memory_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (2004, 'TG_STATUS_CANCEL', 'U12345', 'ENTRY', 'MSFT', 'STK', 'SMART', 'BUY', 5, 'LMT', 'Submitted')
        """
    )
    await in_memory_db.commit()

    mock_trade = MagicMock()
    mock_trade.order.orderId = 2004
    mock_trade.orderStatus.status = "Cancelled"
    mock_trade.orderStatus.permId = 88888
    mock_trade.orderStatus.avgFillPrice = 0.0
    mock_trade.orderStatus.whyHeld = ""
    mock_trade.contract.symbol = "MSFT"
    mock_trade.contract.secType = "STK"
    mock_trade.log = []

    with patch.object(
        callbacks_manager,
        "_is_near_or_after_market_close",
        return_value=False,
    ):
        # Act 1: Erstes Event
        callbacks_manager.on_order_status(mock_trade)
        await asyncio.sleep(0.05)

        # Assert 1
        assert mock_notifier.send_order_failed.await_count == 1

        # Act 2: Zweites Event (z. B. nachfolgende Fehlermeldung 202 für dieselbe Order)
        await callbacks_manager._cancel_order_in_db(
            request_id=2004,
            error_code=202,
            error_string="Order Canceled - reason: User cancel",
        )

        # Assert 2: Kein zweiter Alarm
        assert mock_notifier.send_order_failed.await_count == 1


@pytest.mark.asyncio
async def test_order_status_error_triggers_fatal_alarm(
    callbacks_manager: TwsCallbacksManager,
    in_memory_db: aiosqlite.Connection,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass ein orderStatusEvent('Error') oder unbekannter Status einen fatalen Alarm auslöst."""
    await in_memory_db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status)
        VALUES (2005, 'TG_ERROR_STATUS', 'U12345', 'ENTRY', 'NVDA', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Created')
        """
    )
    await in_memory_db.commit()

    mock_trade = MagicMock()
    mock_trade.order.orderId = 2005
    mock_trade.orderStatus.status = "Error"
    mock_trade.orderStatus.permId = 99999
    mock_trade.orderStatus.avgFillPrice = 0.0
    mock_trade.orderStatus.whyHeld = ""
    mock_trade.contract.symbol = "NVDA"
    mock_trade.contract.secType = "STK"
    mock_trade.log = []

    callbacks_manager.on_order_status(mock_trade)
    await asyncio.sleep(0.05)

    mock_notifier.send_order_failed.assert_awaited_once()
    call_kwargs = mock_notifier.send_order_failed.await_args.kwargs
    assert call_kwargs["order_id"] == 2005
    assert call_kwargs["symbol"] == "NVDA"
    assert call_kwargs["is_fatal"] is True


@pytest.mark.asyncio
async def test_order_status_extracts_reason_from_log_and_why_held(
    callbacks_manager: TwsCallbacksManager,
    in_memory_db: aiosqlite.Connection,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass on_order_status Stornogründe aus trade.log und trade.orderStatus.whyHeld extrahiert."""
    # Testfall 1: Aus trade.log
    mock_trade_log = MagicMock()
    mock_trade_log.order.orderId = 2006
    mock_trade_log.orderStatus.status = "Cancelled"
    mock_trade_log.orderStatus.permId = 12345
    mock_trade_log.orderStatus.avgFillPrice = 0.0
    mock_trade_log.orderStatus.whyHeld = ""
    mock_trade_log.contract.symbol = "AAPL"
    mock_trade_log.contract.secType = "STK"

    log_entry1 = MagicMock()
    log_entry1.message = "Initial routing"
    log_entry2 = MagicMock()
    log_entry2.message = "Cancelled by user"
    mock_trade_log.log = [log_entry1, log_entry2]

    with patch.object(
        callbacks_manager, "_is_near_or_after_market_close", return_value=False
    ):
        with patch.object(
            callbacks_manager, "_process_status_change", AsyncMock()
        ) as mock_process:
            callbacks_manager.on_order_status(mock_trade_log)
            await asyncio.sleep(0.01)
            mock_process.assert_called_once()
            assert mock_process.call_args.kwargs["reason"] == "Cancelled by user"

    # Testfall 2: Aus whyHeld
    mock_trade_held = MagicMock()
    mock_trade_held.order.orderId = 2007
    mock_trade_held.orderStatus.status = "Cancelled"
    mock_trade_held.orderStatus.permId = 12346
    mock_trade_held.orderStatus.avgFillPrice = 0.0
    mock_trade_held.orderStatus.whyHeld = "Locate required"
    mock_trade_held.contract.symbol = "AAPL"
    mock_trade_held.contract.secType = "STK"
    mock_trade_held.log = []

    with patch.object(
        callbacks_manager, "_is_near_or_after_market_close", return_value=False
    ):
        with patch.object(
            callbacks_manager, "_process_status_change", AsyncMock()
        ) as mock_process:
            callbacks_manager.on_order_status(mock_trade_held)
            await asyncio.sleep(0.01)
            mock_process.assert_called_once()
            assert mock_process.call_args.kwargs["reason"] == "Locate required"


@pytest.mark.asyncio
async def test_on_order_status_unhandled_exception_sends_emergency_alert(
    callbacks_manager: TwsCallbacksManager,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass unbehandelte Ausnahmen in on_order_status einen Notfall-Alarm senden."""
    broken_trade = MagicMock()
    # Zugriff auf orderId wirft Ausnahme
    type(broken_trade.order).orderId = property(lambda self: 1 / 0)

    callbacks_manager.on_order_status(broken_trade)
    await asyncio.sleep(0.05)

    mock_notifier.send_message.assert_awaited()
    sent_alert = mock_notifier.send_message.await_args[0][0]
    assert "KRITISCHER SYSTEMFEHLER IN ON_ORDER_STATUS" in sent_alert
    assert "division by zero" in sent_alert


@pytest.mark.asyncio
async def test_process_status_change_unhandled_exception_sends_emergency_alert(
    callbacks_manager: TwsCallbacksManager,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass unbehandelte Ausnahmen in _process_status_change einen Notfall-Alarm senden."""
    with patch.object(
        callbacks_manager,
        "_update_order_status_db",
        side_effect=RuntimeError("Corrupt status table"),
    ):
        await callbacks_manager._process_status_change(
            order_id=9999,
            mapped_status="Filled",
            permanent_id=1111,
        )

    mock_notifier.send_message.assert_awaited()
    sent_alert = mock_notifier.send_message.await_args[0][0]
    assert "KRITISCHER FEHLER IN STATUSVERARBEITUNG" in sent_alert
    assert "Corrupt status table" in sent_alert


@pytest.mark.asyncio
async def test_send_emergency_alert_handles_notifier_exception(
    callbacks_manager: TwsCallbacksManager,
    mock_notifier: MagicMock,
) -> None:
    """Verifiziert, dass _send_emergency_alert nicht abstürzt, wenn Telegram fehlschlägt."""
    mock_notifier.send_message = AsyncMock(side_effect=ConnectionError("Telegram down"))
    # Soll lautlos abgefangen und kritisch geloggt werden, keine unhandled exception
    await callbacks_manager._send_emergency_alert(
        title="Test Title",
        details="Test Details",
    )


def test_is_near_or_after_market_close_all_branches(
    callbacks_manager: TwsCallbacksManager,
) -> None:
    """Verifiziert alle Zweige von _is_near_or_after_market_close (None-Symbol, naive und timezoned Zeiten)."""
    ny_tz = ZoneInfo("America/New_York")

    # 1. symbol is None
    # Vor Cutoff (15:44 NY)
    t_before_ny = datetime(2026, 9, 11, 15, 44, 0, tzinfo=ny_tz)
    assert callbacks_manager._is_near_or_after_market_close(None, t_before_ny) is False

    # Nach Cutoff (15:45 NY)
    t_after_ny = datetime(2026, 9, 11, 15, 45, 0, tzinfo=ny_tz)
    assert callbacks_manager._is_near_or_after_market_close(None, t_after_ny) is True

    # Naive Zeit ohne tzinfo für symbol=None
    t_naive_ny = datetime(2026, 9, 11, 16, 5, 0)
    assert callbacks_manager._is_near_or_after_market_close(None, t_naive_ny) is True

    # current_time is None für symbol=None
    res_none = callbacks_manager._is_near_or_after_market_close(None, None)
    assert isinstance(res_none, bool)

    # 2. Deutscher Markt .DE
    # current_time is None
    res_de = callbacks_manager._is_near_or_after_market_close("SAP.DE", None)
    assert isinstance(res_de, bool)

    # Naive Zeit für .DE
    t_naive_de_before = datetime(2026, 9, 11, 17, 14, 0)
    assert (
        callbacks_manager._is_near_or_after_market_close("SAP.DE", t_naive_de_before)
        is False
    )
    t_naive_de_after = datetime(2026, 9, 11, 17, 16, 0)
    assert (
        callbacks_manager._is_near_or_after_market_close("SAP.DE", t_naive_de_after)
        is True
    )

    # 3. US Markt
    # current_time is None
    res_us = callbacks_manager._is_near_or_after_market_close("AAPL", None)
    assert isinstance(res_us, bool)

    # Naive Zeit für US
    t_naive_us_before = datetime(2026, 9, 11, 15, 44, 0)
    assert (
        callbacks_manager._is_near_or_after_market_close("AAPL", t_naive_us_before)
        is False
    )
    t_naive_us_after = datetime(2026, 9, 11, 15, 46, 0)
    assert (
        callbacks_manager._is_near_or_after_market_close("AAPL", t_naive_us_after)
        is True
    )


def test_is_market_closed_error_codes_all_branches() -> None:
    """Verifiziert alle Zweige von is_market_closed in error_codes.py."""
    from app.trading.error_codes import is_market_closed_for_symbol as is_market_closed

    # None current_time
    assert isinstance(is_market_closed("SAP.DE", None), bool)
    assert isinstance(is_market_closed("AAPL", None), bool)

    # Naive current_time
    t_de_closed = datetime(2026, 9, 11, 17, 31, 0)
    assert is_market_closed("SAP.DE", t_de_closed) is True

    t_us_closed = datetime(2026, 9, 11, 16, 1, 0)
    assert is_market_closed("AAPL", t_us_closed) is True
