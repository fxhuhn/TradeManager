"""Tests für DockerContainerManager Service."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from app.services.container_manager import DockerContainerManager


def test_is_available_returns_true_when_socket_exists(tmp_path: Path) -> None:
    """Verifiziert, dass is_available() True liefert, wenn die Socket-Datei existiert."""
    fake_socket = tmp_path / "docker.sock"
    fake_socket.touch()

    manager = DockerContainerManager(docker_socket_path=str(fake_socket))
    assert manager.is_available() is True


def test_is_available_returns_false_when_socket_missing(tmp_path: Path) -> None:
    """Verifiziert, dass is_available() False liefert, wenn die Datei nicht existiert."""
    fake_socket = tmp_path / "missing.sock"

    manager = DockerContainerManager(docker_socket_path=str(fake_socket))
    assert manager.is_available() is False


def test_is_available_handles_os_error() -> None:
    """Verifiziert, dass OSError sauber abgefangen wird."""
    manager = DockerContainerManager(docker_socket_path="/root/forbidden/docker.sock")
    with patch("pathlib.Path.exists", side_effect=PermissionError("Forbidden")):
        assert manager.is_available() is False


@pytest.mark.asyncio
async def test_restart_container_fails_when_socket_unavailable() -> None:
    """Verifiziert, dass restart_container abbricht, wenn der Socket nicht verfügbar ist."""
    manager = DockerContainerManager(docker_socket_path="/tmp/nonexistent.sock")
    success, message = await manager.restart_container("ibkr")

    assert success is False
    assert "nicht verfügbar" in message


def _make_mock_client_session_context(
    mock_response: AsyncMock | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    """Erzeugt einen konformen Mock für aiohttp.ClientSession und post() Kontextmanager."""
    mock_post_context = MagicMock()
    if side_effect:
        mock_post_context.__aenter__ = AsyncMock(side_effect=side_effect)
    else:
        mock_post_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_context.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    if side_effect:
        mock_session.post = MagicMock(side_effect=side_effect)
    else:
        mock_session.post = MagicMock(return_value=mock_post_context)

    mock_session_context = MagicMock()
    mock_session_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_context.__aexit__ = AsyncMock(return_value=False)
    return mock_session_context


@pytest.mark.asyncio
async def test_restart_container_success_204(tmp_path: Path) -> None:
    """Verifiziert erfolgreichen Neustart bei HTTP 204 No Content."""
    fake_socket = tmp_path / "docker.sock"
    fake_socket.touch()

    mock_response = AsyncMock()
    mock_response.status = 204

    mock_session_ctx = _make_mock_client_session_context(mock_response=mock_response)

    manager = DockerContainerManager(docker_socket_path=str(fake_socket))
    with (
        patch("aiohttp.UnixConnector", return_value=MagicMock()),
        patch("aiohttp.ClientSession", return_value=mock_session_ctx),
    ):
        success, message = await manager.restart_container("ibkr", timeout_seconds=5)

    assert success is True
    assert "erfolgreich neu gestartet" in message


@pytest.mark.asyncio
async def test_restart_container_not_found_404(tmp_path: Path) -> None:
    """Verifiziert Fehlerbehandlung bei HTTP 404 Not Found."""
    fake_socket = tmp_path / "docker.sock"
    fake_socket.touch()

    mock_response = AsyncMock()
    mock_response.status = 404

    mock_session_ctx = _make_mock_client_session_context(mock_response=mock_response)

    manager = DockerContainerManager(docker_socket_path=str(fake_socket))
    with (
        patch("aiohttp.UnixConnector", return_value=MagicMock()),
        patch("aiohttp.ClientSession", return_value=mock_session_ctx),
    ):
        success, message = await manager.restart_container("ibkr")

    assert success is False
    assert "nicht gefunden (404)" in message


@pytest.mark.asyncio
async def test_restart_container_api_error_500(tmp_path: Path) -> None:
    """Verifiziert Fehlerbehandlung bei HTTP 500."""
    fake_socket = tmp_path / "docker.sock"
    fake_socket.touch()

    mock_response = AsyncMock()
    mock_response.status = 500
    mock_response.text.return_value = "daemon error"

    mock_session_ctx = _make_mock_client_session_context(mock_response=mock_response)

    manager = DockerContainerManager(docker_socket_path=str(fake_socket))
    with (
        patch("aiohttp.UnixConnector", return_value=MagicMock()),
        patch("aiohttp.ClientSession", return_value=mock_session_ctx),
    ):
        success, message = await manager.restart_container("ibkr")

    assert success is False
    assert "Docker-API Fehler 500" in message


@pytest.mark.asyncio
async def test_restart_container_timeout_error(tmp_path: Path) -> None:
    """Verifiziert Fehlerbehandlung bei TimeoutError."""
    fake_socket = tmp_path / "docker.sock"
    fake_socket.touch()

    mock_session_ctx = _make_mock_client_session_context(
        side_effect=TimeoutError("Request timed out")
    )

    manager = DockerContainerManager(docker_socket_path=str(fake_socket))
    with (
        patch("aiohttp.UnixConnector", return_value=MagicMock()),
        patch("aiohttp.ClientSession", return_value=mock_session_ctx),
    ):
        success, message = await manager.restart_container("ibkr")

    assert success is False
    assert "Timeout beim Neustart" in message


@pytest.mark.asyncio
async def test_restart_container_client_error(tmp_path: Path) -> None:
    """Verifiziert Fehlerbehandlung bei aiohttp.ClientError."""
    fake_socket = tmp_path / "docker.sock"
    fake_socket.touch()

    mock_session_ctx = _make_mock_client_session_context(
        side_effect=aiohttp.ClientConnectionError("Connection refused")
    )

    manager = DockerContainerManager(docker_socket_path=str(fake_socket))
    with (
        patch("aiohttp.UnixConnector", return_value=MagicMock()),
        patch("aiohttp.ClientSession", return_value=mock_session_ctx),
    ):
        success, message = await manager.restart_container("ibkr")

    assert success is False
    assert "Netzwerk-/Socket-Fehler" in message


@pytest.mark.asyncio
async def test_restart_container_unexpected_exception(tmp_path: Path) -> None:
    """Verifiziert Fehlerbehandlung bei unvorhergesehenen Ausnahmen."""
    fake_socket = tmp_path / "docker.sock"
    fake_socket.touch()

    mock_session_ctx = _make_mock_client_session_context(
        side_effect=RuntimeError("Fatal hardware failure")
    )

    manager = DockerContainerManager(docker_socket_path=str(fake_socket))
    with (
        patch("aiohttp.UnixConnector", return_value=MagicMock()),
        patch("aiohttp.ClientSession", return_value=mock_session_ctx),
    ):
        success, message = await manager.restart_container("ibkr")

    assert success is False
    assert "Unerwarteter Fehler" in message
