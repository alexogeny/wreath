from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from .._json import dumps, loads
from .core import (
    BackplaneError,
    ModelRequest,
    ModelResponseEvent,
    ModelUsage,
)

type TransportBody = bytes | AsyncIterator[bytes]
type TransportResult = tuple[int, Mapping[str, str], TransportBody]
type Transport = Callable[[str, str, Mapping[str, str], bytes], Awaitable[TransportResult]]

__all__ = [
    "AnthropicMessagesBackplane",
    "GeminiGenerateContentBackplane",
    "OpenAICompatibleBackplane",
    "OpenAIResponsesBackplane",
]

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429})
_DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_HEADER_TOKEN = frozenset("!#$%&'*+-.^_`|~")


class _Backplane:
    name: ClassVar[str]

    def __init__(
        self,
        *,
        base_url: str,
        transport: Transport,
        model: str | None,
        streaming: bool,
        max_retries: int,
        max_response_bytes: int,
        https_only: bool,
    ) -> None:
        self.base_url = _base_url(base_url, https_only=https_only)
        if not callable(transport):
            raise TypeError(f"{self.name} transport must be callable")
        if model is not None and not model:
            raise ValueError(f"{self.name} model must be non-empty when configured")
        if max_retries < 0:
            raise ValueError(f"{self.name} max_retries must be non-negative")
        if max_response_bytes <= 0:
            raise ValueError(f"{self.name} max_response_bytes must be positive")
        self.transport = transport
        self.model = model
        self.streaming = streaming
        self.max_retries = max_retries
        self.max_response_bytes = max_response_bytes

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponseEvent]:
        body = self._render(request)
        attempts = 0
        output_started = False
        while True:
            attempt = self._stream_once(request, body)
            try:
                async for event in attempt:
                    output_started = True
                    yield event
                return
            except BackplaneError as error:
                if error.retryable and not output_started and attempts < self.max_retries:
                    attempts += 1
                    continue
                if output_started and not error.output_started:
                    raise BackplaneError(
                        str(error),
                        retryable=error.retryable,
                        status=error.status,
                        request_id=error.request_id,
                        output_started=True,
                    ) from error
                raise
            except (OSError, TimeoutError) as error:
                if not output_started and attempts < self.max_retries:
                    attempts += 1
                    continue
                raise BackplaneError(
                    f"{self.name} transport failed: {error}",
                    retryable=True,
                    output_started=output_started,
                ) from error
            finally:
                await _close_body(attempt)

    def _render(self, request: ModelRequest) -> bytes:
        raise NotImplementedError

    def _stream_once(self, request: ModelRequest, body: bytes) -> AsyncIterator[ModelResponseEvent]:
        raise NotImplementedError

    async def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[Mapping[str, str], TransportBody]:
        result = await self.transport(method, url, headers, body)
        if not isinstance(result, tuple) or len(result) != 3:
            raise BackplaneError(f"{self.name} transport must return (status, headers, body)")
        status, raw_headers, response_body = result
        if (
            type(status) is not int
            or not 100 <= status <= 599
            or not isinstance(raw_headers, Mapping)
        ):
            if not isinstance(response_body, bytes):
                await _close_body(response_body)
            raise BackplaneError(f"{self.name} transport returned an invalid response")
        try:
            normalized = _response_headers(raw_headers, provider=self.name)
        except BackplaneError:
            if not isinstance(response_body, bytes):
                await _close_body(response_body)
            raise
        if not 200 <= status < 300:
            encoded = await _read_body(
                response_body,
                maximum=self.max_response_bytes,
                provider=self.name,
            )
            message = _error_message(encoded, f"{self.name} returned HTTP {status}")
            raise BackplaneError(
                message,
                retryable=status in _RETRYABLE_STATUS or status >= 500,
                status=status,
                request_id=_request_id(normalized),
            )
        return normalized, response_body


