from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from typing import ClassVar, cast
from urllib.parse import urlsplit

from .._json import dumps
from .backplanes import (
    OpenAICompatibleBackplane,
    Transport,
    TransportResult,
    _chat_completions_request,
)
from .core import BackplaneError, ModelRequest, ModelResponseEvent

type TokenProvider = Callable[[], Awaitable[str]]

_DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Azure OpenAI endpoint must be an absolute https origin")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Azure OpenAI endpoint must be an absolute https origin") from error
    message = (
        "Azure OpenAI endpoint must be an absolute https origin without a path, "
        "query, fragment, or credentials"
    )
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(message)
    if port is not None and port <= 0:
        raise ValueError(message)
    if parsed.username is not None:
        raise ValueError(message)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(message)
    return value.rstrip("/")


class _AzureTransport:
    __slots__ = ("_api_key", "_token_provider", "_transport", "_url")

    def __init__(
        self,
        *,
        url: str,
        transport: Transport,
        api_key: str | None,
        token_provider: TokenProvider | None,
    ) -> None:
        self._url = url
        self._transport = transport
        self._api_key = api_key
        self._token_provider = token_provider

    async def __call__(
        self,
        method: str,
        _url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> TransportResult:
        authenticated = dict(headers)
        if self._api_key is not None:
            authenticated["api-key"] = self._api_key
        else:
            provider = cast(TokenProvider, self._token_provider)
            pending = provider()
            if not isinstance(pending, Awaitable):
                raise BackplaneError("Azure OpenAI token_provider must return an awaitable")
            token = await pending
            if not isinstance(token, str) or not token:
                raise BackplaneError(
                    "Azure OpenAI token_provider must return a non-empty bearer token"
                )
            authenticated["authorization"] = f"Bearer {token}"
        status, response_headers, response_body = await self._transport(
            method,
            self._url,
            authenticated,
            body,
        )
        apim_request_id: str | None = None
        has_generic_request_id = False
        for raw_name, raw_value in response_headers.items():
            name = str(raw_name).lower()
            if name == "apim-request-id":
                apim_request_id = str(raw_value)
            elif name in {"x-request-id", "request-id"}:
                has_generic_request_id = True
        if apim_request_id is not None and not has_generic_request_id:
            normalized_headers = dict(response_headers)
            normalized_headers["x-request-id"] = apim_request_id
            response_headers = normalized_headers
        return status, response_headers, response_body


class _AzureCompatibleBackplane(OpenAICompatibleBackplane):
    name = "azure-openai"

    def _render(self, request: ModelRequest) -> bytes:
        value = _chat_completions_request(request, stream=self.streaming)
        maximum = value.pop("max_tokens", None)
        if maximum is not None:
            value["max_completion_tokens"] = maximum
        return dumps(value)


class AzureOpenAIBackplane:
    name: ClassVar[str] = "azure-openai"

    __slots__ = ("_delegate", "deployment", "endpoint")

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        transport: Transport,
        api_key: str | None = None,
        token_provider: TokenProvider | None = None,
        api_version: str | None = None,
        streaming: bool = True,
        max_retries: int = 2,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        origin = _endpoint(endpoint)
        if not isinstance(deployment, str) or not deployment:
            raise ValueError("Azure OpenAI deployment must be a non-empty string")
        if (api_key is None) == (token_provider is None):
            raise ValueError("Azure OpenAI requires exactly one of api_key or token_provider")
        if api_key is not None and not api_key:
            raise ValueError("Azure OpenAI api_key must be non-empty")
        if token_provider is not None and not callable(token_provider):
            raise TypeError("Azure OpenAI token_provider must be an async callable")
        if not callable(transport):
            raise TypeError("Azure OpenAI transport must be callable")
        if api_version not in {None, "v1", "preview"}:
            raise ValueError("Azure OpenAI api_version must be v1 or preview")
        url = f"{origin}/openai/v1/chat/completions"
        if api_version is not None:
            url = f"{url}?api-version={api_version}"
        authenticated_transport = _AzureTransport(
            url=url,
            transport=transport,
            api_key=api_key,
            token_provider=token_provider,
        )
        self._delegate = _AzureCompatibleBackplane(
            base_url=origin,
            transport=authenticated_transport,
            streaming=streaming,
            max_retries=max_retries,
            max_response_bytes=max_response_bytes,
        )
        self.endpoint = origin
        self.deployment = deployment

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponseEvent]:
        return self._delegate.stream(replace(request, model=self.deployment))


__all__ = ["AzureOpenAIBackplane"]
