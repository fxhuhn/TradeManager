# filename: test_notifier.py
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.notifier import (
    AsyncTelegramRateLimiter,
    TelegramNotifier,
    _strip_html,
)


@pytest.fixture
def mock_config() -> MagicMock:
    """Fixture providing a mock configuration with active Telegram settings."""
    config = MagicMock()
    config.telegram.bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    config.telegram.chat_id = "987654321"
    config.telegram.rate_limit_delay_s = 0.1
    config.telegram.request_timeout_s = 5.0
    return config


def test_strip_html_removes_tags() -> None:
    """Verifies that _strip_html correctly strips HTML tags for logging."""
    # Arrange
    html_text = "<b>Hello</b> <a href='test'>World</a>"

    # Act
    cleaned = _strip_html(html_text)

    # Assert
    assert cleaned == "Hello World"


def test_strip_html_handles_empty_string() -> None:
    """Verifies that _strip_html returns an empty string when input is empty."""
    # Arrange
    text = ""

    # Act
    cleaned = _strip_html(text)

    # Assert
    assert cleaned == ""


@pytest.mark.asyncio
async def test_rate_limiter_enforces_delay() -> None:
    """Verifies that AsyncTelegramRateLimiter calls asyncio.sleep with correct duration."""
    # Arrange
    limiter = AsyncTelegramRateLimiter(delay_seconds=1.5)

    # Mock time.monotonic to return sequential values: 100.0, 100.5, 101.0, 102.0
    # First call: now = 100.0, elapsed = 100.0 >= 1.5 -> No sleep, last_sent = 100.5
    # Second call: now = 101.0, elapsed = 101.0 - 100.5 = 0.5 < 1.5 -> sleep_time = 1.0, last_sent = 102.0
    with (
        patch("time.monotonic", side_effect=[100.0, 100.5, 101.0, 102.0]),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        # Act
        await limiter.wait()  # First call
        await limiter.wait()  # Second call

        # Assert
        mock_sleep.assert_called_once_with(1.0)


@pytest.mark.asyncio
async def test_notifier_with_dummy_token_is_inactive(
    mock_config: MagicMock,
) -> None:
    """Verifies that TelegramNotifier detects DUMMY bot token and operates in inactive mock mode."""
    # Arrange
    mock_config.telegram.bot_token = "DUMMY_TOKEN"
    notifier = TelegramNotifier(mock_config)

    # Act
    result = await notifier.send_message("Hello Test")

    # Assert
    assert not notifier.is_active
    assert result is True


@pytest.mark.asyncio
async def test_send_message_success(mock_config: MagicMock) -> None:
    """Verifies that send_message returns True when Telegram API returns 200."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    mock_response = AsyncMock()
    mock_response.status = 200

    # The context manager returned by post(...)
    mock_post_context = MagicMock()
    mock_post_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_context.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_post_context)

    # The context manager returned by ClientSession()
    mock_client_session_context = MagicMock()
    mock_client_session_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_client_session_context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("aiohttp.ClientSession", return_value=mock_client_session_context),
        patch.object(notifier.limiter, "wait", new_callable=AsyncMock) as mock_wait,
    ):
        # Act
        result = await notifier.send_message("Success Msg")

        # Assert
        assert result is True
        mock_wait.assert_called_once()
        mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_api_error(mock_config: MagicMock) -> None:
    """Verifies that send_message returns False when Telegram API returns non-200 status."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    mock_response = AsyncMock()
    mock_response.status = 400
    mock_response.text.return_value = "Bad Request"

    mock_post_context = MagicMock()
    mock_post_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_context.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_post_context)

    mock_client_session_context = MagicMock()
    mock_client_session_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_client_session_context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("aiohttp.ClientSession", return_value=mock_client_session_context),
        patch.object(notifier.limiter, "wait", new_callable=AsyncMock),
    ):
        # Act
        result = await notifier.send_message("Error Msg")

        # Assert
        assert result is False


@pytest.mark.asyncio
async def test_send_message_handles_http_exception(
    mock_config: MagicMock,
) -> None:
    """Verifies that send_message returns False and handles HTTP exceptions gracefully."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(side_effect=Exception("Connection Refused"))

    mock_client_session_context = MagicMock()
    mock_client_session_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_client_session_context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("aiohttp.ClientSession", return_value=mock_client_session_context),
        patch.object(notifier.limiter, "wait", new_callable=AsyncMock),
    ):
        # Act
        result = await notifier.send_message("Fail Msg")

        # Assert
        assert result is False


@pytest.mark.asyncio
async def test_send_system_status(mock_config: MagicMock) -> None:
    """Verifies that send_system_status formats the status message correctly."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_system_status(title="SYSTEM START", emoji="🚀")

        # Assert
        mock_send.assert_called_once()
        called_text = mock_send.call_args[0][0]
        assert "🚀 <b>IBKR: SYSTEM START</b>" in called_text
        assert "Time:" in called_text


