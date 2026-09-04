from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest

from wreath._agents.core import BackplaneError, ModelMessage, ModelRequest, ModelUsage
from wreath._agents.enterprise_backplanes import AzureOpenAIBackplane

type TransportBody = bytes | AsyncIterator[bytes]
type TransportResult = tuple[int, Mapping[str, str], TransportBody]


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


@dataclass
class Transport:
    responses: list[TransportResult | Exception]
    requests: list[tuple[str, str, Mapping[str, str], bytes]] = field(default_factory=list)

    async def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> TransportResult:
        self.requests.append((method, url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def request() -> ModelRequest:
    return ModelRequest(
        "caller-model-must-not-escape",
        (ModelMessage("user", "hello"),),
        max_output_tokens=42,
        temperature=0.1,
        metadata={"tenant": "acme"},
    )


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: AzureOpenAIBackplane(
                endpoint="http://resource.openai.azure.com",
                deployment="chat",
                api_key="secret",
                transport=Transport([]),
            ),
            "endpoint.*absolute https",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com/custom",
                deployment="chat",
                api_key="secret",
                transport=Transport([]),
            ),
            "endpoint.*origin",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com?api-version=preview",
                deployment="chat",
                api_key="secret",
                transport=Transport([]),
            ),
            "endpoint.*origin",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com",
                deployment="",
                api_key="secret",
                transport=Transport([]),
            ),
            "deployment",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com",
                deployment="chat",
                transport=Transport([]),
            ),
            "exactly one of api_key or token_provider",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com",
                deployment="chat",
                api_key="",
                transport=Transport([]),
            ),
            "api_key.*non-empty",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com",
                deployment="chat",
                token_provider=cast(Any, False),
                transport=Transport([]),
            ),
            "token_provider.*async callable",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com",
                deployment="chat",
                api_key="secret",
                transport=cast(Any, False),
            ),
            "transport.*callable",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com",
                deployment="chat",
                api_key="secret",
                token_provider=cast(Any, lambda: None),
                transport=Transport([]),
            ),
            "exactly one of api_key or token_provider",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com",
                deployment="chat",
                api_key="secret",
                api_version="legacy",
                transport=Transport([]),
            ),
            "api_version.*v1 or preview",
        ),
        (
            lambda: AzureOpenAIBackplane(
                endpoint="https://resource.openai.azure.com",
                deployment="chat",
                api_key="secret",
                max_response_bytes=0,
                transport=Transport([]),
            ),
            "max_response_bytes",
        ),
    ],
)
def test_azure_configuration_refuses_ambiguous_or_malformed_facts(
    build: Any,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        build()


def test_azure_endpoint_refuses_each_unsafe_origin_component() -> None:
    endpoints: tuple[Any, ...] = (
        7,
        "https://",
        "https://resource.openai.azure.com:0",
        "https://user@resource.openai.azure.com",
        "https://resource.openai.azure.com/custom",
        "https://resource.openai.azure.com#fragment",
        "https://resource.openai.azure.com:not-a-port",
    )
    for endpoint in endpoints:
        with pytest.raises(ValueError, match="endpoint.*absolute https"):
            AzureOpenAIBackplane(
                endpoint=endpoint,
                deployment="chat",
                api_key="secret",
                transport=Transport([]),
            )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://resource.openai.azure.com\r.evil",
        "https://resource.openai.azure.com\t",
        "https://resource.openai.azure.com\x7f",
    ],
)
def test_azure_endpoint_refuses_parser_control_ambiguity(endpoint: str) -> None:
    with pytest.raises(ValueError, match="endpoint.*absolute https"):
        AzureOpenAIBackplane(
            endpoint=endpoint,
            deployment="chat",
            api_key="secret",
            transport=Transport([]),
        )


def test_azure_api_key_cannot_inject_transport_headers() -> None:
    with pytest.raises(ValueError, match="api_key.*header"):
        AzureOpenAIBackplane(
            endpoint="https://resource.openai.azure.com",
            deployment="chat",
            api_key="secret\r\nx-injected: yes",
            transport=Transport([]),
        )


@pytest.mark.parametrize("token", ["token\nextra", "token\x85extra"])
async def test_dynamic_entra_token_cannot_inject_transport_headers(token: str) -> None:
    async def token_provider() -> str:
        return token

    transport = Transport([])
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        token_provider=token_provider,
        transport=transport,
    )

    with pytest.raises(BackplaneError, match="bearer token.*header"):
        _ = [event async for event in plane.stream(request())]

    assert transport.requests == []


