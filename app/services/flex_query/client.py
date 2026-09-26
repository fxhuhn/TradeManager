"""
Asynchroner HTTP-Client für den IBKR Flex Query Web Service.

Implementiert das zweistufige Abfrageprotokoll (SendRequest -> GetStatement)
mit automatischem Backoff-Polling bei Status 1019 (Report-Generierung läuft).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
import defusedxml.ElementTree as DefusedET

if TYPE_CHECKING:
    from app.core.config import FlexQueryConfig

logger = logging.getLogger(__name__)


class FlexQueryError(Exception):
    """Basisklasse für alle Flex-Query-Fehler."""


class FlexQueryAuthError(FlexQueryError):
    """Ungültiger oder abgelaufener API-Token (z. B. Error 1018)."""


class FlexQueryRateLimitError(FlexQueryError):
    """Rate-Limit der IBKR Flex Service API erreicht (z. B. Error 1016)."""


class FlexQueryTimeoutError(FlexQueryError):
    """Zeitüberschreitung beim Warten auf die Report-Generierung (Status 1019)."""


class FlexQueryServiceError(FlexQueryError):
    """Allgemeiner Server- oder Schnittstellenfehler."""


class FlexWebServiceClient:
    """Async Client für den interaktiven Abruf von IBKR Flex Statements."""

    def __init__(
        self,
        config: FlexQueryConfig,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._config = config
        self._external_session = session

    async def _get_session(self) -> tuple[aiohttp.ClientSession, bool]:
        """Gibt eine aktive ClientSession zurück.

        Returns:
            Tuple aus (Session, is_internal_flag).
        """
        if self._external_session and not self._external_session.closed:
            return self._external_session, False
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30.0),
            headers={"User-Agent": "TradeManager/1.0"},
        ), True

    async def request_reference_code(
        self,
        token: str | None = None,
        query_id: str | None = None,
    ) -> str:
        """Stellt die initiale SendRequest-Anfrage und extrahiert den ReferenceCode.

        Args:
            token: Optionaler Token (überschreibt Konfiguration).
            query_id: Optionale Query-ID (überschreibt Konfiguration).

        Returns:
            Der von IBKR vergebene ReferenceCode zur Abholung.

        Raises:
            FlexQueryAuthError: Bei Authentifizierungsfehlern.
            FlexQueryRateLimitError: Bei Überschreitung der Anfragegrenzen.
            FlexQueryServiceError: Bei unerwarteter Antwort oder HTTP-Fehler.
        """
        active_token = token or self._config.token
        active_query_id = query_id or self._config.query_id

        if not active_token or not active_query_id:
            raise FlexQueryAuthError(
                "IBKR Flex Token oder Query ID nicht konfiguriert."
            )

        url = f"{self._config.base_url.rstrip('/')}/SendRequest"
        params = {"t": active_token, "q": active_query_id, "v": "3"}

        session, is_internal = await self._get_session()
        try:
            logger.info(
                "Sende SendRequest an IBKR Flex Service (Query-ID: %s)...",
                active_query_id,
            )
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    text = str(await response.text())
                    raise FlexQueryServiceError(
                        f"HTTP {response.status} von IBKR Flex Service: {text[:200]}"
                    )
                content = str(await response.text())
        except aiohttp.ClientError as err:
            raise FlexQueryServiceError(
                f"Netzwerkfehler bei SendRequest: {err}"
            ) from err
        finally:
            if is_internal:
                await session.close()

        return self._extract_reference_code(content)

    def _extract_reference_code(self, xml_content: str) -> str:
        """Parst die SendRequest-XML-Antwort."""
        try:
            root = DefusedET.fromstring(xml_content)
        except Exception as err:
            raise FlexQueryServiceError(
                f"Antwort ist kein valides XML: {xml_content[:200]}"
            ) from err

        status_elem = root.find("Status")
        status_text = (
            status_elem.text if status_elem is not None and status_elem.text else ""
        )

        if status_text == "Success":
            ref_elem = root.find("ReferenceCode")
            if ref_elem is not None and ref_elem.text:
                return str(ref_elem.text).strip()
            raise FlexQueryServiceError(
                "Erfolgreiche Antwort enthielt keinen ReferenceCode."
            )

        # Fehlerbehandlung
        err_code_elem = root.find("ErrorCode")
        err_msg_elem = root.find("ErrorMessage")
        err_code = (
            err_code_elem.text
            if err_code_elem is not None and err_code_elem is not None
            else ""
        )
        err_msg = (
            err_msg_elem.text if err_msg_elem is not None and err_msg_elem.text else ""
        )

        if err_code in ("1018", "1017", "1015"):
            raise FlexQueryAuthError(f"IBKR Flex Auth-Fehler ({err_code}): {err_msg}")
        if err_code == "1016":
            raise FlexQueryRateLimitError(
                f"IBKR Flex Rate-Limit ({err_code}): {err_msg}"
            )

        raise FlexQueryServiceError(
            f"IBKR Flex Service Fehler ({err_code}): {err_msg} (Status: {status_text})"
        )

    async def fetch_statement_xml(
        self,
        reference_code: str,
        token: str | None = None,
    ) -> str:
        """Fragt das finale XML mit dem ReferenceCode ab.

        Args:
            reference_code: Der aus SendRequest erhaltene Code.
            token: Optionaler Token (überschreibt Konfiguration).

        Returns:
            Der rohe XML-String des Flex Statements.

        Raises:
            FlexQueryTimeoutError: Wenn nach max_retries immer noch Code 1019 gemeldet wird.
            FlexQueryError: Bei sonstigen Fehlern.
        """
        active_token = token or self._config.token
        url = f"{self._config.base_url.rstrip('/')}/GetStatement"
        params = {"q": reference_code, "t": active_token, "v": "3"}

        session, is_internal = await self._get_session()
        try:
            for attempt in range(1, self._config.max_retries + 1):
                logger.info(
                    "Frage GetStatement ab (Versuch %d/%d, Ref: %s)...",
                    attempt,
                    self._config.max_retries,
                    reference_code,
                )
                try:
                    async with session.get(url, params=params) as response:
                        if response.status != 200:
                            text = str(await response.text())
                            raise FlexQueryServiceError(
                                f"HTTP {response.status} bei GetStatement: {text[:200]}"
                            )
                        content = str(await response.text())
                except aiohttp.ClientError as err:
                    raise FlexQueryServiceError(
                        f"Netzwerkfehler bei GetStatement: {err}"
                    ) from err

                # Prüfen, ob noch generiert wird (1019) oder ob es das finale XML ist
                is_pending, err_msg = self._check_pending_or_error(content)
                if not is_pending and err_msg is None:
                    # Erfolgreich generiert und geliefert
                    return content

                if is_pending:
                    logger.info(
                        "IBKR generiert Statement noch (Code 1019). Warte %.1fs...",
                        self._config.retry_delay_s,
                    )
                    await asyncio.sleep(self._config.retry_delay_s)
                    continue

                # Ein echter Fehler trat auf
                raise FlexQueryServiceError(f"Fehler bei GetStatement: {err_msg}")

            raise FlexQueryTimeoutError(
                f"Statement wurde nach {self._config.max_retries} Versuchen nicht fertiggestellt."
            )
        finally:
            if is_internal:
                await session.close()

    def _check_pending_or_error(self, content: str) -> tuple[bool, str | None]:
        """Prüft, ob der Inhalt eine Status-Antwort (Warn/Fail/1019) ist.

        Returns:
            (is_in_progress, error_message_or_none)
        """
        # Wenn der Wurzelknoten FlexQueryResponse ist, ist der Report vollständig
        if "<FlexQueryResponse" in content:
            return False, None

        try:
            root = DefusedET.fromstring(content)
        except Exception:
            # Kein wohlgeformtes XML, evtl. Rohdaten oder Fehler
            return False, "Unbekanntes Antwortformat von IBKR"

        err_code_elem = root.find("ErrorCode")
        err_msg_elem = root.find("ErrorMessage")
        err_code = (
            err_code_elem.text
            if err_code_elem is not None and err_code_elem.text
            else ""
        )
        err_msg = (
            err_msg_elem.text if err_msg_elem is not None and err_msg_elem.text else ""
        )

        if err_code == "1019":
            return True, None

        return False, f"Code {err_code}: {err_msg}"

    async def fetch_statement(
        self,
        token: str | None = None,
        query_id: str | None = None,
    ) -> str:
        """Führt den kompletten Ablauf aus: Referenzcode holen und Statement abrufen."""
        ref_code = await self.request_reference_code(token=token, query_id=query_id)
        return await self.fetch_statement_xml(reference_code=ref_code, token=token)
