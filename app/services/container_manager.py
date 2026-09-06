"""Docker Container Manager Service für TradeManager.

Ermöglicht den asynchronen Zugriff auf den lokalen Docker-Daemon über den
UNIX Domain Socket (/var/run/docker.sock), um Container wie IBKR Gateway
kontrolliert neu zu starten.
"""

from pathlib import Path
from typing import Final

import aiohttp
import structlog

logger = structlog.get_logger()

DEFAULT_DOCKER_SOCKET_PATH: Final[str] = "/var/run/docker.sock"
DEFAULT_RESTART_TIMEOUT_SECONDS: Final[int] = 10


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