def test_azure_deployment_must_be_a_string() -> None:
    with pytest.raises(ValueError, match="deployment.*non-empty string"):
        AzureOpenAIBackplane(
            endpoint="https://resource.openai.azure.com",
            deployment=cast(Any, 7),
            api_key="secret",
            transport=Transport([]),
        )


async def test_explicit_standard_https_port_remains_part_of_the_origin() -> None:
    transport = Transport([(200, {}, b'{"id":"chat","choices":[],"usage":{}}')])
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com:443",
        deployment="chat",
        api_key="secret",
        transport=transport,
        streaming=False,
    )

    _ = [event async for event in plane.stream(request())]

    assert transport.requests[0][1].startswith("https://resource.openai.azure.com:443/")


async def test_api_key_current_v1_endpoint_and_deployment_are_exact() -> None:
    transport = Transport(
        [
            (
                200,
                {"apim-request-id": "apim-header"},
                b'{"id":"chat-body","choices":[{"message":{"content":"hello"}}],'
                b'"usage":{"prompt_tokens":3,"completion_tokens":2}}',
            )
        ]
    )
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com/",
        deployment="finance/chat v2",
        api_key="secret",
        transport=transport,
        streaming=False,
    )

    events = [event async for event in plane.stream(request())]

    assert plane.name == "azure-openai"
    method, url, headers, encoded = transport.requests[0]
    assert (method, url) == (
        "POST",
        "https://resource.openai.azure.com/openai/v1/chat/completions",
    )
    assert headers == {"content-type": "application/json", "api-key": "secret"}
    payload = json.loads(encoded)
    assert payload == {
        "model": "finance/chat v2",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "max_completion_tokens": 42,
        "temperature": 0.1,
    }
    assert [event.kind for event in events] == ["text", "usage", "completed"]
    assert events[0].text == "hello"
    assert events[1].usage == ModelUsage(3, 2)
    assert {event.provider_request_id for event in events} == {"chat-body"}


async def test_explicit_current_api_version_is_query_encoded_once() -> None:
    transport = Transport([(200, {}, b'{"id":"chat","choices":[],"usage":{}}')])
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        api_key="secret",
        api_version="preview",
        transport=transport,
        streaming=False,
    )

    _ = [event async for event in plane.stream(request())]

    assert transport.requests[0][1] == (
        "https://resource.openai.azure.com/openai/v1/chat/completions?api-version=preview"
    )


async def test_omitted_output_limit_does_not_invent_a_completion_limit() -> None:
    transport = Transport([(200, {}, b'{"id":"chat","choices":[],"usage":{}}')])
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        api_key="secret",
        transport=transport,
        streaming=False,
    )

    _ = [event async for event in plane.stream(replace(request(), max_output_tokens=None))]

    payload = json.loads(transport.requests[0][3])
    assert "max_completion_tokens" not in payload
    assert "max_tokens" not in payload


async def test_async_entra_token_is_resolved_for_every_retry_attempt() -> None:
    supplied = iter(("token-one", "token-two"))
    tokens: list[str] = []

    async def token_provider() -> str:
        token = next(supplied)
        tokens.append(token)
        return token

    transport = Transport(
        [
            OSError("connection reset"),
            (200, {}, b'{"id":"chat","choices":[],"usage":{}}'),
        ]
    )
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        token_provider=token_provider,
        transport=transport,
        streaming=False,
        max_retries=1,
    )

    _ = [event async for event in plane.stream(request())]

    assert tokens == ["token-one", "token-two"]
    assert [item[2]["authorization"] for item in transport.requests] == [
        "Bearer token-one",
        "Bearer token-two",
    ]
    assert all("api-key" not in item[2] for item in transport.requests)


async def test_empty_dynamic_entra_token_refuses_before_transport() -> None:
    async def token_provider() -> str:
        return ""

    transport = Transport([])
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        token_provider=token_provider,
        transport=transport,
    )

    with pytest.raises(BackplaneError, match="token_provider.*non-empty bearer token"):
        _ = [event async for event in plane.stream(request())]

    assert transport.requests == []