class OpenAIResponsesBackplane(_Backplane):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        transport: Transport,
        model: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        streaming: bool = True,
        max_retries: int = 2,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI api_key must be non-empty")
        api_key = _header_value(api_key, label="OpenAI api_key")
        super().__init__(
            base_url=base_url,
            transport=transport,
            model=model,
            streaming=streaming,
            max_retries=max_retries,
            max_response_bytes=max_response_bytes,
            https_only=True,
        )
        self._headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }

    def _render(self, request: ModelRequest) -> bytes:
        return dumps(_openai_responses_request(request, stream=self.streaming))

    async def _stream_once(
        self, request: ModelRequest, body: bytes
    ) -> AsyncIterator[ModelResponseEvent]:
        headers, response = await self._send(
            "POST", f"{self.base_url}/responses", self._headers, body
        )
        request_id = _request_id(headers)
        if not self.streaming:
            encoded = await _read_body(
                response, maximum=self.max_response_bytes, provider=self.name
            )
            value = _json_object(encoded, provider=self.name, request_id=request_id)
            async for event in _openai_response(value, request_id=request_id):
                yield event
            return
        try:
            async for event in _openai_response_stream(
                response,
                request_id=request_id,
                maximum=self.max_response_bytes,
            ):
                yield event
        finally:
            if not isinstance(response, bytes):
                await _close_body(response)


class AnthropicMessagesBackplane(_Backplane):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        transport: Transport,
        model: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        api_version: str = "2023-06-01",
        streaming: bool = True,
        default_max_output_tokens: int = 4096,
        max_retries: int = 2,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not api_key:
            raise ValueError("Anthropic api_key must be non-empty")
        if not api_version:
            raise ValueError("Anthropic api_version must be non-empty")
        api_key = _header_value(api_key, label="Anthropic api_key")
        api_version = _header_value(api_version, label="Anthropic api_version")
        if default_max_output_tokens <= 0:
            raise ValueError("Anthropic default_max_output_tokens must be positive")
        super().__init__(
            base_url=base_url,
            transport=transport,
            model=model,
            streaming=streaming,
            max_retries=max_retries,
            max_response_bytes=max_response_bytes,
            https_only=True,
        )
        self.default_max_output_tokens = default_max_output_tokens
        self._headers = {
            "anthropic-version": api_version,
            "content-type": "application/json",
            "x-api-key": api_key,
        }

    def _render(self, request: ModelRequest) -> bytes:
        return dumps(
            _anthropic_request(
                request,
                stream=self.streaming,
                default_max_output_tokens=self.default_max_output_tokens,
            )
        )

    async def _stream_once(
        self, request: ModelRequest, body: bytes
    ) -> AsyncIterator[ModelResponseEvent]:
        headers, response = await self._send(
            "POST", f"{self.base_url}/messages", self._headers, body
        )
        request_id = _request_id(headers)
        if not self.streaming:
            encoded = await _read_body(
                response, maximum=self.max_response_bytes, provider=self.name
            )
            value = _json_object(encoded, provider=self.name, request_id=request_id)
            async for event in _anthropic_response(value, request_id=request_id):
                yield event
            return
        try:
            async for event in _anthropic_response_stream(
                response,
                request_id=request_id,
                maximum=self.max_response_bytes,
            ):
                yield event
        finally:
            if not isinstance(response, bytes):
                await _close_body(response)


