"""Docker Container Manager Service für TradeManager.

Ermöglicht den asynchronen Zugriff auf den lokalen Docker-Daemon über den
UNIX Domain Socket (/var/run/docker.sock), um Container wie IBKR Gateway
kontrolliert neu zu starten.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import aiohttp
import structlog

logger = structlog.get_logger()

DEFAULT_DOCKER_SOCKET_PATH: Final[str] = "/var/run/docker.sock"
DEFAULT_RESTART_TIMEOUT_SECONDS: Final[int] = 10


@dataclass(frozen=True)
class ContainerStatusReport:
    """Ergebnis einer Container-Status- und Health-Abfrage."""

    name_or_id: str
    exists: bool
    is_running: bool = False
    status: str = "unknown"
    health_status: str | None = None
    exit_code: int | None = None
    error_message: str | None = None


class DockerContainerManager:
    """Verwaltet Docker-Container über den lokalen UNIX Domain Socket."""

    def __init__(
        self,
        docker_socket_path: str = DEFAULT_DOCKER_SOCKET_PATH,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        """Initialisiert den DockerContainerManager.

        Args:
            docker_socket_path: Dateipfad zum Docker UNIX Domain Socket.
            request_timeout_seconds: Timeout für HTTP-Aufrufe an die Docker-API.
        """
        self._docker_socket_path = docker_socket_path
        self._request_timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)

    def is_available(self) -> bool:
        """Prüft, ob der Docker UNIX Domain Socket existiert und ansprechbar ist.

        Returns:
            bool: True, falls die Socket-Datei existiert, sonst False.
        """
        socket_path = Path(self._docker_socket_path)
        try:
            return socket_path.is_socket() or socket_path.exists()
        except OSError:
            return False

    async def restart_container(
        self,
        container_name: str,
        timeout_seconds: int = DEFAULT_RESTART_TIMEOUT_SECONDS,
    ) -> tuple[bool, str]:
        """Startet einen Docker-Container über die Docker Engine API neu.

        Sendet einen POST-Request an /containers/{container_name}/restart.

        Args:
            container_name: Name oder ID des neu zu startenden Containers.
            timeout_seconds: Wartezeit in Sekunden bis zum SIGKILL.

        Returns:
            tuple[bool, str]: (Erfolg, Status- oder Fehlermeldung).
        """
        if not self.is_available():
            message = (
                f"Docker-Socket nicht verfügbar unter '{self._docker_socket_path}'. "
                "Neustart kann nicht ausgeführt werden."
            )
            logger.warning(
                "Docker socket unavailable for restart",
                socket_path=self._docker_socket_path,
                container=container_name,
            )
            return False, message

        endpoint_url = (
            f"http://localhost/containers/{container_name}/restart?t={timeout_seconds}"
        )
        logger.info(
            "Requesting container restart via Docker API",
            container=container_name,
            timeout_seconds=timeout_seconds,
            socket_path=self._docker_socket_path,
        )

        try:
            connector = aiohttp.UnixConnector(path=self._docker_socket_path)
            async with aiohttp.ClientSession(
                connector=connector, timeout=self._request_timeout
            ) as session:
                async with session.post(endpoint_url) as response:
                    if response.status == 204:
                        success_message = (
                            f"Container '{container_name}' erfolgreich neu gestartet."
                        )
                        logger.info(
                            "Container restarted successfully",
                            container=container_name,
                        )
                        return True, success_message

                    if response.status == 404:
                        error_message = (
                            f"Container '{container_name}' wurde nicht gefunden (404)."
                        )
                        logger.error(
                            "Container not found for restart",
                            container=container_name,
                        )
                        return False, error_message

                    response_text = await response.text()
                    error_message = (
                        f"Docker-API Fehler {response.status}: {response_text.strip()}"
                    )
                    logger.error(
                        "Docker API returned error on restart",
                        status=response.status,
                        container=container_name,
                        details=response_text,
                    )
                    return False, error_message

        except TimeoutError:
            error_message = f"Timeout beim Neustart von Container '{container_name}'."
            logger.error("Timeout restarting container", container=container_name)
            return False, error_message
        except aiohttp.ClientError as client_exception:
            error_message = (
                f"Netzwerk-/Socket-Fehler beim Docker-Aufruf: {client_exception}"
            )
            logger.error(
                "ClientError interacting with Docker socket",
                container=container_name,
                error=str(client_exception),
            )
            return False, error_message
        except Exception as unexpected_exception:
            error_message = (
                f"Unerwarteter Fehler beim Docker-Aufruf: {unexpected_exception}"
            )
            logger.error(
                "Unexpected error interacting with Docker socket",
                container=container_name,
                error=str(unexpected_exception),
            )
            return False, error_message

    async def get_container_status(self, container_name: str) -> ContainerStatusReport:
        """Fragt den aktuellen Ausführungs- und Health-Status eines Docker-Containers ab.

        Sendet einen GET-Request an /containers/{container_name}/json über den UNIX Domain Socket.

        Args:
            container_name: Name oder ID des abzufragenden Containers.

        Returns:
            ContainerStatusReport: Strukturierter Bericht über Zustand und Health.
        """
        if not self.is_available():
            message = (
                f"Docker-Socket nicht verfügbar unter '{self._docker_socket_path}'."
            )
            return ContainerStatusReport(
                name_or_id=container_name,
                exists=False,
                status="socket_unavailable",
                error_message=message,
            )

        endpoint_url = f"http://localhost/containers/{container_name}/json"
        try:
            connector = aiohttp.UnixConnector(path=self._docker_socket_path)
            async with aiohttp.ClientSession(
                connector=connector, timeout=self._request_timeout
            ) as session:
                async with session.get(endpoint_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        state = data.get("State", {})
                        status = str(state.get("Status", "unknown"))
                        is_running = bool(state.get("Running", False))
                        exit_code = state.get("ExitCode")
                        health_data = state.get("Health")
                        health_status = (
                            str(health_data.get("Status"))
                            if isinstance(health_data, dict) and "Status" in health_data
                            else None
                        )
                        return ContainerStatusReport(
                            name_or_id=container_name,
                            exists=True,
                            is_running=is_running,
                            status=status,
                            health_status=health_status,
                            exit_code=exit_code,
                        )

                    if response.status == 404:
                        return ContainerStatusReport(
                            name_or_id=container_name,
                            exists=False,
                            status="not_found",
                            error_message=f"Container '{container_name}' wurde nicht gefunden (404).",
                        )

                    response_text = await response.text()
                    return ContainerStatusReport(
                        name_or_id=container_name,
                        exists=False,
                        status=f"http_{response.status}",
                        error_message=f"Docker-API Fehler {response.status}: {response_text.strip()}",
                    )

        except TimeoutError:
            error_message = (
                f"Timeout bei Statusabfrage von Container '{container_name}'."
            )
            logger.error("Timeout inspecting container", container=container_name)
            return ContainerStatusReport(
                name_or_id=container_name,
                exists=False,
                status="timeout",
                error_message=error_message,
            )
        except aiohttp.ClientError as client_exception:
            error_message = (
                f"Netzwerk-/Socket-Fehler beim Docker-Aufruf: {client_exception}"
            )
            logger.error(
                "ClientError inspecting container via Docker socket",
                container=container_name,
                error=str(client_exception),
            )
            return ContainerStatusReport(
                name_or_id=container_name,
                exists=False,
                status="client_error",
                error_message=error_message,
            )
        except Exception as unexpected_exception:
            error_message = (
                f"Unerwarteter Fehler beim Docker-Aufruf: {unexpected_exception}"
            )
            logger.error(
                "Unexpected error inspecting container via Docker socket",
                container=container_name,
                error=str(unexpected_exception),
            )
            return ContainerStatusReport(
                name_or_id=container_name,
                exists=False,
                status="error",
                error_message=error_message,
            )
