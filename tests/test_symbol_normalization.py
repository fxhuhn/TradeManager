"""Unit tests for symbol normalization and IBKR position matching logic."""

from decimal import Decimal
from unittest.mock import MagicMock

from app.trading.order_builder import make_stock_contract, normalize_symbol
from app.trading.recovery import _has_live_position
from app.trading.worker import _get_live_position_quantity


def test_normalize_symbol_strips_dot_de_suffix() -> None:
    """Verifies that normalize_symbol removes .DE suffix and upper-cases ticker."""
    assert normalize_symbol("SXRV.DE") == "SXRV"
    assert normalize_symbol("sxrv.de") == "SXRV"
    assert normalize_symbol("  sxrv.de  ") == "SXRV"


def test_normalize_symbol_preserves_standard_symbols() -> None:
    """Verifies that normalize_symbol preserves tickers without exchange suffixes."""
    assert normalize_symbol("AAPL") == "AAPL"
    assert normalize_symbol("msft") == "MSFT"


def test_normalize_symbol_converts_us_share_class_separators() -> None:
    """Verifies that normalize_symbol converts hyphens and dots in US share classes to spaces."""
    assert normalize_symbol("BF-B") == "BF B"
    assert normalize_symbol("BF.B") == "BF B"
    assert normalize_symbol("BRK-B") == "BRK B"
    assert normalize_symbol("BRK.B") == "BRK B"
    assert normalize_symbol("  bf-b  ") == "BF B"


def test_make_stock_contract_with_dot_de_suffix() -> None:
    """Verifies that make_stock_contract configures XETRA parameters for .DE symbols."""
    contract = make_stock_contract("SXRV.DE")
    assert contract.symbol == "SXRV"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "EUR"
    assert contract.primaryExchange == "IBIS2"


def test_make_stock_contract_for_us_stock() -> None:
    """Verifies that make_stock_contract configures USD SMART routing for US symbols."""
    contract = make_stock_contract("AAPL")
    assert contract.symbol == "AAPL"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"


def test_make_stock_contract_for_us_share_class() -> None:
    """Verifies that make_stock_contract normalizes US share class symbol BF-B to BF B."""
    contract = make_stock_contract("BF-B")
    assert contract.symbol == "BF B"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"


def test_get_live_position_quantity_matches_dot_de_symbol() -> None:
    """Verifies that _get_live_position_quantity matches DB symbol SXRV.DE against IBKR position SXRV."""
    mock_position = MagicMock()
    mock_position.account = "ACCOUNT_123"
    mock_position.contract.symbol = "SXRV"
    mock_position.position = 5.0

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]

    quantity = _get_live_position_quantity(mock_ib, "ACCOUNT_123", "SXRV.DE")
    assert quantity == Decimal("5.0")


def test_has_live_position_matches_dot_de_symbol() -> None:
    """Verifies that _has_live_position returns True when matching SXRV.DE against IBKR position SXRV."""
    mock_position = MagicMock()
    mock_position.account = "ACCOUNT_123"
    mock_position.contract.symbol = "SXRV"
    mock_position.position = 5.0

    mock_ib = MagicMock()
    mock_ib.positions.return_value = [mock_position]

    assert _has_live_position(mock_ib, "ACCOUNT_123", "SXRV.DE") is True
    assert _has_live_position(mock_ib, "ACCOUNT_999", "SXRV.DE") is False