class GeminiGenerateContentBackplane(_Backplane):
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        transport: Transport,
        model: str | None = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        streaming: bool = True,
        max_retries: int = 2,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini api_key must be non-empty")
        api_key = _header_value(api_key, label="Gemini api_key")
        super().__init__(
            base_url=base_url,
            transport=transport,
            model=model,
            streaming=streaming,
            max_retries=max_retries,
            max_response_bytes=max_response_bytes,
            https_only=True,
        )
        self._headers = {
            "content-type": "application/json",
            "x-goog-api-key": api_key,
        }

    def _render(self, request: ModelRequest) -> bytes:
        return dumps(_gemini_request(request))

    async def _stream_once(
        self, request: ModelRequest, body: bytes
    ) -> AsyncIterator[ModelResponseEvent]:
        operation = "streamGenerateContent?alt=sse" if self.streaming else "generateContent"
        model = quote(request.model, safe="")
        url = f"{self.base_url}/models/{model}:{operation}"
        headers, response = await self._send("POST", url, self._headers, body)
        request_id = _request_id(headers)
        if not self.streaming:
            encoded = await _read_body(
                response, maximum=self.max_response_bytes, provider=self.name
            )
            value = _json_object(encoded, provider=self.name, request_id=request_id)
            async for event in _gemini_response(value, request_id=request_id):
                yield event
            return
        latest_id = request_id
        call_sequence = [0]
        terminal = False
        try:
            async for value in _json_sse(
                response, maximum=self.max_response_bytes, provider=self.name
            ):
                latest_id = _optional_text(value.get("responseId")) or latest_id
                candidates = value.get("candidates")
                if isinstance(candidates, list) and any(
                    isinstance(candidate, Mapping)
                    and isinstance(candidate.get("finishReason"), str)
                    and bool(candidate["finishReason"])
                    for candidate in candidates
                ):
                    terminal = True
                async for event in _gemini_content(
                    value, request_id=latest_id, call_sequence=call_sequence
                ):
                    yield event
            if not terminal:
                raise BackplaneError(
                    "gemini stream ended before a terminal event",
                    retryable=True,
                    request_id=latest_id,
                )
            yield ModelResponseEvent.completed(request_id=latest_id)
        finally:
            if not isinstance(response, bytes):
                await _close_body(response)


class OpenAICompatibleBackplane(_Backplane):
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        transport: Transport,
        api_key: str | None = None,
        model: str | None = None,
        streaming: bool = True,
        max_retries: int = 2,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if api_key is not None and not api_key:
            raise ValueError("OpenAI-compatible api_key must be non-empty when configured")
        if api_key is not None:
            api_key = _header_value(api_key, label="OpenAI-compatible api_key")
        super().__init__(
            base_url=base_url,
            transport=transport,
            model=model,
            streaming=streaming,
            max_retries=max_retries,
            max_response_bytes=max_response_bytes,
            https_only=False,
        )
        headers = {"content-type": "application/json"}
        if api_key is not None:
            headers["authorization"] = f"Bearer {api_key}"
        self._headers = headers

    def _render(self, request: ModelRequest) -> bytes:
        return dumps(_chat_completions_request(request, stream=self.streaming))

    async def _stream_once(
        self, request: ModelRequest, body: bytes
    ) -> AsyncIterator[ModelResponseEvent]:
        headers, response = await self._send(
            "POST", f"{self.base_url}/chat/completions", self._headers, body
        )
        request_id = _request_id(headers)
        if not self.streaming:
            encoded = await _read_body(
                response, maximum=self.max_response_bytes, provider=self.name
            )
            value = _json_object(encoded, provider=self.name, request_id=request_id)
            async for event in _chat_completion(value, request_id=request_id):
                yield event
            return
        try:
            async for event in _chat_completion_stream(
                response,
                request_id=request_id,
                maximum=self.max_response_bytes,
            ):
                yield event
        finally:
            if not isinstance(response, bytes):
                await _close_body(response)


