from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.models import LegRow, OrderRow
from app.services.alert_watcher import AlertState, check_dead_orders
from app.services.csv_reader import validate_group
from app.trading.error_codes import ErrorClass, classify_error_code
from app.trading.settlement import trigger_settlement


@pytest.mark.asyncio
async def test_csv_validation_fut_rejected():
    """FUT wird abgelehnt: Prüft, dass sec_type='FUT' korrekterweise abgewiesen wird."""
    invalid_legs = [
        LegRow(
            trade_group_id="20260530_Invalid",
            bracket_role="ENTRY",
            symbol="ES",
            sec_type="FUT",  # Ungültig
            exchange="SMART",
            account_id="DU12345",
            action="BUY",
            quantity=1,
            order_type="LMT",
            target_price=Decimal("5100.0"),
            tif="GTC",
            strategy_name="FuturesStrategy",
        )
    ]
    is_valid, error_message = validate_group("20260530_Invalid", invalid_legs)
    assert not is_valid
    assert "sec_type='STK' ist erlaubt" in error_message


@pytest.mark.asyncio
async def test_csv_validation_valid_bracket():
    """Validiert ein korrektes Bracket-Setup."""
    valid_legs = [
        LegRow(
            trade_group_id="20260530_Valid",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU12345",
            action="BUY",
            quantity=100,
            order_type="LMT",
            target_price=Decimal("180.0"),
            tif="GTC",
            strategy_name="Momentum",
        ),
        LegRow(
            trade_group_id="20260530_Valid",
            bracket_role="TP",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU12345",
            action="SELL",
            quantity=100,
            order_type="LMT",
            target_price=Decimal("190.0"),
            tif="GTC",
            strategy_name="Momentum",
        ),
    ]
    is_valid, error_message = validate_group("20260530_Valid", valid_legs)
    assert is_valid
    assert error_message == ""


@pytest.mark.asyncio
async def test_upsert_idempotency(db):
    """UPSERT-Idempotenz: Doppeltes Importieren aktualisiert nur Preis/Menge und erzeugt keine Duplikate."""
    # Erste Order anlegen
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 180.0, 'Created')
        """
    )
    await db.commit()

    # Zweiter Import simuliert durch manuelles Überschreiben/Update
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 200, 'LMT', 185.0, 'Created')
        ON CONFLICT(account_id, trade_group_id, bracket_role, order_type) DO UPDATE SET
            quantity = excluded.quantity,
            target_price = excluded.target_price
        """
    )
    await db.commit()

    # Prüfen, dass nur ein Eintrag existiert mit neuen Werten
    async with db.execute(
        "SELECT quantity, target_price FROM orders WHERE trade_group_id = 'G1'"
    ) as cursor:
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["quantity"] == 200
        assert rows[0]["target_price"] == 185.0


