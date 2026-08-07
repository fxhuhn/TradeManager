# filename: test_retry.py
"""
Unit tests for app.trading.retry module.
Verifies retry backoff calculations, re-queueing, max-retry threshold limits,
and error handling resilience.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from app.core.config import AppConfig, Config
from app.services.notifier import TelegramNotifier
from app.trading.retry import (
    _fetch_order_retry_info,
    handle_retriable_error,
)


@pytest.fixture
def test_config() -> Config:
    """Provides type-safe configuration with minimal retry delays for testing."""
    config_mock = MagicMock(spec=Config)
    app_config_mock = MagicMock(spec=AppConfig)
    app_config_mock.max_retries = 3
    app_config_mock.retry_backoff_base_s = 0.01  # Fast delay for test execution
    config_mock.app = app_config_mock
    return config_mock


@pytest.fixture
def mock_notifier() -> AsyncMock:
    """Provides mock TelegramNotifier with async send_message method."""
    notifier = AsyncMock(spec=TelegramNotifier)
    notifier.send_message = AsyncMock()
    return notifier


@pytest.mark.asyncio
async def test_handle_retriable_error_successful_backoff_and_requeue(
    db: aiosqlite.Connection, test_config: Config, mock_notifier: AsyncMock
) -> None:
    """Verifies that an order under max_retries undergoes backoff, status update to Created, and requeueing."""
    # Arrange
    order_id = 101
    trade_group_id = "TG_RETRY_001"
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status, retry_count)
        VALUES (?, ?, 'DU123456', 'ENTRY', 'AAPL', 'STK', 'SMART', 'BUY', 10, 'LMT', 'Submitted', 0)
        """,
        (order_id, trade_group_id),
    )
    await db.commit()

    # Prevent handler's finally block from closing shared test DB
    original_close = db.close
    db.close = AsyncMock()

    queue: asyncio.Queue[str] = asyncio.Queue()

    async def get_db_connection() -> aiosqlite.Connection:
        return db

    # Act
    await handle_retriable_error(
        db_factory=get_db_connection,
        order_id=order_id,
        queue=queue,
        notifier=mock_notifier,
        config=test_config,
    )

    # Assert
    async with db.execute(
        "SELECT status, retry_count FROM orders WHERE order_id = ?", (order_id,)
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Created"
        assert row["retry_count"] == 1

    assert not queue.empty()
    queued_item = await queue.get()
    assert queued_item == trade_group_id
    mock_notifier.send_message.assert_not_called()

    # Restore close
    db.close = original_close


@pytest.mark.asyncio
async def test_handle_retriable_error_exceeds_max_retries_marks_error_and_notifies(
    db: aiosqlite.Connection, test_config: Config, mock_notifier: AsyncMock
) -> None:
    """Verifies that an order reaching max_retries is marked as Error and sends Telegram notification."""
    # Arrange
    order_id = 102
    trade_group_id = "TG_RETRY_MAX"
    await db.execute(
        """
        INSERT INTO orders (order_id, trade_group_id, account_id, bracket_role, symbol, sec_type, exchange, action, quantity, order_type, status, retry_count)
        VALUES (?, ?, 'DU123456', 'ENTRY', 'MSFT', 'STK', 'SMART', 'BUY', 5, 'LMT', 'Submitted', 3)
        """,
        (order_id, trade_group_id),
    )
    await db.commit()

    # Prevent handler's finally block from closing shared test DB
    original_close = db.close
    db.close = AsyncMock()

    queue: asyncio.Queue[str] = asyncio.Queue()

    async def get_db_connection() -> aiosqlite.Connection:
        return db

    # Act
    await handle_retriable_error(
        db_factory=get_db_connection,
        order_id=order_id,
        queue=queue,
        notifier=mock_notifier,
        config=test_config,
    )

    # Assert
    async with db.execute(
        "SELECT status, retry_count FROM orders WHERE order_id = ?", (order_id,)
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "Error"
        assert row["retry_count"] == 3

    assert queue.empty()
    mock_notifier.send_message.assert_called_once()
    alert_text = mock_notifier.send_message.call_args[0][0]
    assert "RETRY-LIMIT EXCEEDED" in alert_text
    assert "MSFT" in alert_text

    # Restore close
    db.close = original_close


@pytest.mark.asyncio
async def test_handle_retriable_error_returns_early_when_order_not_found(
    db: aiosqlite.Connection, test_config: Config, mock_notifier: AsyncMock
) -> None:
    """Verifies that handle_retriable_error gracefully handles non-existent order_id."""
    # Arrange
    non_existent_order_id = 99999
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def get_db_connection() -> aiosqlite.Connection:
        return db

    # Act
    await handle_retriable_error(
        db_factory=get_db_connection,
        order_id=non_existent_order_id,
        queue=queue,
        notifier=mock_notifier,
        config=test_config,
    )

    # Assert
    assert queue.empty()
    mock_notifier.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retriable_error_handles_database_exception_gracefully(
    test_config: Config, mock_notifier: AsyncMock
) -> None:
    """Verifies that exceptions during retry handling are logged without bubbling up, and DB is closed."""
    # Arrange
    mock_db = MagicMock(spec=aiosqlite.Connection)
    mock_db.execute.side_effect = Exception("Simulated DB connection crash")
    mock_db.close = AsyncMock()

    async def get_failing_db() -> aiosqlite.Connection:
        return mock_db

    queue: asyncio.Queue[str] = asyncio.Queue()

    # Act & Assert
    try:
        await handle_retriable_error(
            db_factory=get_failing_db,
            order_id=500,
            queue=queue,
            notifier=mock_notifier,
            config=test_config,
        )
    except Exception as exc:
        pytest.fail(f"handle_retriable_error raised unexpected exception: {exc}")

    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_order_retry_info_returns_none_for_missing_order(
    db: aiosqlite.Connection,
) -> None:
    """Directly tests _fetch_order_retry_info returns None when query matches 0 rows."""
    # Act
    result = await _fetch_order_retry_info(db, order_id=88888)

    # Assert
    assert result is None