def _base_url(value: str, *, https_only: bool) -> str:
    schemes = {"https"} if https_only else {"http", "https"}
    expected = "absolute https" if https_only else "absolute http or https"
    message = (
        f"model backplane base_url must be an {expected} URL without credentials, "
        "controls, a query, or a fragment"
    )
    if not isinstance(value, str) or any(
        ord(character) <= 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError(message)
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(message) from error
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or port is not None
        and port <= 0
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(message)
    return value.rstrip("/")


def _header_value(value: str, *, label: str) -> str:
    if not isinstance(value, str) or any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError(f"{label} must be text without HTTP header control characters")
    return value


def _response_headers(
    headers: Mapping[str, str], *, provider: str
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not name
            or any(
                not (
                    character.isascii()
                    and (character.isalnum() or character in _HEADER_TOKEN)
                )
                for character in name
            )
            or not isinstance(value, str)
            or any(
                ord(character) < 0x20 and character != "\t"
                or ord(character) == 0x7F
                for character in value
            )
        ):
            raise BackplaneError(
                f"{provider} transport returned an invalid response header"
            )
        normalized[name.lower()] = value
    return normalized


async def _read_body(body: TransportBody, *, maximum: int, provider: str) -> bytes:
    if isinstance(body, bytes):
        if len(body) > maximum:
            raise BackplaneError(f"{provider} response exceeds {maximum} bytes")
        return body
    parts: list[bytes] = []
    length = 0
    try:
        async for part in body:
            if not isinstance(part, bytes):
                raise BackplaneError(f"{provider} transport yielded a non-bytes body chunk")
            length += len(part)
            if length > maximum:
                raise BackplaneError(f"{provider} response exceeds {maximum} bytes")
            parts.append(part)
    finally:
        await _close_body(body)
    return b"".join(parts)


async def _close_body(body: AsyncIterator[Any]) -> None:
    close = getattr(body, "aclose", None)
    if close is not None:
        await close()


async def _body_chunks(body: TransportBody) -> AsyncIterator[bytes]:
    if isinstance(body, bytes):
        yield body
        return
    async for part in body:
        if not isinstance(part, bytes):
            raise BackplaneError("model transport yielded a non-bytes body chunk")
        yield part


async def _json_sse(
    body: TransportBody, *, maximum: int, provider: str
) -> AsyncIterator[Mapping[str, Any]]:
    buffer = bytearray()
    data: list[bytes] = []
    total = 0
    try:
        async for chunk in _body_chunks(body):
            total += len(chunk)
            if total > maximum:
                raise BackplaneError(f"{provider} response exceeds {maximum} bytes")
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line = bytes(buffer[:newline]).rstrip(b"\r")
                del buffer[: newline + 1]
                if not line:
                    if data:
                        # complexity: allow SL-LINEAR-METHOD -- each data line is joined once
                        encoded = b"\n".join(data)
                        data.clear()
                        if encoded == b"[DONE]":
                            yield {"type": "__done__"}
                        else:
                            yield _json_object(encoded, provider=provider)
                    continue
                if line.startswith(b"data:"):
                    data.append(line[5:].lstrip())
        if buffer:
            line = bytes(buffer).rstrip(b"\r")
            if line.startswith(b"data:"):
                data.append(line[5:].lstrip())
        if data:
            encoded = b"\n".join(data)
            if encoded == b"[DONE]":
                yield {"type": "__done__"}
            else:
                yield _json_object(encoded, provider=provider)
    finally:
        if not isinstance(body, bytes):
            await _close_body(body)


def _json_object(
    encoded: bytes, *, provider: str, request_id: str | None = None
) -> Mapping[str, Any]:
    try:
        value = loads(encoded)
    except (TypeError, ValueError) as error:
        raise BackplaneError(
            f"{provider} returned invalid JSON",
            request_id=request_id,
        ) from error
    if not isinstance(value, Mapping):
        raise BackplaneError(
            f"{provider} returned a non-object JSON response",
            request_id=request_id,
        )
    return value


def _error_message(encoded: bytes, fallback: str) -> str:
    try:
        value = loads(encoded)
    except TypeError, ValueError:
        return fallback
    if not isinstance(value, Mapping):
        return fallback
    error = value.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return fallback


def _request_id(headers: Mapping[str, str]) -> str | None:
    for name in ("x-request-id", "request-id", "x-goog-request-id"):
        value = headers.get(name)
        if value:
            return value
    return None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _arguments(value: Any, *, provider: str, request_id: str | None) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise BackplaneError(f"{provider} returned invalid tool arguments", request_id=request_id)
    parsed = _json_object(value.encode(), provider=provider, request_id=request_id)
    return parsed


def _message_arguments(value: str, *, provider: str) -> Mapping[str, Any]:
    if not value:
        return {}
    return _arguments(value, provider=provider, request_id=None)


def _openai_usage(value: Any) -> ModelUsage:
    usage = value if isinstance(value, Mapping) else {}
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else 0
    return ModelUsage(
        _integer(usage.get("input_tokens")),
        _integer(usage.get("output_tokens")),
        _integer(cached),
    )


def _chat_usage(value: Any) -> ModelUsage:
    usage = value if isinstance(value, Mapping) else {}
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else 0
    return ModelUsage(
        _integer(usage.get("prompt_tokens")),
        _integer(usage.get("completion_tokens")),
        _integer(cached),
    )


def _anthropic_usage(value: Any, *, output_tokens: int | None = None) -> ModelUsage:
    usage = value if isinstance(value, Mapping) else {}
    return ModelUsage(
        _integer(usage.get("input_tokens")),
        _integer(usage.get("output_tokens")) if output_tokens is None else output_tokens,
        _integer(usage.get("cache_read_input_tokens")),
    )


def _gemini_usage(value: Any) -> ModelUsage:
    usage = value if isinstance(value, Mapping) else {}
    return ModelUsage(
        _integer(usage.get("promptTokenCount")),
        _integer(usage.get("candidatesTokenCount")),
        _integer(usage.get("cachedContentTokenCount")),
    )


def _openai_responses_request(request: ModelRequest, *, stream: bool) -> dict[str, Any]:
    inputs: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "assistant" and message.name and message.call_id:
            inputs.append(
                {
                    "type": "function_call",
                    "call_id": message.call_id,
                    "name": message.name,
                    "arguments": message.content or "{}",
                }
            )
        elif message.role == "tool":
            inputs.append(
                {
                    "type": "function_call_output",
                    "call_id": message.call_id,
                    "output": message.content,
                }
            )
        else:
            inputs.append({"role": message.role, "content": message.content})
    value: dict[str, Any] = {
        "model": request.model,
        "input": inputs,
        "stream": stream,
    }
    if request.tools:
        value["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in request.tools
        ]
    _generation_options(value, request, max_name="max_output_tokens")
    if request.metadata:
        metadata = {
            name: item
            for name, item in request.metadata.items()
            if name not in {"agent_profile", "tenant"}
        }
        if metadata:
            value["metadata"] = metadata
    return value


async def _openai_response(
    value: Mapping[str, Any], *, request_id: str | None
) -> AsyncIterator[ModelResponseEvent]:
    request_id = _optional_text(value.get("id")) or request_id
    output = value.get("output")
    if not isinstance(output, list):
        raise BackplaneError("openai response output must be a list", request_id=request_id)
    for item in output:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "message":
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, Mapping) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str):
                            yield ModelResponseEvent.text_delta(text, request_id=request_id)
        elif item.get("type") == "function_call":
            yield ModelResponseEvent.tool_call(
                str(item.get("name", "")),
                str(item.get("call_id", "")),
                _arguments(item.get("arguments"), provider="openai", request_id=request_id),
                request_id=request_id,
            )
    yield ModelResponseEvent.usage_report(_openai_usage(value.get("usage")), request_id=request_id)
    yield ModelResponseEvent.completed(request_id=request_id)


