# filename: tests/trading/test_order_builder_gtd.py
"""
Unit and edge-case tests for LOC/MOC GTD cutoff logic in app.trading.order_builder.

Edge-Case Attack Matrix:
| Test ID       | Category              | Input Conditions                                     | Expected Outcome                            |
|---------------|-----------------------|------------------------------------------------------|---------------------------------------------|
| TC_STRUC_01   | Structural Boundary   | TP + LMT with sibling LOC                            | should_apply_loc_gtd == True                |
| TC_STRUC_02   | Structural Boundary   | EXIT + LMT with sibling MOC                          | should_apply_loc_gtd == True                |
| TC_STRUC_03   | Structural Boundary   | SL + STP with sibling LOC                            | should_apply_loc_gtd == False (SL protected)|
| TC_STRUC_04   | Structural Boundary   | ENTRY + LMT with sibling LOC                         | should_apply_loc_gtd == False (Entry safe)  |
| TC_STRUC_05   | Structural Boundary   | TP + LMT with siblings SL (STP) and TP (LMT)         | should_apply_loc_gtd == False (No LOC/MOC)  |
| TC_STRUC_06   | Structural Boundary   | TP + LMT with empty sibling_orders                   | should_apply_loc_gtd == False               |
| TC_STRUC_07   | Structural Boundary   | EXIT + LOC with sibling LMT                          | should_apply_loc_gtd == False (LOC is child)|
| TC_STRUC_08   | Structural Boundary   | TP + MKT with sibling LOC                            | should_apply_loc_gtd == False               |
| TC_STRUC_09   | Structural Boundary   | Sibling list contains child itself as only LOC order | should_apply_loc_gtd == False (ID equality) |
| TC_TIME_01    | Temporal Calculation  | US symbol (AAPL) at 10:00:00                         | Cutoff: 'YYYYMMDD 15:48:00 US/Eastern'      |
| TC_TIME_02    | Temporal Calculation  | German symbol (SXRV.DE) at 10:00:00                  | Cutoff: 'YYYYMMDD 17:18:00 Europe/Berlin'   |
| TC_TIME_03    | Temporal Calculation  | Naive reference datetime                             | Handled and localized to target timezone    |
| TC_TIME_04    | Temporal Calculation  | Aware UTC reference datetime                         | Converted to target timezone correctly      |
| TC_TIME_05    | Temporal Boundary     | US symbol exactly at 15:47:59                        | is_past_loc_gtd_cutoff == False             |
| TC_TIME_06    | Temporal Boundary     | US symbol exactly at 15:48:00                        | is_past_loc_gtd_cutoff == True              |
| TC_TIME_07    | Temporal Boundary     | US symbol at 15:52:00 (after cutoff)                 | is_past_loc_gtd_cutoff == True              |
| TC_TIME_08    | Temporal Boundary     | German symbol (.DE) at 17:17:59                      | is_past_loc_gtd_cutoff == False             |
| TC_TIME_09    | Temporal Boundary     | German symbol (.DE) at 17:18:00                      | is_past_loc_gtd_cutoff == True              |
| TC_TIME_10    | Temporal Boundary     | Current time None (uses system time)                 | Executes without error                      |
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core.models import OrderRow
from app.trading.order_builder import (
    compute_loc_gtd_cutoff,
    is_past_loc_gtd_cutoff,
    should_apply_loc_gtd,
)


def _create_test_order(
    order_id: int,
    bracket_role: str,
    order_type: str,
    symbol: str = "AAPL",
    target_price: Decimal | None = Decimal("150.0"),
    quantity: int = 10,
    tif: str = "GTC",
) -> OrderRow:
    """Helper creating typed immutable OrderRow instances for testing."""
    return OrderRow(
        order_id=order_id,
        perm_id=None,
        parent_id=None,
        trade_group_id="TG_TEST_LOC",
        account_id="U1234567",
        bracket_role=bracket_role,
        symbol=symbol,
        sec_type="STK",
        exchange="SMART",
        action="SELL" if bracket_role != "ENTRY" else "BUY",
        quantity=quantity,
        order_type=order_type,
        target_price=target_price,
        tif=tif,
        strategy_name="Momentum",
        status="Created",
        retry_count=0,
        transmitted_at=None,
    )


# --- Tests for should_apply_loc_gtd ---


def test_should_apply_loc_gtd_returns_true_for_take_profit_with_loc_sibling() -> None:
    """Verifies that a TP LMT order gets GTD when a LOC sibling is present."""
    # Arrange
    child = _create_test_order(order_id=2, bracket_role="TP", order_type="LMT")
    loc_sibling = _create_test_order(order_id=3, bracket_role="EXIT", order_type="LOC")
    sl_sibling = _create_test_order(order_id=4, bracket_role="SL", order_type="STP")
    sibling_orders = [child, loc_sibling, sl_sibling]

    # Act
    result = should_apply_loc_gtd(child, sibling_orders)

    # Assert
    assert result is True


def test_should_apply_loc_gtd_returns_true_for_exit_role_with_moc_sibling() -> None:
    """Verifies that an EXIT LMT order gets GTD when an MOC sibling is present."""
    # Arrange
    child = _create_test_order(order_id=2, bracket_role="EXIT", order_type="LMT")
    moc_sibling = _create_test_order(order_id=3, bracket_role="EXIT", order_type="MOC")
    sibling_orders = [child, moc_sibling]

    # Act
    result = should_apply_loc_gtd(child, sibling_orders)

    # Assert
    assert result is True


def test_should_apply_loc_gtd_returns_false_for_stop_loss_order() -> None:
    """Verifies that Stop Loss orders NEVER receive GTD, guaranteeing capital protection."""
    # Arrange
    sl_child = _create_test_order(order_id=2, bracket_role="SL", order_type="STP")
    loc_sibling = _create_test_order(order_id=3, bracket_role="EXIT", order_type="LOC")
    sibling_orders = [sl_child, loc_sibling]

    # Act
    result = should_apply_loc_gtd(sl_child, sibling_orders)

    # Assert
    assert result is False


def test_should_apply_loc_gtd_returns_false_for_entry_order() -> None:
    """Verifies that ENTRY orders never receive GTD."""
    # Arrange
    entry_child = _create_test_order(order_id=1, bracket_role="ENTRY", order_type="LMT")
    loc_sibling = _create_test_order(order_id=3, bracket_role="EXIT", order_type="LOC")
    sibling_orders = [entry_child, loc_sibling]

    # Act
    result = should_apply_loc_gtd(entry_child, sibling_orders)

    # Assert
    assert result is False


def test_should_apply_loc_gtd_returns_false_when_no_closing_sibling_exists() -> None:
    """Verifies that standard brackets without LOC/MOC do not apply GTD."""
    # Arrange
    child = _create_test_order(order_id=2, bracket_role="TP", order_type="LMT")
    sl_sibling = _create_test_order(order_id=3, bracket_role="SL", order_type="STP")
    second_lmt = _create_test_order(order_id=4, bracket_role="TP", order_type="LMT")
    sibling_orders = [child, sl_sibling, second_lmt]

    # Act
    result = should_apply_loc_gtd(child, sibling_orders)

    # Assert
    assert result is False


def test_should_apply_loc_gtd_returns_false_on_empty_siblings() -> None:
    """Verifies that empty sibling collection safely returns False without exceptions."""
    # Arrange
    child = _create_test_order(order_id=2, bracket_role="TP", order_type="LMT")

    # Act
    result = should_apply_loc_gtd(child, [])

    # Assert
    assert result is False


def test_should_apply_loc_gtd_returns_false_for_loc_child_itself() -> None:
    """Verifies that the LOC order itself does not get flagged for GTD modification."""
    # Arrange
    loc_child = _create_test_order(order_id=3, bracket_role="EXIT", order_type="LOC")
    lmt_sibling = _create_test_order(order_id=2, bracket_role="TP", order_type="LMT")
    sibling_orders = [loc_child, lmt_sibling]

    # Act
    result = should_apply_loc_gtd(loc_child, sibling_orders)

    # Assert
    assert result is False


def test_should_apply_loc_gtd_returns_false_when_sibling_is_self() -> None:
    """Verifies that an order is not evaluated against its own order ID."""
    # Arrange
    loc_child = _create_test_order(order_id=3, bracket_role="TP", order_type="LMT")
    # Same ID
    sibling_orders = [loc_child]

    # Act
    result = should_apply_loc_gtd(loc_child, sibling_orders)

    # Assert
    assert result is False


# --- Tests for compute_loc_gtd_cutoff ---


def test_compute_loc_gtd_cutoff_for_us_symbol_formats_correctly() -> None:
    """Verifies US equity cutoff calculation: 15:48:00 US/Eastern."""
    # Arrange
    reference_time = datetime(
        2026, 9, 10, 10, 30, 0, tzinfo=ZoneInfo("America/New_York")
    )

    # Act
    cutoff_string = compute_loc_gtd_cutoff("AAPL", reference_time=reference_time)

    # Assert
    assert cutoff_string == "20260910 15:48:00 US/Eastern"


def test_compute_loc_gtd_cutoff_for_german_symbol_formats_correctly() -> None:
    """Verifies German equity cutoff calculation (.DE): 17:18:00 Europe/Berlin."""
    # Arrange
    reference_time = datetime(2026, 9, 10, 10, 30, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    # Act
    cutoff_string = compute_loc_gtd_cutoff("SXRV.DE", reference_time=reference_time)

    # Assert
    assert cutoff_string == "20260910 17:18:00 Europe/Berlin"


def test_compute_loc_gtd_cutoff_handles_naive_datetime() -> None:
    """Verifies naive datetime inputs are properly localized to the market timezone."""
    # Arrange
    naive_reference = datetime(2026, 12, 1, 9, 0, 0)

    # Act
    us_cutoff = compute_loc_gtd_cutoff("MSFT", reference_time=naive_reference)
    de_cutoff = compute_loc_gtd_cutoff("SAP.DE", reference_time=naive_reference)

    # Assert
    assert us_cutoff == "20261201 15:48:00 US/Eastern"
    assert de_cutoff == "20261201 17:18:00 Europe/Berlin"


def test_compute_loc_gtd_cutoff_converts_utc_datetime_accurately() -> None:
    """Verifies timezone conversion from UTC to US/Eastern."""
    # Arrange
    utc_time = datetime(2026, 7, 15, 14, 0, 0, tzinfo=ZoneInfo("UTC"))

    # Act
    cutoff_string = compute_loc_gtd_cutoff("NVDA", reference_time=utc_time)

    # Assert
    assert cutoff_string == "20260715 15:48:00 US/Eastern"


def test_compute_loc_gtd_cutoff_without_reference_time_uses_now() -> None:
    """Verifies that omitting reference_time runs without raising an exception."""
    # Act
    cutoff_us = compute_loc_gtd_cutoff("GOOGL")
    cutoff_de = compute_loc_gtd_cutoff("BMW.DE")

    # Assert
    assert "US/Eastern" in cutoff_us
    assert "Europe/Berlin" in cutoff_de


# --- Tests for is_past_loc_gtd_cutoff ---


@pytest.mark.parametrize(
    "current_time, expected_result",
    [
        (
            datetime(2026, 9, 10, 15, 47, 59, tzinfo=ZoneInfo("America/New_York")),
            False,
        ),
        (
            datetime(2026, 9, 10, 15, 48, 0, tzinfo=ZoneInfo("America/New_York")),
            True,
        ),
        (
            datetime(2026, 9, 10, 15, 48, 1, tzinfo=ZoneInfo("America/New_York")),
            True,
        ),
        (
            datetime(2026, 9, 10, 16, 0, 0, tzinfo=ZoneInfo("America/New_York")),
            True,
        ),
    ],
)
def test_is_past_loc_gtd_cutoff_us_symbol_boundaries(
    current_time: datetime, expected_result: bool
) -> None:
    """Verifies exact second-precision boundary behavior for US equities."""
    # Act
    result = is_past_loc_gtd_cutoff("TSLA", current_time=current_time)

    # Assert
    assert result is expected_result


@pytest.mark.parametrize(
    "current_time, expected_result",
    [
        (
            datetime(2026, 9, 10, 17, 17, 59, tzinfo=ZoneInfo("Europe/Berlin")),
            False,
        ),
        (
            datetime(2026, 9, 10, 17, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            True,
        ),
        (
            datetime(2026, 9, 10, 17, 18, 1, tzinfo=ZoneInfo("Europe/Berlin")),
            True,
        ),
        (
            datetime(2026, 9, 10, 17, 30, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            True,
        ),
    ],
)
def test_is_past_loc_gtd_cutoff_german_symbol_boundaries(
    current_time: datetime, expected_result: bool
) -> None:
    """Verifies exact second-precision boundary behavior for German equities (.DE)."""
    # Act
    result = is_past_loc_gtd_cutoff("SXRV.DE", current_time=current_time)

    # Assert
    assert result is expected_result


def test_is_past_loc_gtd_cutoff_handles_naive_datetime() -> None:
    """Verifies that naive datetime is localized correctly when checking past cutoff."""
    # Arrange
    before_cutoff = datetime(2026, 9, 10, 14, 0, 0)
    after_cutoff = datetime(2026, 9, 10, 15, 50, 0)

    # Act & Assert
    assert is_past_loc_gtd_cutoff("AAPL", current_time=before_cutoff) is False
    assert is_past_loc_gtd_cutoff("AAPL", current_time=after_cutoff) is True


def test_is_past_loc_gtd_cutoff_without_current_time_runs() -> None:
    """Verifies calling without current_time executes using current system clock."""
    # Act
    result = is_past_loc_gtd_cutoff("AAPL")

    # Assert
    assert isinstance(result, bool)