async def test_non_string_dynamic_entra_token_refuses_before_transport() -> None:
    async def token_provider() -> Any:
        return b"not-a-token"

    transport = Transport([])
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        token_provider=token_provider,
        transport=transport,
    )

    with pytest.raises(BackplaneError, match="token_provider.*non-empty bearer token"):
        _ = [event async for event in plane.stream(request())]

    assert transport.requests == []


async def test_token_provider_must_return_an_awaitable_before_transport() -> None:
    transport = Transport([])
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        token_provider=cast(Any, lambda: "synchronous-token"),
        transport=transport,
    )

    with pytest.raises(BackplaneError, match="token_provider.*awaitable"):
        _ = [event async for event in plane.stream(request())]

    assert transport.requests == []


async def test_streaming_reuses_normalized_decoder_and_preserves_request_id() -> None:
    stream = chunks(
        b'data: {"id":"azure-request","choices":[{"delta":{"content":"hel"}}]}\n\n',
        b'data: {"id":"azure-request","choices":[{"delta":{"content":"lo"}}],',
        b'"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n',
        b"data: [DONE]\n\n",
    )
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        api_key="secret",
        transport=Transport([(200, {"apim-request-id": "header-request"}, stream)]),
    )

    events = [event async for event in plane.stream(request())]

    assert [event.kind for event in events] == ["text", "text", "usage", "completed"]
    assert "".join(event.text or "" for event in events) == "hello"
    assert events[2].usage == ModelUsage(4, 2)
    assert {event.provider_request_id for event in events} == {"azure-request"}


async def test_transport_request_id_and_retryability_survive_http_failure() -> None:
    transport = Transport(
        [
            (
                429,
                {"Apim-Request-Id": "azure-429", "Content-Type": "application/json"},
                b'{"error":{"message":"busy"}}',
            )
        ]
    )
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        api_key="secret",
        transport=transport,
        max_retries=0,
    )

    with pytest.raises(BackplaneError, match="busy") as caught:
        _ = [event async for event in plane.stream(request())]

    assert caught.value.retryable is True
    assert caught.value.status == 429
    assert caught.value.request_id == "azure-429"


async def test_missing_provider_request_id_stays_missing() -> None:
    transport = Transport(
        [(200, {"Content-Type": "application/json"}, b'{"choices":[],"usage":{}}')]
    )
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        api_key="secret",
        transport=transport,
        streaming=False,
    )

    events = [event async for event in plane.stream(request())]

    assert {event.provider_request_id for event in events} == {None}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Request-Id": "generic-request"},
        {"Apim-Request-Id": "apim-request", "Request-Id": "generic-request"},
    ],
)
async def test_generic_request_id_is_never_replaced_by_apim_fallback(
    headers: Mapping[str, str],
) -> None:
    transport = Transport([(200, headers, b'{"choices":[],"usage":{}}')])
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        api_key="secret",
        transport=transport,
        streaming=False,
    )

    events = [event async for event in plane.stream(request())]

    assert {event.provider_request_id for event in events} == {"generic-request"}


async def test_retry_never_replays_after_stream_output() -> None:
    async def partial() -> AsyncIterator[bytes]:
        yield b'data: {"id":"request-1","choices":[{"delta":{"content":"started"}}]}\n\n'
        raise OSError("disconnected")

    transport = Transport(
        [
            (200, {}, partial()),
            (200, {}, chunks(b"data: [DONE]\n\n")),
        ]
    )
    plane = AzureOpenAIBackplane(
        endpoint="https://resource.openai.azure.com",
        deployment="chat",
        api_key="secret",
        transport=transport,
        max_retries=1,
    )
    stream = plane.stream(request())

    first = await anext(stream)
    assert first.text == "started"
    with pytest.raises(BackplaneError) as caught:
        await anext(stream)
    assert caught.value.output_started is True
    assert len(transport.requests) == 1


async def test_stream_and_buffered_responses_share_the_same_body_bound() -> None:
    for streaming, body in (
        (False, b"{" + b"x" * 32 + b"}"),
        (True, chunks(b"data: ", b"x" * 32)),
    ):
        plane = AzureOpenAIBackplane(
            endpoint="https://resource.openai.azure.com",
            deployment="chat",
            api_key="secret",
            transport=Transport([(200, {}, body)]),
            streaming=streaming,
            max_response_bytes=16,
            max_retries=0,
        )

        with pytest.raises(BackplaneError, match="exceeds 16 bytes"):
            _ = [event async for event in plane.stream(request())]
