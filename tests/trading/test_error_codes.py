# filename: tests/trading/test_error_codes.py
"""Unit tests for TWS error code classification in app.trading.error_codes."""

from unittest.mock import MagicMock

import pytest

from app.trading.error_codes import (
    ErrorClass,
    classify_error_code,
    is_pre_market_hold_notice,
    is_trade_pre_market_held,
)


@pytest.mark.asyncio
async def test_error_code_classification() -> None:
    """TWS Error-Code Klassifizierung (INFO vs RETRIABLE vs FATAL vs RECONNECT vs CANCEL)."""
    assert classify_error_code(2104) == ErrorClass.INFO
    assert classify_error_code(2109) == ErrorClass.INFO
    assert classify_error_code(2157) == ErrorClass.INFO
    assert classify_error_code(2158) == ErrorClass.INFO
    assert classify_error_code(10167) == ErrorClass.INFO
    assert classify_error_code(321) == ErrorClass.INFO
    assert classify_error_code(322) == ErrorClass.INFO
    assert classify_error_code(1100) == ErrorClass.RETRIABLE
    assert classify_error_code(1101) == ErrorClass.RECONNECT
    assert classify_error_code(202) == ErrorClass.CANCEL
    assert classify_error_code(9999) == ErrorClass.FATAL


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (
            201,
            "Order rejected - reason:BEFORE WE CAN ACCEPT YOUR ORDER IN THIS SECURITY, PLEASE LOGIN TO CLIENT PORTAL AND VERIFY USING THE TOKEN WE EMAILED TO YOU.",
            True,
        ),
        (
            201,
            "PLEASE LOGIN TO CLIENT PORTAL AND VERIFY USING THE TOKEN WE <br>EMAILED TO YOU.",
            True,
        ),
        (0, "Please verify using the token sent to your email", True),
        (0, "verification process required in client portal", True),
        (201, "Order rejected - token required", True),
        (1100, "Connectivity between IB and Trader Workstation has been lost.", False),
        (399, "Order will not be routed until next regular trading session.", False),
        (202, "Order Canceled - reason:", False),
        (0, "Normal order fill received", False),
    ],
)
def test_is_reauthorization_error(code: int, message: str, expected: bool) -> None:
    """Verifies that is_reauthorization_error correctly flags token prompts and ignores standard errors."""
    from app.trading.error_codes import is_reauthorization_error

    assert is_reauthorization_error(code, message) is expected


def test_is_market_closed_for_symbol_xetra() -> None:
    """Verifies market close detection for German equities (.DE) at 17:30 Berlin time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.trading.error_codes import is_market_closed_for_symbol

    berlin_tz = ZoneInfo("Europe/Berlin")

    # Before 17:30
    time_open = datetime(2026, 9, 2, 17, 29, 59, tzinfo=berlin_tz)
    assert is_market_closed_for_symbol("SXRV.DE", current_time=time_open) is False

    # Exactly 17:30
    time_close = datetime(2026, 9, 2, 17, 30, 0, tzinfo=berlin_tz)
    assert is_market_closed_for_symbol("SXRV.DE", current_time=time_close) is True

    # After 17:30
    time_after = datetime(2026, 9, 2, 17, 35, 0, tzinfo=berlin_tz)
    assert is_market_closed_for_symbol("SXRV.DE", current_time=time_after) is True


def test_is_market_closed_for_symbol_us() -> None:
    """Verifies market close detection for US equities at 16:00 New York time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.trading.error_codes import is_market_closed_for_symbol

    ny_tz = ZoneInfo("America/New_York")

    # Before 16:00
    time_open = datetime(2026, 9, 2, 15, 59, 59, tzinfo=ny_tz)
    assert is_market_closed_for_symbol("AAPL", current_time=time_open) is False

    # Exactly 16:00
    time_close = datetime(2026, 9, 2, 16, 0, 0, tzinfo=ny_tz)
    assert is_market_closed_for_symbol("AAPL", current_time=time_close) is True

    # After 16:00
    time_after = datetime(2026, 9, 2, 16, 5, 0, tzinfo=ny_tz)
    assert is_market_closed_for_symbol("AAPL", current_time=time_after) is True


@pytest.mark.parametrize(
    "code,message,why_held,expected",
    [
        (399, "", "", True),
        (2109, "", "", True),
        (
            0,
            "Your order will not be placed at the exchange until 09:30 US/Eastern",
            "",
            True,
        ),
        (
            0,
            "",
            "Warning: your order will not be placed at the exchange until 09:30 US/Eastern",
            True,
        ),
        (
            0,
            "Order will not be routed to the exchange until market opens",
            "",
            True,
        ),
        (0, "Order submitted outside regular trading hours", "", True),
        (201, "Order rejected - insufficient funds", "", False),
        (103, "Duplicate order id", "", False),
        (0, "Regular order fill", "", False),
    ],
)
def test_is_pre_market_hold_notice(
    code: int, message: str, why_held: str, expected: bool
) -> None:
    """Verifies that is_pre_market_hold_notice correctly detects hold notices."""
    assert is_pre_market_hold_notice(code, message, why_held) is expected


def test_is_trade_pre_market_held() -> None:
    """Verifies that is_trade_pre_market_held evaluates trade logs and whyHeld properly."""

    # Trade with warning 399 only
    trade1 = MagicMock()
    trade1.orderStatus.whyHeld = ""
    log_entry1 = MagicMock()
    log_entry1.errorCode = 399
    log_entry1.message = (
        "Your order will not be placed at the exchange until 09:30 US/Eastern"
    )
    trade1.log = [log_entry1]
    assert is_trade_pre_market_held(trade1) is True

    # Trade with warning in whyHeld
    trade2 = MagicMock()
    trade2.orderStatus.whyHeld = (
        "Your order will not be placed at the exchange until 09:30 US/Eastern"
    )
    trade2.log = []
    assert is_trade_pre_market_held(trade2) is True

    # Trade with warning 399 AND real fatal error (code 201)
    trade3 = MagicMock()
    trade3.orderStatus.whyHeld = ""
    log_entry_error = MagicMock()
    log_entry_error.errorCode = 201
    log_entry_error.message = "Order rejected - insufficient margin"
    trade3.log = [log_entry1, log_entry_error]
    assert is_trade_pre_market_held(trade3) is False

    # Trade without hold indication
    trade4 = MagicMock()
    trade4.orderStatus.whyHeld = ""
    trade4.log = []
    assert is_trade_pre_market_held(trade4) is False