async def _openai_response_stream(
    body: TransportBody, *, request_id: str | None, maximum: int
) -> AsyncIterator[ModelResponseEvent]:
    terminal = False
    async for value in _json_sse(body, maximum=maximum, provider="openai"):
        kind = value.get("type")
        response = value.get("response")
        if isinstance(response, Mapping):
            request_id = _optional_text(response.get("id")) or request_id
        if kind == "response.output_text.delta":
            text = value.get("delta")
            if isinstance(text, str):
                yield ModelResponseEvent.text_delta(text, request_id=request_id)
        elif kind == "response.function_call_arguments.done":
            yield ModelResponseEvent.tool_call(
                str(value.get("name", "")),
                str(value.get("call_id", "")),
                _arguments(value.get("arguments"), provider="openai", request_id=request_id),
                request_id=request_id,
            )
        elif kind == "response.completed":
            terminal = True
            usage = response.get("usage") if isinstance(response, Mapping) else None
            yield ModelResponseEvent.usage_report(_openai_usage(usage), request_id=request_id)
            yield ModelResponseEvent.completed(request_id=request_id)
        elif kind == "error":
            error = value.get("error")
            detail = error if isinstance(error, Mapping) else value
            error_type = str(detail.get("type", ""))
            raise BackplaneError(
                str(detail.get("message", "OpenAI stream failed")),
                retryable=error_type in {"server_error", "rate_limit_error"},
                request_id=request_id,
            )
    if not terminal:
        raise BackplaneError(
            "openai stream ended before response.completed",
            retryable=True,
            request_id=request_id,
        )


