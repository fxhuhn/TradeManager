"""
Unit-Tests für den asynchronen IBKR Flex Query Web Service Client.
"""

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientResponse

from app.core.config import FlexQueryConfig
from app.services.flex_query.client import (
    FlexQueryAuthError,
    FlexQueryRateLimitError,
    FlexQueryServiceError,
    FlexQueryTimeoutError,
    FlexWebServiceClient,
)


@pytest.fixture
def flex_config() -> FlexQueryConfig:
    return FlexQueryConfig(
        token="DUMMY_TOKEN",
        query_id="123456",
        base_url="https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService",
        max_retries=3,
        retry_delay_s=0.01,
        enabled=True,
    )


def _mock_response(status: int, text: str) -> AsyncMock:
    resp = AsyncMock(spec=ClientResponse)
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    return resp


@pytest.mark.asyncio
async def test_request_reference_code_success(flex_config: FlexQueryConfig) -> None:
    client = FlexWebServiceClient(flex_config)
    success_xml = """<FlexStatementResponse timestamp="25 September, 2026 10:00 AM EDT">
        <Status>Success</Status>
        <ReferenceCode>1234567890</ReferenceCode>
        <Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
    </FlexStatementResponse>"""

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = _mock_response(200, success_xml)
        mock_get.return_value = mock_ctx

        ref_code = await client.request_reference_code()
        assert ref_code == "1234567890"


@pytest.mark.asyncio
async def test_request_reference_code_auth_error(flex_config: FlexQueryConfig) -> None:
    client = FlexWebServiceClient(flex_config)
    error_xml = """<FlexStatementResponse timestamp="25 September, 2026 10:00 AM EDT">
        <Status>Warn</Status>
        <ErrorCode>1018</ErrorCode>
        <ErrorMessage>Token has expired.</ErrorMessage>
    </FlexStatementResponse>"""

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = _mock_response(200, error_xml)
        mock_get.return_value = mock_ctx

        with pytest.raises(FlexQueryAuthError, match="Token has expired"):
            await client.request_reference_code()


@pytest.mark.asyncio
async def test_request_reference_code_rate_limit(flex_config: FlexQueryConfig) -> None:
    client = FlexWebServiceClient(flex_config)
    error_xml = """<FlexStatementResponse timestamp="25 September, 2026 10:00 AM EDT">
        <Status>Warn</Status>
        <ErrorCode>1016</ErrorCode>
        <ErrorMessage>Too many requests have been made.</ErrorMessage>
    </FlexStatementResponse>"""

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = _mock_response(200, error_xml)
        mock_get.return_value = mock_ctx

        with pytest.raises(FlexQueryRateLimitError, match="Too many requests"):
            await client.request_reference_code()


@pytest.mark.asyncio
async def test_request_reference_code_missing_credentials() -> None:
    empty_config = FlexQueryConfig(token="", query_id="")
    client = FlexWebServiceClient(empty_config)

    with pytest.raises(FlexQueryAuthError, match="nicht konfiguriert"):
        await client.request_reference_code()


@pytest.mark.asyncio
async def test_fetch_statement_xml_immediate_success(
    flex_config: FlexQueryConfig,
) -> None:
    client = FlexWebServiceClient(flex_config)
    xml_data = "<FlexQueryResponse queryName='Test'><FlexStatements count='1'/></FlexQueryResponse>"

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = _mock_response(200, xml_data)
        mock_get.return_value = mock_ctx

        statement = await client.fetch_statement_xml("1234567890")
        assert "<FlexQueryResponse" in statement


@pytest.mark.asyncio
async def test_fetch_statement_xml_polling_1019_then_success(
    flex_config: FlexQueryConfig,
) -> None:
    client = FlexWebServiceClient(flex_config)
    pending_xml = """<FlexStatementResponse>
        <Status>Warn</Status>
        <ErrorCode>1019</ErrorCode>
        <ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
    </FlexStatementResponse>"""
    completed_xml = "<FlexQueryResponse><FlexStatements/></FlexQueryResponse>"

    with patch("aiohttp.ClientSession.get") as mock_get:
        resp_pending = _mock_response(200, pending_xml)
        resp_completed = _mock_response(200, completed_xml)

        mock_ctx_pending = AsyncMock()
        mock_ctx_pending.__aenter__.return_value = resp_pending

        mock_ctx_completed = AsyncMock()
        mock_ctx_completed.__aenter__.return_value = resp_completed

        mock_get.side_effect = [mock_ctx_pending, mock_ctx_completed]

        statement = await client.fetch_statement_xml("1234567890")
        assert "<FlexQueryResponse" in statement
        assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_statement_xml_timeout_after_max_retries(
    flex_config: FlexQueryConfig,
) -> None:
    client = FlexWebServiceClient(flex_config)
    pending_xml = """<FlexStatementResponse>
        <Status>Warn</Status>
        <ErrorCode>1019</ErrorCode>
        <ErrorMessage>Statement generation in progress.</ErrorMessage>
    </FlexStatementResponse>"""

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = _mock_response(200, pending_xml)
        mock_get.return_value = mock_ctx

        with pytest.raises(FlexQueryTimeoutError, match="nicht fertiggestellt"):
            await client.fetch_statement_xml("1234567890")

        assert mock_get.call_count == flex_config.max_retries


@pytest.mark.asyncio
async def test_fetch_statement_xml_server_error(flex_config: FlexQueryConfig) -> None:
    client = FlexWebServiceClient(flex_config)

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = _mock_response(500, "Internal Server Error")
        mock_get.return_value = mock_ctx

        with pytest.raises(FlexQueryServiceError, match="HTTP 500"):
            await client.fetch_statement_xml("1234567890")
