# filename: tests/trading/test_error_codes.py
"""Unit tests for TWS error code classification in app.trading.error_codes."""

import pytest

from app.trading.error_codes import ErrorClass, classify_error_code


@pytest.mark.asyncio
async def test_error_code_classification() -> None:
    """TWS Error-Code Klassifizierung (INFO vs RETRIABLE vs FATAL vs RECONNECT vs CANCEL)."""
    assert classify_error_code(2104) == ErrorClass.INFO
    assert classify_error_code(1100) == ErrorClass.RETRIABLE
    assert classify_error_code(1101) == ErrorClass.RECONNECT
    assert classify_error_code(202) == ErrorClass.CANCEL
    assert classify_error_code(9999) == ErrorClass.FATAL