def _anthropic_request(
    request: ModelRequest, *, stream: bool, default_max_output_tokens: int
) -> dict[str, Any]:
    systems: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "system":
            systems.append(message.content)
        elif message.role == "assistant" and message.name and message.call_id:
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": message.call_id,
                            "name": message.name,
                            "input": _message_arguments(message.content, provider="anthropic"),
                        }
                    ],
                }
            )
        elif message.role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.call_id,
                            "content": message.content,
                        }
                    ],
                }
            )
        else:
            messages.append({"role": message.role, "content": message.content})
    value: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_output_tokens or default_max_output_tokens,
        "stream": stream,
    }
    if systems:
        value["system"] = "\n\n".join(systems)
    if request.tools:
        value["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in request.tools
        ]
    if request.temperature is not None:
        value["temperature"] = request.temperature
    user_id = request.metadata.get("user_id")
    if isinstance(user_id, str) and user_id:
        value["metadata"] = {"user_id": user_id}
    return value


async def _anthropic_response(
    value: Mapping[str, Any], *, request_id: str | None
) -> AsyncIterator[ModelResponseEvent]:
    request_id = _optional_text(value.get("id")) or request_id
    content = value.get("content")
    if not isinstance(content, list):
        raise BackplaneError("anthropic response content must be a list", request_id=request_id)
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            yield ModelResponseEvent.text_delta(block["text"], request_id=request_id)
        elif block.get("type") == "tool_use":
            yield ModelResponseEvent.tool_call(
                str(block.get("name", "")),
                str(block.get("id", "")),
                _arguments(block.get("input"), provider="anthropic", request_id=request_id),
                request_id=request_id,
            )
    yield ModelResponseEvent.usage_report(
        _anthropic_usage(value.get("usage")), request_id=request_id
    )
    yield ModelResponseEvent.completed(request_id=request_id)


async def _anthropic_response_stream(
    body: TransportBody, *, request_id: str | None, maximum: int
) -> AsyncIterator[ModelResponseEvent]:
    input_usage: Mapping[str, Any] = {}
    tools: dict[int, tuple[str, str, list[str]]] = {}
    terminal = False
    async for value in _json_sse(body, maximum=maximum, provider="anthropic"):
        kind = value.get("type")
        if kind == "message_start":
            message = value.get("message")
            if isinstance(message, Mapping):
                request_id = _optional_text(message.get("id")) or request_id
                usage = message.get("usage")
                if isinstance(usage, Mapping):
                    input_usage = usage
        elif kind == "content_block_start":
            index = value.get("index")
            block = value.get("content_block")
            if isinstance(index, int) and isinstance(block, Mapping):
                if block.get("type") == "tool_use":
                    tools[index] = (
                        str(block.get("name", "")),
                        str(block.get("id", "")),
                        [],
                    )
        elif kind == "content_block_delta":
            index = value.get("index")
            delta = value.get("delta")
            if not isinstance(delta, Mapping):
                continue
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                yield ModelResponseEvent.text_delta(delta["text"], request_id=request_id)
            elif (
                delta.get("type") == "input_json_delta"
                and isinstance(index, int)
                and index in tools
                and isinstance(delta.get("partial_json"), str)
            ):
                tools[index][2].append(delta["partial_json"])
        elif kind == "content_block_stop":
            index = value.get("index")
            if isinstance(index, int) and index in tools:
                name, call_id, parts = tools.pop(index)
                yield ModelResponseEvent.tool_call(
                    name,
                    call_id,
                    _arguments("".join(parts), provider="anthropic", request_id=request_id),
                    request_id=request_id,
                )
        elif kind == "message_delta":
            usage = value.get("usage")
            output = usage.get("output_tokens") if isinstance(usage, Mapping) else None
            yield ModelResponseEvent.usage_report(
                _anthropic_usage(input_usage, output_tokens=_integer(output)),
                request_id=request_id,
            )
        elif kind == "message_stop":
            terminal = True
            yield ModelResponseEvent.completed(request_id=request_id)
        elif kind == "error":
            error = value.get("error")
            detail = error if isinstance(error, Mapping) else value
            error_type = str(detail.get("type", ""))
            raise BackplaneError(
                str(detail.get("message", "Anthropic stream failed")),
                retryable=error_type in {"overloaded_error", "rate_limit_error"},
                request_id=request_id,
            )
    if not terminal:
        raise BackplaneError(
            "anthropic stream ended before message_stop",
            retryable=True,
            request_id=request_id,
        )