@pytest.mark.asyncio
async def test_send_order_filled_formatting(mock_config: MagicMock) -> None:
    """Verifies that send_order_filled constructs a correct HTML message."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_order_filled(
            symbol="AAPL",
            bracket_role="ENTRY",
            action="BUY",
            quantity=Decimal("100"),
            execution_price=Decimal("150.50"),
            order_type="LMT",
            order_id=123,
            strategy_name="Momentum",
        )

        # Assert
        mock_send.assert_called_once()
        called_text = mock_send.call_args[0][0]
        assert "🟢 <b>ORDER GEFÜLLT</b> | <code>AAPL</code>" in called_text
        assert "<b>Typ:</b> <code>ENTRY</code> (BUY)" in called_text
        assert (
            "<b>Menge:</b> <code>100</code> @ <code>150.50</code> (LMT)" in called_text
        )
        assert "<b>Wert:</b> <code>$ 15,050.00</code>" in called_text
        assert "ID: <code>123</code>" in called_text
        assert "<i>Momentum</i>" in called_text


@pytest.mark.asyncio
async def test_send_order_filled_market_price(mock_config: MagicMock) -> None:
    """Verifies that send_order_filled handles None execution price as market execution."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_order_filled(
            symbol="AAPL",
            bracket_role="ENTRY",
            action="BUY",
            quantity=Decimal("100"),
            execution_price=None,
            order_type="MKT",
            order_id=123,
            strategy_name="Momentum",
        )

        # Assert
        mock_send.assert_called_once()
        called_text = mock_send.call_args[0][0]
        assert "@ <code>MKT</code>" in called_text
        assert "<b>Wert:</b> <code>$ 0.00</code>" in called_text


@pytest.mark.asyncio
async def test_send_order_failed_formatting(mock_config: MagicMock) -> None:
    """Verifies that send_order_failed constructs correct error messages."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        # Fatal error
        await notifier.send_order_failed(
            order_id=123,
            tws_code=201,
            reason="Order rejected",
            symbol="AAPL",
            bracket_role="ENTRY",
            is_fatal=True,
        )

        # Warning/Cancel
        await notifier.send_order_failed(
            order_id=456,
            tws_code=202,
            reason="User cancelled",
            symbol="MSFT",
            bracket_role="SL",
            is_fatal=False,
        )

        # Assert
        assert mock_send.call_count == 2
        fatal_msg = mock_send.call_args_list[0][0][0]
        cancel_msg = mock_send.call_args_list[1][0][0]

        assert "🚨 <b>ORDER FEHLGESCHLAGEN</b> | <code>ID: 123</code>" in fatal_msg
        assert "Symbol/Typ:</b> <code>AAPL</code> (ENTRY)" in fatal_msg
        assert "TWS-Code:</b> <code>201</code>" in fatal_msg
        assert "Grund:</b> <i>Order rejected</i>" in fatal_msg

        assert "🚫 <b>ORDER CANCELED</b> | <code>ID: 456</code>" in cancel_msg


@pytest.mark.asyncio
async def test_send_importer_info_formatting(mock_config: MagicMock) -> None:
    """Verifies that send_importer_info constructs correct import statistics message."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_importer_info(
            file_name="orders_2026_07_03.csv",
            status="SUCCESS",
            details="Imported 5 trade groups.",
        )

        # Assert
        mock_send.assert_called_once()
        called_text = mock_send.call_args[0][0]
        assert (
            "📁 <b>DATEN IMPORT</b> | <code>orders_2026_07_03.csv</code>" in called_text
        )
        assert "<b>Status:</b> <code>SUCCESS</code>" in called_text
        assert "<b>Details:</b> <i>Imported 5 trade groups.</i>" in called_text


