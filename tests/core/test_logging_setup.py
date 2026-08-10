"""Tests for structured logging setup and rotation configuration."""

from pathlib import Path

import pytest
import structlog

from app.core.logging_setup import configure_logging


@pytest.mark.asyncio
async def test_configure_logging_creates_file_and_writes(tmp_path: Path) -> None:
    """
    Verifies that configure_logging() creates the log file
    and structlog output is clean of ANSI colors and formatted properly.
    """
    # 1. Temporaeren Logfile-Pfad festlegen
    temp_log_file = tmp_path / "test_app.log"

    # 2. Logger konfigurieren
    configure_logging(log_file_path=temp_log_file, backup_count=3)

    # 3. Test-Logmeldung absetzen
    logger = structlog.get_logger("test_logger")
    logger.info("Testnachricht fuer das rotierende Logfile", key_param="value_param")

    # 4. Verifizieren, dass die Datei existiert
    assert temp_log_file.exists()

    # 5. Inhalt verifizieren
    log_content = temp_log_file.read_text(encoding="utf-8")

    # Es sollte den Log-Text und die Key-Values enthalten
    assert "Testnachricht fuer das rotierende Logfile" in log_content
    assert "key_param=value_param" in log_content

    # Es darf KEINE ANSI-Farbcodes enthalten (wie z.B. ESC[32m oder \x1b)
    assert "\x1b" not in log_content

    # 6. Standard-Verhalten wiederherstellen (um andere Tests nicht zu beintraechtigen)
    configure_logging(log_file_path=Path(":memory:"), backup_count=1)


def test_simplify_ibkr_warning_non_matching() -> None:
    """Verifies that non-matching warnings are returned untouched."""
    from app.core.logging_setup import _simplify_ibkr_warning

    msg = "Some standard warning message"
    assert _simplify_ibkr_warning(msg) == msg


def test_simplify_ibkr_warning_with_why_held() -> None:
    """Verifies formatting of IBKR warning containing whyHeld."""
    from app.core.logging_setup import _simplify_ibkr_warning

    raw = (
        "IBKR API validation warning: Trade(contract=Contract(symbol='AAPL'), "
        "order=Order(orderId=101, action='BUY', totalQuantity=50, orderType='LMT', lmtPrice=150.5), "
        "whyHeld='Locate required')"
    )
    result = _simplify_ibkr_warning(raw)
    assert "BUY 50 AAPL (LMT @ 150.5)" in result
    assert "Held: Locate required" in result
    assert "OrderId: 101" in result


def test_simplify_ibkr_warning_with_warning_text() -> None:
    """Verifies formatting of IBKR warning containing warningText and auxPrice."""
    from app.core.logging_setup import _simplify_ibkr_warning

    raw = (
        "IBKR API validation warning: Trade(contract=Contract(symbol='TSLA'), "
        "order=Order(orderId=202, action='SELL', totalQuantity=10, orderType='STP', auxPrice=200.0), "
        "warningText='Order price outside limits')"
    )
    result = _simplify_ibkr_warning(raw)
    assert "SELL 10 TSLA (STP @ 200.0)" in result
    assert "Held: Order price outside limits" in result
    assert "OrderId: 202" in result


def test_simplify_ibkr_warning_without_held_reason() -> None:
    """Verifies formatting when no held reason is in the warning string."""
    from app.core.logging_setup import _simplify_ibkr_warning

    raw = "IBKR API validation warning: Trade(contract=Contract(symbol='MSFT'))"
    result = _simplify_ibkr_warning(raw)
    assert "UNKNOWN" in result
    assert "OrderId: UNKNOWN" in result
    assert result.startswith("IBKR API validation warning:")


def test_clean_ib_async_warnings_processor() -> None:
    """Verifies that the structlog processor simplifies matching events and passes others."""
    from app.core.logging_setup import clean_ib_async_warnings_processor

    # Non-string event
    event_dict = {"event": 12345}
    assert clean_ib_async_warnings_processor(None, "info", event_dict) == {
        "event": 12345
    }

    # Non-matching string event
    event_dict = {"event": "Normal log event"}
    assert clean_ib_async_warnings_processor(None, "info", event_dict) == {
        "event": "Normal log event"
    }

    # Matching event
    raw = (
        "IBKR API validation warning: Trade(contract=Contract(symbol='NVDA'), "
        "order=Order(orderId=505, action='BUY', totalQuantity=100, orderType='MKT'))"
    )
    event_dict = {"event": raw}
    processed = clean_ib_async_warnings_processor(None, "info", event_dict)
    assert "BUY 100 NVDA" in processed["event"]


def test_configure_logging_memory_mode() -> None:
    """Verifies configure_logging with :memory: path (no file handler created)."""
    configure_logging(log_file_path=Path(":memory:"), backup_count=1)
    logger = structlog.get_logger("memory_test")
    logger.info("Test memory logging")