def _gemini_request(request: ModelRequest) -> dict[str, Any]:
    systems: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "system":
            systems.append(message.content)
        elif message.role == "assistant" and message.name and message.call_id:
            contents.append(
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": message.name,
                                "args": _message_arguments(message.content, provider="gemini"),
                            }
                        }
                    ],
                }
            )
        elif message.role == "tool":
            try:
                response = loads(message.content.encode())
            except TypeError, ValueError:
                response = {"result": message.content}
            if not isinstance(response, Mapping):
                response = {"result": response}
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": message.name,
                                "response": response,
                            }
                        }
                    ],
                }
            )
        else:
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.content}]})
    value: dict[str, Any] = {"contents": contents}
    if systems:
        value["systemInstruction"] = {"parts": [{"text": "\n\n".join(systems)}]}
    if request.tools:
        value["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    }
                    for tool in request.tools
                ]
            }
        ]
    generation: dict[str, Any] = {}
    if request.max_output_tokens is not None:
        generation["maxOutputTokens"] = request.max_output_tokens
    if request.temperature is not None:
        generation["temperature"] = request.temperature
    if generation:
        value["generationConfig"] = generation
    return value


async def _gemini_content(
    value: Mapping[str, Any], *, request_id: str | None, call_sequence: list[int]
) -> AsyncIterator[ModelResponseEvent]:
    error = value.get("error")
    if isinstance(error, Mapping):
        status = str(error.get("status", ""))
        raise BackplaneError(
            str(error.get("message", "Gemini stream failed")),
            retryable=status in {"RESOURCE_EXHAUSTED", "UNAVAILABLE"},
            request_id=request_id,
        )
    candidates = value.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, Mapping) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, Mapping):
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    yield ModelResponseEvent.text_delta(text, request_id=request_id)
                call = part.get("functionCall")
                if isinstance(call, Mapping):
                    name = str(call.get("name", ""))
                    call_sequence[0] += 1
                    yield ModelResponseEvent.tool_call(
                        name,
                        str(call.get("id") or f"{request_id or 'gemini'}:call:{call_sequence[0]}"),
                        _arguments(call.get("args"), provider="gemini", request_id=request_id),
                        request_id=request_id,
                    )
    usage = value.get("usageMetadata")
    if isinstance(usage, Mapping):
        yield ModelResponseEvent.usage_report(_gemini_usage(usage), request_id=request_id)


async def _gemini_response(
    value: Mapping[str, Any], *, request_id: str | None
) -> AsyncIterator[ModelResponseEvent]:
    request_id = _optional_text(value.get("responseId")) or request_id
    async for event in _gemini_content(value, request_id=request_id, call_sequence=[0]):
        yield event
    yield ModelResponseEvent.completed(request_id=request_id)


def _chat_completions_request(request: ModelRequest, *, stream: bool) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "assistant" and message.name and message.call_id:
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": message.call_id,
                            "function": {
                                "name": message.name,
                                "arguments": message.content or "{}",
                            },
                        }
                    ],
                }
            )
            continue
        value: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.role == "tool":
            value["tool_call_id"] = message.call_id
        if message.name is not None:
            value["name"] = message.name
        messages.append(value)
    result: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "stream": stream,
    }
    if request.tools:
        result["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in request.tools
        ]
    _generation_options(result, request, max_name="max_tokens")
    if stream:
        result["stream_options"] = {"include_usage": True}
    return result