@pytest.mark.asyncio
async def test_upsert_protects_submitted(db):
    """UPSERT schützt Submitted: Aktive Orders werden nicht überschrieben."""
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 180.0, 'Submitted')
        """
    )
    await db.commit()

    # Versuche UPSERT mit geänderten Werten
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 200, 'LMT', 185.0, 'Created')
        ON CONFLICT(account_id, trade_group_id, bracket_role, order_type) DO UPDATE SET
            quantity = excluded.quantity,
            target_price = excluded.target_price
        WHERE status IN ('Created', 'Error')
        """
    )
    await db.commit()

    # Prüfen, dass Werte unberührt blieben da Status 'Submitted' war
    async with db.execute(
        "SELECT quantity, target_price FROM orders WHERE trade_group_id = 'G1'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row["quantity"] == 100
        assert row["target_price"] == 180.0


@pytest.mark.asyncio
async def test_settlement_vwap_calculation(db):
    """Settlement VWAP: Korrekte mengengewichtete PnL-Berechnung bei Partial Fills."""
    # 1. Orders eintragen
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 150.00, 'Filled')
        """
    )
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (2, 1, 'G1', 'A1', 'TP', 'AAPL', 'STK', 'SMART', 'SELL', 100, 'LMT', 155.00, 'Filled')
        """
    )
    await db.commit()

    # 2. Teilausführungen eintragen
    # Entry: 60 Shares zu $150.10 und 40 Shares zu $149.80 (VWAP = 149.98)
    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E1', 1, 150.10, 60, 1.0)"
    )
    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E2', 1, 149.80, 40, 1.0)"
    )

    # Exit: 100 Shares zu $155.05 (VWAP = 155.05)
    await db.execute(
        "INSERT INTO executions (exec_id, order_id, price, qty, commission) VALUES ('E3', 2, 155.05, 100, 2.0)"
    )
    await db.commit()

    # 3. Settlement ausführen
    # Mock db.close, damit die Test-Verbindung danach für Assertions geöffnet bleibt
    original_close = db.close
    db.close = AsyncMock()

    async def db_factory():
        return db

    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)

    try:
        await trigger_settlement(db_factory, "G1", "A1", mock_notifier)
    finally:
        # Verbindung nach dem Aufruf wiederherstellen, damit pytest sie ordentlich schließen kann
        db.close = original_close

    # 4. Prüfen der Ergebnisse in trades_settlement
    async with db.execute(
        "SELECT * FROM trades_settlement WHERE trade_group_id = 'G1'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row["avg_entry_price"] - 149.98) < 0.001
        assert abs(row["avg_exit_price"] - 155.05) < 0.001
        # slippage = target_price - avg_entry = 150.00 - 149.98 = +0.02
        assert abs(row["price_diff_slippage"] - 0.02) < 0.001
        assert row["total_commissions"] == 4.0
        # gross_pnl = (155.05 - 149.98) * 100 = 507.0
        # net_pnl = 507.0 - 4.0 = 503.0
        assert abs(row["net_pnl"] - 503.0) < 0.01


@pytest.mark.asyncio
async def test_error_code_classification():
    """Error: Klassifiziert TWS Fehler- und Info-Codes."""
    assert classify_error_code(2104) == ErrorClass.INFO
    assert classify_error_code(399) == ErrorClass.INFO
    assert classify_error_code(1101) == ErrorClass.RECONNECT
    assert classify_error_code(1100) == ErrorClass.RETRIABLE
    assert classify_error_code(202) == ErrorClass.CANCEL
    assert classify_error_code(201) == ErrorClass.FATAL


@pytest.mark.asyncio
async def test_alert_watcher_dead_orders(db):
    """Alert: Dead Order Check meldet hängende offene Orders."""
    state = AlertState()
    mock_notifier = MagicMock()
    mock_notifier.send_message = AsyncMock(return_value=True)

    # Hängende Order eintragen (transmitted_at liegt in der Vergangenheit)
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status, transmitted_at
        ) VALUES (1, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'MKT', 180.0, 'Submitted', '2026-05-30 10:00:00')
        """
    )
    await db.commit()

    # Check ausführen (Schwellenwert 15 Minuten)
    from datetime import UTC, datetime

    # 2026-06-04 14:00:00 UTC corresponds to Thursday, 10:00:00 AM NY time (active trading hours)
    test_time = datetime(2026, 6, 4, 14, 0, 0, tzinfo=UTC)
    await check_dead_orders(
        db, mock_notifier, state, threshold_minutes=15, current_time=test_time
    )

    # Verifizieren, dass der Alarm gesendet wurde
    mock_notifier.send_message.assert_called_once()
    assert state.is_order_reported(1)


@pytest.mark.asyncio
async def test_order_builder_order_ref():
    """Verify that build_order() correctly sets the TWS orderRef field to the strategy name."""
    from app.trading.order_builder import build_order

    order_row = OrderRow(
        order_id=42,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("180.0"),
        tif="GTC",
        strategy_name="NDXMomentum",
        status="Created",
    )
    tws_order = build_order(order_row)
    assert tws_order.orderRef == "NDXMomentum"


def test_calculate_settlement_pure():
    """Verify that calculate_settlement accurately computes VWAP, slippage, and net PnL (pure core)."""
    from app.trading.settlement import (
        ExecutionTuple,
        SettlementInput,
        calculate_settlement,
    )

    inputs = SettlementInput(
        entry_executions=[
            ExecutionTuple(quantity=Decimal("60"), price=Decimal("150.10")),
            ExecutionTuple(quantity=Decimal("40"), price=Decimal("149.80")),
        ],
        exit_executions=[
            ExecutionTuple(quantity=Decimal("100"), price=Decimal("155.05")),
        ],
        entry_target_price=Decimal("150.00"),
        entry_action="BUY",
        total_commissions=Decimal("4.00"),
    )

    output = calculate_settlement(inputs)

    assert abs(output.avg_entry_price - Decimal("149.98")) < Decimal("0.001")
    assert abs(output.avg_exit_price - Decimal("155.05")) < Decimal("0.001")
    assert abs(output.price_diff_slippage - Decimal("0.02")) < Decimal("0.001")
    assert abs(output.net_profit_loss - Decimal("503.00")) < Decimal("0.01")


@pytest.mark.asyncio
async def test_place_and_verify_order_warning_399(db):
    """Prüft, dass _place_and_verify_order bei Warnung 399 (ValidationError) Erfolg (True) zurückgibt."""
    from app.trading.worker import _place_and_verify_order

    # 1. Database setup
    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (42, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 180.0, 'Submitted')
        """
    )
    await db.commit()

    order_row = OrderRow(
        order_id=42,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("180.0"),
        tif="GTC",
        strategy_name="NDXMomentum",
        status="Submitted",
    )

    # 2. Mocking IB / Trade / Log
    mock_log_entry = MagicMock()
    mock_log_entry.errorCode = 399
    mock_log_entry.status = "ValidationError"
    mock_log_entry.message = "Warning 399: order held"

    mock_trade = MagicMock()
    mock_trade.orderStatus.status = "ValidationError"
    mock_trade.log = [mock_log_entry]

    mock_ib = MagicMock()
    mock_ib.placeOrder.return_value = mock_trade

    mock_notifier = AsyncMock()
    mock_notifier.send_message = AsyncMock(return_value=True)

    # 3. Call _place_and_verify_order
    result = await _place_and_verify_order(
        db=db,
        interactive_brokers=mock_ib,
        contract=MagicMock(),
        ib_order=MagicMock(),
        order_row=order_row,
        tws_order_id=42,
        notifier=mock_notifier,
    )

    # 4. Assertions
    assert result is True
    # Der Status der Order in der DB sollte unverändert bleiben (nicht auf Error gesetzt)
    async with db.execute("SELECT status FROM orders WHERE order_id = 42") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Submitted"