@pytest.mark.asyncio
async def test_send_bracket_order_submitted_empty(
    mock_config: MagicMock,
) -> None:
    """Verifies that send_bracket_order_submitted returns False immediately when order list is empty."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    result = await notifier.send_bracket_order_submitted(
        symbol="AAPL", trade_group_id="G1", strategy_name="Momentum", orders=[]
    )

    # Assert
    assert result is False


@pytest.mark.asyncio
async def test_send_bracket_order_submitted_formatting(
    mock_config: MagicMock,
) -> None:
    """Verifies that send_bracket_order_submitted formats single and multiple orders correctly."""
    # Arrange
    notifier = TelegramNotifier(mock_config)
    single_order = [
        {
            "role": "ENTRY",
            "action": "BUY",
            "quantity": 100,
            "order_type": "LMT",
            "price": 150.0,
        }
    ]
    multiple_orders = [
        {
            "role": "ENTRY",
            "action": "BUY",
            "quantity": 100,
            "order_type": "LMT",
            "price": 150.0,
        },
        {
            "role": "SL",
            "action": "SELL",
            "quantity": 100,
            "order_type": "STP",
            "price": 140.0,
        },
    ]

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        # Test single order title
        await notifier.send_bracket_order_submitted(
            symbol="AAPL",
            trade_group_id="",
            strategy_name="Momentum",
            orders=single_order,
        )

        # Test multiple orders title and group id
        await notifier.send_bracket_order_submitted(
            symbol="MSFT",
            trade_group_id="G123",
            strategy_name="Momentum",
            orders=multiple_orders,
        )

        # Assert
        assert mock_send.call_count == 2
        single_msg = mock_send.call_args_list[0][0][0]
        multi_msg = mock_send.call_args_list[1][0][0]

        assert "📤 <b>ORDER GESENDET</b> | <code>AAPL</code>" in single_msg
        assert "ENTRY:</b> <code>BUY 100</code> @ <code>150.00</code>" in single_msg
        assert "System:</b> <i>Momentum</i>" in single_msg

        assert "📤 <b>BRACKET ORDER GESENDET</b> | <code>MSFT</code>" in multi_msg
        assert "ENTRY:</b> <code>BUY 100</code> @ <code>150.00</code>" in multi_msg
        assert "SL:</b> <code>SELL 100</code> @ <code>140.00</code>" in multi_msg
        assert "Group: <code>G123</code> • <i>Momentum</i>" in multi_msg


@pytest.mark.asyncio
async def test_send_margin_limit_exceeded(mock_config: MagicMock) -> None:
    """Verifies that send_margin_limit_exceeded formats the limits correctly."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_margin_limit_exceeded(
            symbol="AAPL",
            account_id="DU123",
            init_margin_after=Decimal("150000"),
            limit_value=Decimal("120000"),
            cushion_percentage=Decimal("8.5"),
        )

        # Assert
        mock_send.assert_called_once()
        called_text = mock_send.call_args[0][0]
        assert "🚨 <b>MARGIN-LIMIT ÜBERSCHRITTEN</b>" in called_text
        assert "DU123" in called_text
        assert "$ 150,000.00" in called_text
        assert "$ 120,000.00" in called_text
        assert "8.5%" in called_text


@pytest.mark.asyncio
async def test_send_margin_utilization_warning(
    mock_config: MagicMock,
) -> None:
    """Verifies that send_margin_utilization_warning formats correctly."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_margin_utilization_warning(
            symbol="AAPL",
            account_id="DU123",
            purchase_value=Decimal("80000"),
            total_cash=Decimal("50000"),
            margin_needed=Decimal("30000"),
        )

        # Assert
        mock_send.assert_called_once()
        called_text = mock_send.call_args[0][0]
        assert "ℹ️ <b>MARGIN-NUTZUNG ERFORDERLICH</b>" in called_text
        assert "$ 80,000.00" in called_text
        assert "$ 50,000.00" in called_text
        assert "$ 30,000.00" in called_text


@pytest.mark.asyncio
async def test_send_high_margin_usage_warning(mock_config: MagicMock) -> None:
    """Verifies that send_high_margin_usage_warning formats correctly."""
    # Arrange
    notifier = TelegramNotifier(mock_config)

    # Act
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_high_margin_usage_warning(
            symbol="AAPL",
            account_id="DU123",
            usage_percentage=Decimal("55.4"),
            init_margin_after=Decimal("110000"),
            net_liquidation=Decimal("200000"),
        )

        # Assert
        mock_send.assert_called_once()
        called_text = mock_send.call_args[0][0]
        assert "⚠️ <b>HOHE MARGIN-AUSLASTUNG (>50%)</b>" in called_text
        assert "55.4%" in called_text
        assert "$ 110,000.00" in called_text
        assert "$ 200,000.00" in called_text