def _generation_options(value: dict[str, Any], request: ModelRequest, *, max_name: str) -> None:
    if request.max_output_tokens is not None:
        value[max_name] = request.max_output_tokens
    if request.temperature is not None:
        value["temperature"] = request.temperature


async def _chat_completion(
    value: Mapping[str, Any], *, request_id: str | None
) -> AsyncIterator[ModelResponseEvent]:
    request_id = _optional_text(value.get("id")) or request_id
    choices = value.get("choices")
    if not isinstance(choices, list):
        raise BackplaneError("openai-compatible choices must be a list", request_id=request_id)
    for choice in choices:
        message = choice.get("message") if isinstance(choice, Mapping) else None
        if not isinstance(message, Mapping):
            continue
        text = message.get("content")
        if isinstance(text, str) and text:
            yield ModelResponseEvent.text_delta(text, request_id=request_id)
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                function = call.get("function") if isinstance(call, Mapping) else None
                if not isinstance(function, Mapping):
                    continue
                yield ModelResponseEvent.tool_call(
                    str(function.get("name", "")),
                    str(call.get("id", "")),
                    _arguments(
                        function.get("arguments"),
                        provider="openai-compatible",
                        request_id=request_id,
                    ),
                    request_id=request_id,
                )
    yield ModelResponseEvent.usage_report(_chat_usage(value.get("usage")), request_id=request_id)
    yield ModelResponseEvent.completed(request_id=request_id)


async def _chat_completion_stream(
    body: TransportBody, *, request_id: str | None, maximum: int
) -> AsyncIterator[ModelResponseEvent]:
    tools: dict[int, tuple[str, str, list[str]]] = {}
    completed = False
    terminal = False
    async for value in _json_sse(body, maximum=maximum, provider="openai-compatible"):
        if value.get("type") == "__done__":
            async for event in _flush_chat_tools(tools, request_id=request_id):
                yield event
            yield ModelResponseEvent.completed(request_id=request_id)
            completed = True
            terminal = True
            continue
        request_id = _optional_text(value.get("id")) or request_id
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, Mapping):
                    continue
                if choice.get("finish_reason") is not None:
                    terminal = True
                delta = choice.get("delta")
                if not isinstance(delta, Mapping):
                    continue
                text = delta.get("content")
                if isinstance(text, str) and text:
                    yield ModelResponseEvent.text_delta(text, request_id=request_id)
                calls = delta.get("tool_calls")
                if isinstance(calls, list):
                    _accumulate_chat_tools(tools, calls)
                if choice.get("finish_reason") is not None:
                    async for event in _flush_chat_tools(tools, request_id=request_id):
                        yield event
        usage = value.get("usage")
        if isinstance(usage, Mapping):
            yield ModelResponseEvent.usage_report(_chat_usage(usage), request_id=request_id)
    if not completed:
        if not terminal:
            raise BackplaneError(
                "openai-compatible stream ended before a terminal event",
                retryable=True,
                request_id=request_id,
            )
        async for event in _flush_chat_tools(tools, request_id=request_id):
            yield event
        yield ModelResponseEvent.completed(request_id=request_id)


def _accumulate_chat_tools(tools: dict[int, tuple[str, str, list[str]]], calls: list[Any]) -> None:
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        index = call.get("index")
        if not isinstance(index, int):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping):
            function = {}
        current = tools.get(index)
        if current is None:
            current = (
                str(function.get("name", "")),
                str(call.get("id", "")),
                [],
            )
            tools[index] = current
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            current[2].append(arguments)


async def _flush_chat_tools(
    tools: dict[int, tuple[str, str, list[str]]], *, request_id: str | None
) -> AsyncIterator[ModelResponseEvent]:
    for index in sorted(tools):
        name, call_id, parts = tools[index]
        yield ModelResponseEvent.tool_call(
            name,
            call_id,
            _arguments(
                "".join(parts),
                provider="openai-compatible",
                request_id=request_id,
            ),
            request_id=request_id,
        )
    tools.clear()
