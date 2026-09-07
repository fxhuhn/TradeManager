"""Demolition test suite for TradeManager service layer.

Targets edge conditions, numerical extremes, boundary shifts, and temporal anomalies
across telegram_bot, alert_watcher, importer, and account_metrics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from app.core.config import Config, TelegramConfig
from app.services.account_metrics import (
    AccountMetricsSnapshot,
    get_latest_account_metrics,
    save_account_metrics,
    sync_and_save_account_metrics,
)
from app.services.alert_watcher import (
    AlertState,
    check_archived_error_files,
    check_dead_orders,
    check_high_slippage,
)
from app.services.importer import (
    calculate_downscaled_quantity,
    determine_maximum_capital_allocation,
    resolve_account_id,
)
from app.services.telegram_bot import TelegramCommandListener

# =====================================================================
# 1. ACCOUNT METRICS EDGE CASES & PRECISION PARANOIA
# =====================================================================


@pytest.mark.asyncio
async def test_account_metrics_save_and_retrieve_financial_precision(
    db: aiosqlite.Connection,
) -> None:
    """Verifies that high-precision monetary values are preserved without float drift."""
    # Arrange
    account_id = "U1234567"
    snapshot = AccountMetricsSnapshot(
        account_id=account_id,
        net_liquidation=Decimal("125430.85"),
        total_cash_value=Decimal("45120.12"),
        available_funds=Decimal("80310.73"),
        maint_margin_req=Decimal("25120.50"),
        cushion_pct=Decimal("68.75"),
        buying_power=Decimal("321240.00"),
    )

    # Act
    await save_account_metrics(db, account_id, snapshot)
    retrieved = await get_latest_account_metrics(db, account_id)

    # Assert
    assert retrieved is not None
    assert retrieved.account_id == account_id
    assert retrieved.net_liquidation == Decimal("125430.85")
    assert retrieved.total_cash_value == Decimal("45120.12")
    assert retrieved.available_funds == Decimal("80310.73")
    assert retrieved.cushion_pct == Decimal("68.75")


@pytest.mark.asyncio
async def test_get_latest_account_metrics_returns_none_on_database_exception() -> None:
    """Verifies that database execution errors are caught and return None gracefully."""
    # Arrange
    mock_db = MagicMock(spec=aiosqlite.Connection)
    mock_db.execute.side_effect = RuntimeError("SQLite disk I/O failure")

    # Act
    result = await get_latest_account_metrics(mock_db, "U1234567")

    # Assert
    assert result is None


@pytest.mark.asyncio
async def test_sync_and_save_account_metrics_handles_fetch_exception(
    db: aiosqlite.Connection,
) -> None:
    """Verifies that failures during IBKR account balance retrieval return None."""
    # Arrange
    mock_ib = MagicMock()
    with patch(
        "app.services.importer.fetch_account_balance_metrics",
        new=AsyncMock(side_effect=TimeoutError("IBKR TWS socket timeout")),
    ):
        # Act
        result = await sync_and_save_account_metrics(
            interactive_brokers=mock_ib,
            account_id="U1234567",
            database_connection=db,
        )

        # Assert
        assert result is None


# =====================================================================
# 2. IMPORTER CAPITAL SIZING & ALLOCATION MUTANT KILLERS
# =====================================================================


@pytest.mark.parametrize(
    "net_liq, available, total_cash, margin_factor, mode, limit_pct, expected",
    [
        # Structural boundary: Zero net liquidation
        (
            Decimal("0.0"),
            Decimal("10000.0"),
            Decimal("5000.0"),
            Decimal("1.0"),
            "margin",
            Decimal("0.10"),
            Decimal("0.0"),
        ),
        # Structural boundary: Zero available funds
        (
            Decimal("100000.0"),
            Decimal("0.0"),
            Decimal("5000.0"),
            Decimal("1.0"),
            "margin",
            Decimal("0.10"),
            Decimal("0.0"),
        ),
        # Total cash mode ignores net liq and limit pct
        (
            Decimal("100000.0"),
            Decimal("50000.0"),
            Decimal("12345.67"),
            Decimal("2.0"),
            "total_cash",
            Decimal("0.10"),
            Decimal("12345.67"),
        ),
        # Buying power bottleneck (available funds < theoretical margin limit)
        (
            Decimal("100000.0"),
            Decimal("2000.0"),
            Decimal("5000.0"),
            Decimal("2.0"),
            "margin",
            Decimal("0.20"),
            Decimal("4000.0"),  # min(100k*2*0.2=40k, 2k*2=4k) -> 4k
        ),
        # Margin limit bottleneck (theoretical margin limit < available funds)
        (
            Decimal("50000.0"),
            Decimal("50000.0"),
            Decimal("50000.0"),
            Decimal("1.0"),
            "margin",
            Decimal("0.10"),
            Decimal("5000.0"),  # min(50k*1*0.1=5k, 50k*1=50k) -> 5k
        ),
    ],
)
def test_determine_maximum_capital_allocation_boundary_conditions(
    net_liq: Decimal,
    available: Decimal,
    total_cash: Decimal,
    margin_factor: Decimal,
    mode: str,
    limit_pct: Decimal,
    expected: Decimal,
) -> None:
    """Verifies allocation boundaries, margin bottlenecks, and total cash modes."""
    # Act
    actual = determine_maximum_capital_allocation(
        net_liquidation_value=net_liq,
        available_funds_value=available,
        total_cash_value=total_cash,
        margin_multiplier_factor=margin_factor,
        sizing_mode=mode,
        allocation_limit_percentage=limit_pct,
    )

    # Assert
    assert actual == expected


@pytest.mark.parametrize(
    "target_qty, target_price, max_allocation, expected_qty",
    [
        # Target price is None -> bypass downscaling
        (100, None, Decimal("500.0"), 100),
        # Target price is Zero -> bypass downscaling
        (100, Decimal("0.0"), Decimal("500.0"), 100),
        # Cost exactly matches max allocation -> no downscale
        (10, Decimal("50.0"), Decimal("500.0"), 10),
        # Cost exceeds max allocation -> truncated integer downscale
        (10, Decimal("60.0"), Decimal("500.0"), 8),  # 500 // 60 = 8
        # Max allocation insufficient for single share -> 0
        (5, Decimal("600.0"), Decimal("500.0"), 0),
        # Negative allocation -> floor at 0
        (10, Decimal("50.0"), Decimal("-100.0"), 0),
    ],
)
def test_calculate_downscaled_quantity_edge_cases(
    target_qty: int,
    target_price: Decimal | None,
    max_allocation: Decimal,
    expected_qty: int,
) -> None:
    """Verifies integer floor rounding and zero-boundary enforcement in downscaling."""
    # Act
    actual_qty = calculate_downscaled_quantity(
        target_quantity=target_qty,
        target_price=target_price,
        max_allocation=max_allocation,
    )

    # Assert
    assert actual_qty == expected_qty


def test_resolve_account_id_boundary_scenarios() -> None:
    """Verifies account resolution with empty managed accounts, matches, and fallbacks."""
    # Arrange
    mock_ib = MagicMock()

    # Case 1: Empty managed accounts returns requested account
    mock_ib.managedAccounts.return_value = []
    assert resolve_account_id(mock_ib, "U9999") == "U9999"

    # Case 2: Non-list return value returns requested account
    mock_ib.managedAccounts.return_value = None
    assert resolve_account_id(mock_ib, "U9999") == "U9999"

    # Case 3: Exact match returns requested account
    mock_ib.managedAccounts.return_value = ["U1111", "U2222"]
    assert resolve_account_id(mock_ib, "U2222") == "U2222"

    # Case 4: Mismatch falls back to primary managed account
    assert resolve_account_id(mock_ib, "UNKNOWN") == "U1111"


# =====================================================================
# 3. ALERT WATCHER TEMPORAL ANOMALIES & MUTATION TESTS
# =====================================================================


@pytest.mark.asyncio
async def test_check_dead_orders_ignores_weekend_executions() -> None:
    """Verifies that dead order checks abort immediately on Saturday and Sunday."""
    # Arrange
    mock_db = MagicMock(spec=aiosqlite.Connection)
    mock_notifier = MagicMock()
    state = AlertState()
    # Saturday at 14:00 NY time
    saturday_ny = datetime(2026, 9, 5, 14, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    # Act
    await check_dead_orders(
        db=mock_db,
        notifier=mock_notifier,
        state=state,
        threshold_minutes=15,
        current_time=saturday_ny,
    )

    # Assert: DB query must not even be executed
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_check_dead_orders_ignores_pre_market_and_post_market_hours() -> None:
    """Verifies dead order checks ignore hours before 09:30 and after 16:30 NY."""
    # Arrange
    mock_db = MagicMock(spec=aiosqlite.Connection)
    mock_notifier = MagicMock()
    state = AlertState()
    # Wednesday at 09:15 NY time (Pre-market)
    pre_market = datetime(2026, 9, 2, 9, 15, 0, tzinfo=ZoneInfo("America/New_York"))

    # Act
    await check_dead_orders(
        db=mock_db,
        notifier=mock_notifier,
        state=state,
        threshold_minutes=15,
        current_time=pre_market,
    )

    # Assert
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_check_dead_orders_moc_activation_boundary(
    db: aiosqlite.Connection,
) -> None:
    """Verifies that MOC orders only trigger dead order alerts after 16:00 + threshold."""
    # Arrange
    mock_notifier = AsyncMock()
    mock_notifier.send_message.return_value = True
    state = AlertState()
    ny_tz = ZoneInfo("America/New_York")

    # Order transmitted at 10:00 AM NY
    transmitted_ny = datetime(2026, 9, 2, 10, 0, 0, tzinfo=ny_tz)
    transmitted_utc = transmitted_ny.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

    await db.execute(
        """
        INSERT INTO orders (
            order_id, trade_group_id, account_id, bracket_role, symbol,
            sec_type, exchange, action, quantity, order_type, status, transmitted_at
        ) VALUES (1001, 'TG_MOC', 'U123', 'EXIT', 'AAPL', 'STK', 'SMART', 'SELL', 10, 'MOC', 'Submitted', ?)
        """,
        (transmitted_utc,),
    )
    await db.commit()

    # Time 1: 16:10 NY (Only 10 minutes past close -> threshold 15 min not exceeded)
    current_time_1 = datetime(2026, 9, 2, 16, 10, 0, tzinfo=ny_tz)
    await check_dead_orders(
        db=db,
        notifier=mock_notifier,
        state=state,
        threshold_minutes=15,
        current_time=current_time_1,
    )
    assert not state.is_order_reported(1001)

    # Time 2: 16:20 NY (20 minutes past close -> threshold 15 min exceeded)
    current_time_2 = datetime(2026, 9, 2, 16, 20, 0, tzinfo=ny_tz)
    await check_dead_orders(
        db=db,
        notifier=mock_notifier,
        state=state,
        threshold_minutes=15,
        current_time=current_time_2,
    )
    assert state.is_order_reported(1001)
    mock_notifier.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_check_high_slippage_ignores_zero_and_none_targets(
    db: aiosqlite.Connection,
) -> None:
    """Verifies that MKT/MOC orders without target prices do not trigger false slippage alerts."""
    # Arrange
    mock_notifier = AsyncMock()
    state = AlertState()

    await db.execute(
        """
        INSERT INTO orders (
            order_id, trade_group_id, account_id, bracket_role, symbol,
            sec_type, exchange, action, quantity, order_type, status, target_price
        ) VALUES (2001, 'TG_NOSLIP', 'U123', 'ENTRY', 'MSFT', 'STK', 'SMART', 'BUY', 10, 'MKT', 'Filled', NULL)
        """
    )
    await db.execute(
        """
        INSERT INTO trades_settlement (
            account_id, trade_group_id, avg_entry_price, avg_exit_price,
            price_diff_slippage, total_commissions, net_pnl
        ) VALUES ('U123', 'TG_NOSLIP', '400.0', '410.0', '50.0', '1.0', '99.0')
        """
    )
    await db.commit()

    # Act
    await check_high_slippage(
        db=db,
        notifier=mock_notifier,
        state=state,
        max_slippage_percentage=0.01,
    )

    # Assert
    assert not state.is_group_reported("TG_NOSLIP")
    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_archived_error_files_reports_and_deduplicates(
    tmp_path: Path,
) -> None:
    """Verifies detection of .err files and immediate deduplication in AlertState."""
    # Arrange
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    err_file = archive_dir / "orders_2026_09_02.csv.err"
    err_file.write_text("bad,data\n")

    mock_notifier = AsyncMock()
    mock_notifier.send_archived_error_alert.return_value = True
    state = AlertState()

    # Act: First run detects and reports
    await check_archived_error_files(archive_dir, mock_notifier, state)
    assert state.is_file_reported("orders_2026_09_02.csv.err")
    assert mock_notifier.send_archived_error_alert.call_count == 1

    # Act: Second run skips reporting
    await check_archived_error_files(archive_dir, mock_notifier, state)
    assert mock_notifier.send_archived_error_alert.call_count == 1


# =====================================================================
# 4. TELEGRAM BOT SECURITY & RESTART DEBOUNCE
# =====================================================================


@pytest.mark.asyncio
async def test_telegram_bot_rejects_unauthorized_chat_id() -> None:
    """Verifies that inbound messages and callback queries from alien chat IDs are dropped."""
    # Arrange
    config = MagicMock(spec=Config)
    config.telegram = MagicMock(spec=TelegramConfig)
    config.telegram.enable_commands = True
    config.telegram.bot_token = "123456:ABC-DEF"
    config.telegram.chat_id = "100200300"

    mock_notifier = AsyncMock()
    mock_container_manager = AsyncMock()
    mock_reconnect = AsyncMock()

    listener = TelegramCommandListener(
        config=config,
        notifier=mock_notifier,
        container_manager=mock_container_manager,
        trigger_reconnect_callback=mock_reconnect,
    )

    # Act: Unauthorized command
    unauthorized_update = {
        "update_id": 1,
        "message": {"chat": {"id": "999999999"}, "text": "/restart_ibkr"},
    }
    await listener._process_single_update(unauthorized_update)

    # Assert
    mock_container_manager.restart_container.assert_not_called()
    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_bot_enforces_exact_restart_debounce_boundary() -> None:
    """Verifies that container restarts triggered within debounce window are throttled."""
    # Arrange
    config = MagicMock(spec=Config)
    config.telegram = MagicMock(spec=TelegramConfig)
    config.telegram.enable_commands = True
    config.telegram.bot_token = "123456:ABC-DEF"
    config.telegram.chat_id = "100200300"
    config.telegram.ibkr_container_name = "ibkr-gateway"

    mock_notifier = AsyncMock()
    mock_container_manager = AsyncMock()
    mock_container_manager.restart_container.return_value = (True, "Restarted OK")
    mock_reconnect = AsyncMock()

    listener = TelegramCommandListener(
        config=config,
        notifier=mock_notifier,
        container_manager=mock_container_manager,
        trigger_reconnect_callback=mock_reconnect,
        restart_debounce_seconds=60.0,
    )

    with patch("time.monotonic") as mock_monotonic:
        # T=100: Initial restart attempt
        mock_monotonic.return_value = 100.0
        await listener._execute_ibkr_restart_flow()
        assert mock_container_manager.restart_container.call_count == 1

        # T=130: 30s later (Within 60s window) -> Must debounce!
        mock_monotonic.return_value = 130.0
        await listener._execute_ibkr_restart_flow()
        assert mock_container_manager.restart_container.call_count == 1
        assert (
            "Neustart bereits in Arbeit" in mock_notifier.send_message.call_args[0][0]
        )

        # T=161: 61s later (Debounce elapsed) -> Must allow restart!
        mock_monotonic.return_value = 161.0
        await listener._execute_ibkr_restart_flow()
        assert mock_container_manager.restart_container.call_count == 2