@pytest.mark.asyncio
async def test_place_and_verify_order_real_error(db):
    """Prüft, dass _place_and_verify_order bei einem echten Fehler False zurückgibt und DB-Status auf Error setzt."""
    from app.trading.worker import _place_and_verify_order

    await db.execute(
        """
        INSERT INTO orders (
            order_id, parent_id, trade_group_id, account_id, bracket_role,
            symbol, sec_type, exchange, action, quantity, order_type, target_price, status
        ) VALUES (43, NULL, 'G1', 'A1', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 100, 'LMT', 180.0, 'Submitted')
        """
    )
    await db.commit()

    order_row = OrderRow(
        order_id=43,
        perm_id=None,
        parent_id=None,
        trade_group_id="G1",
        account_id="A1",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("180.0"),
        tif="GTC",
        strategy_name="NDXMomentum",
        status="Submitted",
    )

    mock_log_entry = MagicMock()
    mock_log_entry.errorCode = 201
    mock_log_entry.status = "ValidationError"
    mock_log_entry.message = "Order rejected"

    mock_trade = MagicMock()
    mock_trade.orderStatus.status = "ValidationError"
    mock_trade.log = [mock_log_entry]

    mock_ib = MagicMock()
    mock_ib.placeOrder.return_value = mock_trade

    mock_notifier = AsyncMock()

    result = await _place_and_verify_order(
        db=db,
        interactive_brokers=mock_ib,
        contract=MagicMock(),
        ib_order=MagicMock(),
        order_row=order_row,
        tws_order_id=43,
        notifier=mock_notifier,
    )

    assert result is False
    async with db.execute("SELECT status FROM orders WHERE order_id = 43") as cursor:
        row = await cursor.fetchone()
        assert row["status"] == "Error"
