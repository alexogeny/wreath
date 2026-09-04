from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest

from wreath._agents.backplanes import (
    AnthropicMessagesBackplane,
    GeminiGenerateContentBackplane,
    OpenAICompatibleBackplane,
    OpenAIResponsesBackplane,
    _anthropic_request,
    _anthropic_response_stream,
    _anthropic_usage,
    _chat_completions_request,
    _chat_usage,
    _gemini_request,
    _gemini_usage,
    _openai_response_stream,
    _openai_responses_request,
    _openai_usage,
)
from wreath.agents import (
    BackplaneError,
    ModelMessage,
    ModelRequest,
    ModelUsage,
    ToolSpecification,
)

type TransportBody = bytes | AsyncIterator[bytes]
type TransportResult = tuple[int, Mapping[str, str], TransportBody]


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


async def anthropic_events(
    *values: Mapping[str, Any], request_id: str | None = "header-id"
) -> list[Any]:
    body = b"".join(
        b"data: " + json.dumps(value).encode() + b"\n\n" for value in values
    )
    return [
        event
        async for event in _anthropic_response_stream(
            body,
            request_id=request_id,
            maximum=16_384,
        )
    ]


class ClosableBody:
    def __init__(self, *values: bytes) -> None:
        self._values = iter(values)
        self.closed = False

    def __aiter__(self) -> ClosableBody:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._values)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


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


def request(*, model: str = "request-model") -> ModelRequest:
    return ModelRequest(
        model,
        (
            ModelMessage("system", "Be precise."),
            ModelMessage("user", "weather in Melbourne"),
            ModelMessage("tool", '{"temperature":18}', name="weather", call_id="call-old"),
        ),
        (
            ToolSpecification(
                "weather",
                "Read current weather",
                {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            ),
        ),
        max_output_tokens=128,
        temperature=0.2,
        metadata={"tenant": "acme"},
    )


def tool_history_request() -> ModelRequest:
    return ModelRequest(
        "request-model",
        (
            ModelMessage("user", "weather"),
            ModelMessage(
                "assistant",
                '{"city":"Melbourne"}',
                name="weather",
                call_id="call-1",
            ),
            ModelMessage("tool", '{"temperature":18}', name="weather", call_id="call-1"),
        ),
    )


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: OpenAIResponsesBackplane(api_key="", transport=Transport([])),
            "OpenAI api_key",
        ),
        (
            lambda: AnthropicMessagesBackplane(api_key="", transport=Transport([])),
            "Anthropic api_key",
        ),
        (
            lambda: GeminiGenerateContentBackplane(api_key="", transport=Transport([])),
            "Gemini api_key",
        ),
        (
            lambda: OpenAICompatibleBackplane(base_url="relative", transport=Transport([])),
            "absolute http or https",
        ),
        (
            lambda: OpenAICompatibleBackplane(
                base_url="https://models.example:not-a-port",
                transport=Transport([]),
            ),
            "absolute http or https",
        ),
        (
            lambda: OpenAIResponsesBackplane(
                api_key="secret", transport=Transport([]), max_retries=-1
            ),
            "max_retries",
        ),
    ],
)
def test_backplane_configuration_refuses_invalid_facts_at_construction(
    factory: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"api_version": ""}, "api_version"),
        ({"default_max_output_tokens": 0}, "default_max_output_tokens"),
    ],
)
def test_anthropic_configuration_refuses_invalid_protocol_defaults(
    options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AnthropicMessagesBackplane(
            api_key="secret", transport=Transport([]), **options
        )


def test_compatible_configuration_refuses_an_empty_configured_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenAICompatibleBackplane(
            base_url="https://models.example/v1",
            transport=Transport([]),
            api_key="",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://models.example\r.evil/v1",
        "https://models.example/v1\t/../../admin",
        "https://models.example/v1\x7fadmin",
    ],
)
def test_backplane_base_url_refuses_parser_control_ambiguity(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url.*absolute http or https"):
        OpenAICompatibleBackplane(base_url=base_url, transport=Transport([]))


def test_backplane_base_url_must_be_text() -> None:
    with pytest.raises(ValueError, match="base_url.*absolute http or https"):
        OpenAICompatibleBackplane(
            base_url=cast(Any, 7), transport=Transport([])
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: OpenAIResponsesBackplane(
            api_key="secret\r\nx-injected: yes", transport=Transport([])
        ),
        lambda: AnthropicMessagesBackplane(
            api_key="secret\x7f", transport=Transport([])
        ),
        lambda: AnthropicMessagesBackplane(
            api_key="secret", api_version="version\nextra", transport=Transport([])
        ),
        lambda: GeminiGenerateContentBackplane(
            api_key="secret\tvalue", transport=Transport([])
        ),
        lambda: OpenAICompatibleBackplane(
            base_url="https://models.example/v1",
            api_key="secret\rvalue",
            transport=Transport([]),
        ),
        lambda: OpenAICompatibleBackplane(
            base_url="https://models.example/v1",
            api_key=cast(Any, 7),
            transport=Transport([]),
        ),
    ],
)
def test_backplane_credentials_cannot_inject_transport_headers(factory: Any) -> None:
    with pytest.raises(ValueError, match="header"):
        factory()


@pytest.mark.parametrize(
    "normalize",
    [_openai_usage, _chat_usage, _anthropic_usage, _gemini_usage],
)
def test_usage_normalizers_treat_non_mappings_as_empty(normalize: Any) -> None:
    assert normalize(None) == ModelUsage(0, 0, 0)


def test_openai_request_requires_complete_tool_history_identity() -> None:
    incomplete = ModelRequest(
        "model",
        (
            ModelMessage("assistant", "name only", name="weather"),
            ModelMessage("assistant", "call only", call_id="call-1"),
            ModelMessage("assistant", "", name="weather", call_id="call-2"),
        ),
    )

    payload = _openai_responses_request(incomplete, stream=False)

    assert payload["input"] == [
        {"role": "assistant", "content": "name only"},
        {"role": "assistant", "content": "call only"},
        {
            "type": "function_call",
            "call_id": "call-2",
            "name": "weather",
            "arguments": "{}",
        },
    ]


def test_openai_request_omits_empty_optional_sections() -> None:
    empty = ModelRequest("model", (ModelMessage("user", "hello"),))
    internal_only = replace(empty, metadata={"tenant": "secret", "agent_profile": "worker"})

    assert _openai_responses_request(empty, stream=False) == {
        "model": "model",
        "input": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
    assert "metadata" not in _openai_responses_request(internal_only, stream=False)


def test_anthropic_request_requires_complete_tool_history_identity() -> None:
    incomplete = ModelRequest(
        "model",
        (
            ModelMessage("assistant", "name only", name="weather"),
            ModelMessage("assistant", "call only", call_id="call-1"),
            ModelMessage("assistant", "", name="weather", call_id="call-2"),
        ),
    )

    payload = _anthropic_request(
        incomplete, stream=False, default_max_output_tokens=99
    )

    assert payload["messages"] == [
        {"role": "assistant", "content": "name only"},
        {"role": "assistant", "content": "call only"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-2",
                    "name": "weather",
                    "input": {},
                }
            ],
        },
    ]
    assert payload["max_tokens"] == 99


def test_anthropic_request_omits_empty_sections_and_preserves_zero_temperature() -> None:
    empty = ModelRequest("model", (ModelMessage("user", "hello"),))
    zero_temperature = replace(empty, temperature=0)
    invalid_users = (
        replace(empty, metadata={"user_id": ""}),
        replace(empty, metadata={"user_id": 7}),
    )

    payload = _anthropic_request(empty, stream=False, default_max_output_tokens=99)
    assert payload == {
        "model": "model",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 99,
        "stream": False,
    }
    assert _anthropic_request(
        zero_temperature, stream=False, default_max_output_tokens=99
    )["temperature"] == 0
    for invalid in invalid_users:
        assert "metadata" not in _anthropic_request(
            invalid, stream=False, default_max_output_tokens=99
        )
    assert _anthropic_request(
        replace(empty, metadata={"user_id": "user-7"}),
        stream=False,
        default_max_output_tokens=99,
    )["metadata"] == {"user_id": "user-7"}


@pytest.mark.asyncio
async def test_openai_stream_preserves_fallback_id_and_ignores_non_text_delta() -> None:
    body = b"".join(
        (
            b'data: {"type":"response.output_text.delta","delta":7,'
            b'"response":{"id":""}}\n\n',
            b'data: {"type":"response.completed"}\n\n',
        )
    )

    events = [
        event
        async for event in _openai_response_stream(
            body, request_id="header-id", maximum=1024
        )
    ]

    assert [event.kind for event in events] == ["usage", "completed"]
    assert all(event.provider_request_id == "header-id" for event in events)
    assert events[0].usage == ModelUsage(0, 0, 0)


@pytest.mark.asyncio
async def test_openai_stream_uses_envelope_for_non_mapping_error() -> None:
    body = b'data: {"type":"error","error":"invalid","message":"specific"}\n\n'

    with pytest.raises(BackplaneError, match="specific"):
        [
            event
            async for event in _openai_response_stream(
                body, request_id="header-id", maximum=1024
            )
        ]


def test_gemini_request_preserves_roles_and_requires_complete_tool_identity() -> None:
    messages = ModelRequest(
        "model",
        (
            ModelMessage("user", "hello"),
            ModelMessage("assistant", "plain"),
            ModelMessage("assistant", "name only", name="weather"),
            ModelMessage("assistant", "call only", call_id="call-1"),
            ModelMessage("assistant", "", name="weather", call_id="call-2"),
            ModelMessage("tool", "7", name="weather", call_id="call-2"),
        ),
    )

    payload = _gemini_request(messages)

    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "plain"}]},
        {"role": "model", "parts": [{"text": "name only"}]},
        {"role": "model", "parts": [{"text": "call only"}]},
        {
            "role": "model",
            "parts": [{"functionCall": {"name": "weather", "args": {}}}],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": "weather",
                        "response": {"result": 7},
                    }
                }
            ],
        },
    ]


def test_gemini_request_omits_empty_options_and_preserves_explicit_values() -> None:
    empty = ModelRequest("model", (ModelMessage("user", "hello"),))
    configured = replace(empty, max_output_tokens=1, temperature=0)

    assert _gemini_request(empty) == {
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}]
    }
    assert _gemini_request(configured)["generationConfig"] == {
        "maxOutputTokens": 1,
        "temperature": 0,
    }


def test_chat_request_preserves_message_roles_and_optional_fields() -> None:
    messages = ModelRequest(
        "model",
        (
            ModelMessage("user", "hello"),
            ModelMessage("user", "named user", name="user-name", call_id="not-a-tool"),
            ModelMessage("assistant", "name only", name="weather"),
            ModelMessage("assistant", "call only", call_id="call-1"),
            ModelMessage("assistant", "", name="weather", call_id="call-2"),
            ModelMessage("tool", "result", name="weather", call_id="call-2"),
        ),
    )

    payload = _chat_completions_request(messages, stream=False)

    assert payload["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "named user", "name": "user-name"},
        {"role": "assistant", "content": "name only", "name": "weather"},
        {"role": "assistant", "content": "call only"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "type": "function",
                    "id": "call-2",
                    "function": {"name": "weather", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": "result",
            "tool_call_id": "call-2",
            "name": "weather",
        },
    ]
    assert "stream_options" not in payload
    assert "max_tokens" not in payload
    assert "temperature" not in payload


def test_chat_request_stream_and_generation_options_preserve_explicit_values() -> None:
    configured = ModelRequest(
        "model",
        (ModelMessage("user", "hello"),),
        max_output_tokens=1,
        temperature=0,
    )

    payload = _chat_completions_request(configured, stream=True)

    assert payload["max_tokens"] == 1
    assert payload["temperature"] == 0
    assert payload["stream_options"] == {"include_usage": True}


async def test_openai_responses_non_streaming_renders_once_and_normalizes_every_fact() -> None:
    transport = Transport(
        [
            (
                200,
                {"x-request-id": "req-header"},
                json.dumps(
                    {
                        "id": "resp_123",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "It is 18 C."}],
                            },
                            {
                                "type": "function_call",
                                "name": "weather",
                                "call_id": "call-new",
                                "arguments": '{"city":"Melbourne"}',
                            },
                        ],
                        "usage": {
                            "input_tokens": 21,
                            "output_tokens": 8,
                            "input_tokens_details": {"cached_tokens": 5},
                        },
                    }
                ).encode(),
            )
        ]
    )
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        model="configured-model",
        transport=transport,
        streaming=False,
    )

    events = [event async for event in plane.stream(request())]

    assert plane.name == "openai"
    assert [event.kind for event in events] == ["text", "tool_call", "usage", "completed"]
    assert events[0].text == "It is 18 C."
    assert events[1].tool_name == "weather"
    assert events[1].tool_call_id == "call-new"
    assert events[1].arguments == {"city": "Melbourne"}
    assert events[2].usage == ModelUsage(21, 8, 5)
    assert {event.provider_request_id for event in events} == {"resp_123"}
    method, url, headers, encoded = transport.requests[0]
    assert (method, url) == ("POST", "https://api.openai.com/v1/responses")
    assert headers["authorization"] == "Bearer secret"
    payload = json.loads(encoded)
    assert payload["model"] == "request-model"
    assert payload["stream"] is False
    assert payload["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call-old",
        "output": '{"temperature":18}',
    }
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "weather",
            "description": "Read current weather",
            "parameters": request().tools[0].input_schema,
        }
    ]


async def test_openai_responses_streaming_handles_chunk_boundaries_and_tool_arguments() -> None:
    stream = chunks(
        b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
        b'data: {"type":"response.output_text.delta","delta":"hel',
        b'lo"}\n\ndata: {"type":"response.function_call_arguments.done",',
        b'"name":"weather","call_id":"call-1","arguments":"{\\"city\\":\\"Melbourne\\"}"}\n\n',
        b'data: {"type":"response.completed","response":{"id":"resp_1","usage":',
        b'{"input_tokens":4,"output_tokens":2,"input_tokens_details":{"cached_tokens":1}}}}\n\n',
    )
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=Transport([(200, {}, stream)]),
    )

    events = [event async for event in plane.stream(request())]

    assert [event.kind for event in events] == ["text", "tool_call", "usage", "completed"]
    assert events[0].text == "hello"
    assert events[1].arguments == {"city": "Melbourne"}
    assert events[2].usage == ModelUsage(4, 2, 1)
    assert all(event.provider_request_id == "resp_1" for event in events)


async def test_openai_truncated_stream_retries_before_visible_output() -> None:
    transport = Transport(
        [
            (200, {}, chunks(b'data: {"type":"response.created"}\n\n')),
            (
                200,
                {},
                chunks(
                    b'data: {"type":"response.completed","response":{"id":"resp-2","usage":{}}}\n\n'
                ),
            ),
        ]
    )
    plane = OpenAIResponsesBackplane(api_key="secret", transport=transport, max_retries=1)

    events = [event async for event in plane.stream(request())]

    assert len(transport.requests) == 2
    assert events[-1].kind == "completed"


async def test_each_provider_round_trips_assistant_tool_calls_in_its_native_shape() -> None:
    openai_transport = Transport([(200, {}, b'{"id":"resp","output":[],"usage":{}}')])
    anthropic_transport = Transport([(200, {}, b'{"id":"msg","content":[],"usage":{}}')])
    gemini_transport = Transport(
        [(200, {}, b'{"responseId":"gem","candidates":[],"usageMetadata":{}}')]
    )
    compatible_transport = Transport([(200, {}, b'{"id":"chat","choices":[],"usage":{}}')])
    planes = (
        OpenAIResponsesBackplane(api_key="secret", transport=openai_transport, streaming=False),
        AnthropicMessagesBackplane(
            api_key="secret", transport=anthropic_transport, streaming=False
        ),
        GeminiGenerateContentBackplane(
            api_key="secret", transport=gemini_transport, streaming=False
        ),
        OpenAICompatibleBackplane(
            base_url="https://models.example/v1",
            transport=compatible_transport,
            streaming=False,
        ),
    )

    for plane in planes:
        [event async for event in plane.stream(tool_history_request())]

    openai = json.loads(openai_transport.requests[0][3])
    assert openai["input"][-2] == {
        "type": "function_call",
        "call_id": "call-1",
        "name": "weather",
        "arguments": '{"city":"Melbourne"}',
    }
    anthropic = json.loads(anthropic_transport.requests[0][3])
    assert anthropic["messages"][-2] == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "weather",
                "input": {"city": "Melbourne"},
            }
        ],
    }
    gemini = json.loads(gemini_transport.requests[0][3])
    assert gemini["contents"][-2] == {
        "role": "model",
        "parts": [{"functionCall": {"name": "weather", "args": {"city": "Melbourne"}}}],
    }
    compatible = json.loads(compatible_transport.requests[0][3])
    assert compatible["messages"][-2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "type": "function",
                "id": "call-1",
                "function": {
                    "name": "weather",
                    "arguments": '{"city":"Melbourne"}',
                },
            }
        ],
    }


async def test_openai_request_never_exports_internal_routing_metadata() -> None:
    transport = Transport([(200, {}, b'{"id":"resp","output":[],"usage":{}}')])
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=transport,
        streaming=False,
    )
    supplied = replace(
        request(),
        metadata={
            "tenant": "tenant-secret",
            "agent_profile": "internal-profile",
            "public_trace": "trace-1",
        },
    )

    [event async for event in plane.stream(supplied)]

    payload = json.loads(transport.requests[0][3])
    assert payload["metadata"] == {"public_trace": "trace-1"}


async def test_anthropic_messages_streaming_maps_system_tools_usage_and_partial_json() -> None:
    stream = chunks(
        b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",',
        b'"usage":{"input_tokens":12,"cache_read_input_tokens":3}}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,',
        b'"delta":{"type":"text_delta","text":"sunny"}}\n\n',
        b'event: content_block_start\ndata: {"type":"content_block_start","index":1,',
        b'"content_block":{"type":"tool_use","id":"toolu_1","name":"weather"}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,',
        b'"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,',
        b'"delta":{"type":"input_json_delta","partial_json":"\\"Melbourne\\"}"}}\n\n',
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n',
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    )
    transport = Transport([(200, {"request-id": "header-id"}, stream)])
    plane = AnthropicMessagesBackplane(api_key="secret", transport=transport)

    events = [event async for event in plane.stream(request())]

    assert plane.name == "anthropic"
    assert [event.kind for event in events] == ["text", "tool_call", "usage", "completed"]
    assert events[1].tool_call_id == "toolu_1"
    assert events[1].arguments == {"city": "Melbourne"}
    assert events[2].usage == ModelUsage(12, 7, 3)
    assert all(event.provider_request_id == "msg_1" for event in events)
    _, url, headers, encoded = transport.requests[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "secret"
    payload = json.loads(encoded)
    assert payload["system"] == "Be precise."
    assert payload["max_tokens"] == 128
    assert payload["stream"] is True
    assert payload["messages"][-1]["content"][0]["type"] == "tool_result"
    assert payload["tools"][0]["input_schema"] == request().tools[0].input_schema
    assert "metadata" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        {"type": "message_start", "message": None},
        {"type": "content_block_start", "index": 1, "content_block": None},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text"},
        },
        {
            "type": "content_block_start",
            "index": [],
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "weather"},
        },
        {"type": "content_block_delta", "index": 0, "delta": None},
    ],
)
async def test_anthropic_stream_ignores_each_malformed_optional_event(
    malformed: Mapping[str, Any],
) -> None:
    events = await anthropic_events(malformed, {"type": "message_stop"})

    assert [event.kind for event in events] == ["completed"]


@pytest.mark.asyncio
async def test_anthropic_stream_does_not_turn_text_blocks_into_tools() -> None:
    events = await anthropic_events(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text"},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "message_stop"},
    )

    assert [event.kind for event in events] == ["completed"]


@pytest.mark.asyncio
async def test_anthropic_stream_preserves_request_fallback_and_missing_usage() -> None:
    events = await anthropic_events(
        {"type": "message_start", "message": {"id": "", "usage": None}},
        {"type": "message_delta", "usage": None},
        {"type": "message_stop"},
    )

    assert [event.kind for event in events] == ["usage", "completed"]
    assert all(event.provider_request_id == "header-id" for event in events)
    assert events[0].usage == ModelUsage(0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delta",
    [
        {"type": "other", "text": "unexpected"},
        {"type": "text_delta", "text": 1},
    ],
)
async def test_anthropic_stream_ignores_each_invalid_text_delta(
    delta: Mapping[str, Any],
) -> None:
    events = await anthropic_events(
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "message_stop"},
    )

    assert [event.kind for event in events] == ["completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index", "delta"),
    [
        (1, {"type": "other", "partial_json": '{"city":"Melbourne"}'}),
        (1.0, {"type": "input_json_delta", "partial_json": '{"city":"Melbourne"}'}),
        (2, {"type": "input_json_delta", "partial_json": '{"city":"Melbourne"}'}),
        (1, {"type": "input_json_delta", "partial_json": 7}),
    ],
)
async def test_anthropic_stream_ignores_each_invalid_tool_delta(
    index: object,
    delta: Mapping[str, Any],
) -> None:
    events = await anthropic_events(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "weather"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "content_block_delta", "index": index, "delta": delta},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_stop"},
    )

    assert [event.kind for event in events] == ["tool_call", "completed"]
    assert events[0].arguments == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [1.0, 2])
async def test_anthropic_stream_ignores_each_invalid_tool_stop(index: object) -> None:
    events = await anthropic_events(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "weather"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "content_block_stop", "index": index},
        {"type": "message_stop"},
    )

    assert [event.kind for event in events] == ["completed"]


@pytest.mark.asyncio
async def test_anthropic_stream_surfaces_explicit_provider_errors() -> None:
    with pytest.raises(BackplaneError, match="capacity exhausted") as caught:
        await anthropic_events(
            {
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": "capacity exhausted",
                },
            }
        )

    assert caught.value.retryable is True


async def test_anthropic_truncated_stream_retries_before_visible_output() -> None:
    transport = Transport(
        [
            (200, {}, chunks(b'data: {"type":"ping"}\n\n')),
            (200, {}, chunks(b'data: {"type":"message_stop"}\n\n')),
        ]
    )
    plane = AnthropicMessagesBackplane(api_key="secret", transport=transport, max_retries=1)

    events = [event async for event in plane.stream(request())]

    assert len(transport.requests) == 2
    assert events[-1].kind == "completed"


@pytest.mark.parametrize(
    ("body", "maximum"),
    [
        (ClosableBody(b"data: not-json\n\n"), 1024),
        (ClosableBody(b"data: {}\n\n"), 4),
    ],
)
async def test_stream_body_closes_when_parsing_exits_before_eof(
    body: ClosableBody, maximum: int
) -> None:
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=Transport([(200, {}, body)]),
        max_response_bytes=maximum,
        max_retries=0,
    )

    with pytest.raises(BackplaneError):
        [event async for event in plane.stream(request())]

    assert body.closed is True


async def test_stream_body_closes_when_consumer_stops_after_one_event() -> None:
    body = ClosableBody(
        b'data: {"type":"response.output_text.delta","delta":"one"}\n\n',
        b'data: {"type":"response.output_text.delta","delta":"two"}\n\n',
    )
    plane = OpenAIResponsesBackplane(api_key="secret", transport=Transport([(200, {}, body)]))
    stream = plane.stream(request())

    assert (await anext(stream)).text == "one"
    await stream.aclose()

    assert body.closed is True


async def test_anthropic_non_streaming_normalizes_content_and_usage() -> None:
    response = {
        "id": "msg_2",
        "content": [
            {"type": "text", "text": "cloudy"},
            {
                "type": "tool_use",
                "id": "toolu_2",
                "name": "weather",
                "input": {"city": "Melbourne"},
            },
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 3,
            "cache_read_input_tokens": 2,
        },
    }
    plane = AnthropicMessagesBackplane(
        api_key="secret",
        transport=Transport([(200, {}, json.dumps(response).encode())]),
        streaming=False,
    )

    events = [event async for event in plane.stream(request())]

    assert [event.kind for event in events] == ["text", "tool_call", "usage", "completed"]
    assert events[2].usage == ModelUsage(10, 3, 2)
    assert all(event.provider_request_id == "msg_2" for event in events)


async def test_gemini_generate_content_non_streaming_maps_wire_roles_tools_and_usage() -> None:
    response = {
        "responseId": "gem_1",
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "18 C"},
                        {
                            "functionCall": {
                                "name": "weather",
                                "args": {"city": "Melbourne"},
                            }
                        },
                    ],
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 15,
            "candidatesTokenCount": 4,
            "cachedContentTokenCount": 6,
        },
    }
    transport = Transport([(200, {}, json.dumps(response).encode())])
    plane = GeminiGenerateContentBackplane(
        api_key="secret",
        transport=transport,
        streaming=False,
    )

    events = [event async for event in plane.stream(request())]

    assert plane.name == "gemini"
    assert [event.kind for event in events] == ["text", "tool_call", "usage", "completed"]
    assert events[1].tool_name == "weather"
    assert events[1].arguments == {"city": "Melbourne"}
    assert events[2].usage == ModelUsage(15, 4, 6)
    assert all(event.provider_request_id == "gem_1" for event in events)
    _, url, headers, encoded = transport.requests[0]
    assert url.endswith("/v1beta/models/request-model:generateContent")
    assert headers["x-goog-api-key"] == "secret"
    payload = json.loads(encoded)
    assert payload["systemInstruction"] == {"parts": [{"text": "Be precise."}]}
    assert payload["contents"][-1] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "weather",
                    "response": {"temperature": 18},
                }
            }
        ],
    }
    assert payload["tools"][0]["functionDeclarations"][0]["name"] == "weather"


async def test_gemini_synthesizes_distinct_call_ids_when_provider_omits_them() -> None:
    response = {
        "responseId": "gem_calls",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "lookup", "args": {"place": "A"}}},
                        {"functionCall": {"name": "lookup", "args": {"place": "B"}}},
                    ]
                }
            }
        ],
        "usageMetadata": {},
    }
    plane = GeminiGenerateContentBackplane(
        api_key="secret",
        transport=Transport([(200, {}, json.dumps(response).encode())]),
        streaming=False,
    )

    events = [event async for event in plane.stream(request())]
    calls = [event for event in events if event.kind == "tool_call"]

    assert len(calls) == 2
    assert [call.tool_call_id for call in calls] == [
        "gem_calls:call:1",
        "gem_calls:call:2",
    ]


async def test_gemini_streaming_emits_each_sse_chunk_and_one_terminal_event() -> None:
    stream = chunks(
        b'data: {"responseId":"gem_2","candidates":[{"content":{"parts":[{"text":"hel"}]}}]}\n\n',
        b'data: {"responseId":"gem_2","candidates":[{"content":{"parts":[{"text":"lo"},',
        b'{"functionCall":{"name":"weather","args":{"city":"Melbourne"}}}]},'
        b'"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":5,'
        b'"candidatesTokenCount":2}}\n\n',
    )
    plane = GeminiGenerateContentBackplane(
        api_key="secret",
        transport=Transport([(200, {}, stream)]),
    )

    events = [event async for event in plane.stream(request())]

    assert [event.kind for event in events] == [
        "text",
        "text",
        "tool_call",
        "usage",
        "completed",
    ]
    assert [event.text for event in events[:2]] == ["hel", "lo"]
    assert all(event.provider_request_id == "gem_2" for event in events)


async def test_gemini_truncated_stream_never_synthesizes_success() -> None:
    stream = chunks(
        b'data: {"responseId":"gem_truncated","candidates":'
        b'[{"content":{"parts":[{"text":"partial"}]}}]}\n\n'
    )
    transport = Transport([(200, {}, stream)])
    plane = GeminiGenerateContentBackplane(
        api_key="secret",
        transport=transport,
        max_retries=1,
    )
    emitted = []

    with pytest.raises(BackplaneError, match="ended before a terminal event") as raised:
        async for event in plane.stream(request()):
            emitted.append(event.kind)

    assert emitted == ["text"]
    assert raised.value.retryable is True
    assert raised.value.output_started is True
    assert raised.value.request_id == "gem_truncated"
    assert len(transport.requests) == 1


async def test_openai_compatible_streaming_accumulates_tool_arguments_and_usage() -> None:
    stream = chunks(
        b'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"id":"chatcmpl_1","choices":[{"delta":{"tool_calls":[{"index":0,',
        b'"id":"call_1","function":{"name":"weather","arguments":"{\\"city\\":"}}]}}]}\n\n',
        b'data: {"id":"chatcmpl_1","choices":[{"delta":{"tool_calls":[{"index":0,',
        b'"function":{"arguments":"\\"Melbourne\\"}"}}]},"finish_reason":"tool_calls"}],',
        b'"usage":{"prompt_tokens":9,"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":2}}}\n\n',
        b"data: [DONE]\n\n",
    )
    transport = Transport([(200, {}, stream)])
    plane = OpenAICompatibleBackplane(
        base_url="https://models.example/v1",
        api_key="secret",
        transport=transport,
    )

    events = [event async for event in plane.stream(request())]

    assert plane.name == "openai-compatible"
    assert [event.kind for event in events] == ["text", "tool_call", "usage", "completed"]
    assert events[1].arguments == {"city": "Melbourne"}
    assert events[2].usage == ModelUsage(9, 3, 2)
    assert all(event.provider_request_id == "chatcmpl_1" for event in events)
    _, url, headers, encoded = transport.requests[0]
    assert url == "https://models.example/v1/chat/completions"
    assert headers["authorization"] == "Bearer secret"
    payload = json.loads(encoded)
    assert payload["model"] == "request-model"
    assert payload["tools"][0]["function"]["parameters"] == request().tools[0].input_schema


async def test_openai_compatible_truncated_stream_never_synthesizes_success() -> None:
    stream = chunks(
        b'data: {"id":"chatcmpl_truncated","choices":[{"delta":{"content":"partial"}}]}\n\n'
    )
    transport = Transport([(200, {}, stream)])
    plane = OpenAICompatibleBackplane(
        base_url="https://models.example/v1",
        transport=transport,
        max_retries=1,
    )
    emitted = []

    with pytest.raises(BackplaneError, match="ended before a terminal event") as raised:
        async for event in plane.stream(request()):
            emitted.append(event.kind)

    assert emitted == ["text"]
    assert raised.value.retryable is True
    assert raised.value.output_started is True
    assert raised.value.request_id == "chatcmpl_truncated"
    assert len(transport.requests) == 1


async def test_openai_compatible_accepts_finish_reason_without_done_sentinel() -> None:
    stream = chunks(
        b'data: {"id":"chatcmpl_terminal","choices":'
        b'[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
    )
    plane = OpenAICompatibleBackplane(
        base_url="https://models.example/v1",
        transport=Transport([(200, {}, stream)]),
    )

    events = [event async for event in plane.stream(request())]

    assert [event.kind for event in events] == ["text", "completed"]


async def test_openai_compatible_non_streaming_normalizes_chat_completion() -> None:
    response = {
        "id": "chatcmpl_2",
        "choices": [
            {
                "message": {
                    "content": "done",
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"Melbourne"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }
    plane = OpenAICompatibleBackplane(
        base_url="http://127.0.0.1:8000/v1",
        transport=Transport([(200, {}, json.dumps(response).encode())]),
        streaming=False,
    )

    events = [event async for event in plane.stream(request())]

    assert [event.kind for event in events] == ["text", "tool_call", "usage", "completed"]
    assert events[2].usage == ModelUsage(4, 2, 0)


async def test_retryable_failure_retries_only_before_visible_output() -> None:
    success = json.dumps({"id": "resp_ok", "output": [], "usage": {}}).encode()
    before_output = Transport(
        [
            (429, {"x-request-id": "req_busy"}, b'{"error":{"message":"busy"}}'),
            (200, {}, success),
        ]
    )
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=before_output,
        streaming=False,
        max_retries=1,
    )

    events = [event async for event in plane.stream(request())]

    assert len(before_output.requests) == 2
    assert events[-1].kind == "completed"

    after_output = Transport(
        [
            (
                200,
                {},
                chunks(
                    b'data: {"type":"response.created","response":{"id":"resp_partial"}}\n\n',
                    b'data: {"type":"response.output_text.delta","delta":"started"}\n\n',
                    b'data: {"type":"error","error":{"type":"server_error","message":"lost"}}\n\n',
                ),
            ),
            (200, {}, success),
        ]
    )
    unsafe = OpenAIResponsesBackplane(
        api_key="secret",
        transport=after_output,
        max_retries=1,
    )

    with pytest.raises(BackplaneError, match="lost") as raised:
        async for _ in unsafe.stream(request()):
            pass
    assert raised.value.retryable is True
    assert raised.value.output_started is True
    assert raised.value.request_id == "resp_partial"
    assert len(after_output.requests) == 1

    after_usage = Transport(
        [
            (
                200,
                {},
                chunks(
                    b'data: {"type":"response.completed","response":{"id":"resp_done",',
                    b'"usage":{"input_tokens":1,"output_tokens":0}}}\n\n',
                    b'data: {"type":"error","error":{"type":"server_error",',
                    b'"message":"late failure"}}\n\n',
                ),
            ),
            (200, {}, success),
        ]
    )
    completed = OpenAIResponsesBackplane(
        api_key="secret",
        transport=after_usage,
        max_retries=1,
    )

    with pytest.raises(BackplaneError, match="late failure") as late:
        async for _ in completed.stream(request()):
            pass
    assert late.value.output_started is True
    assert len(after_usage.requests) == 1


async def test_retry_reuses_the_exact_request_rendered_before_first_attempt() -> None:
    metadata = {"attempt": "original"}

    @dataclass
    class MutatingTransport(Transport):
        async def __call__(
            self,
            method: str,
            url: str,
            headers: Mapping[str, str],
            body: bytes,
        ) -> TransportResult:
            result = await super().__call__(method, url, headers, body)
            metadata["attempt"] = "mutated"
            return result

    transport = MutatingTransport(
        [
            (429, {}, b'{"error":{"message":"busy"}}'),
            (200, {}, b'{"id":"resp","output":[],"usage":{}}'),
        ]
    )
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=transport,
        streaming=False,
        max_retries=1,
    )
    model_request = ModelRequest(
        "request-model",
        (ModelMessage("user", "hello"),),
        metadata=metadata,
    )

    [event async for event in plane.stream(model_request)]

    assert len(transport.requests) == 2
    assert transport.requests[0][3] == transport.requests[1][3]
    assert json.loads(transport.requests[1][3])["metadata"] == {"attempt": "original"}


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (408, True), (429, True), (500, True)],
)
async def test_http_errors_preserve_status_request_id_and_retryability(
    status: int, retryable: bool
) -> None:
    transport = Transport(
        [
            (
                status,
                {"x-request-id": "req_error"},
                b'{"error":{"message":"provider refused"}}',
            )
        ]
    )
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=transport,
        streaming=False,
        max_retries=0,
    )

    with pytest.raises(BackplaneError, match="provider refused") as raised:
        async for _ in plane.stream(request()):
            pass
    assert raised.value.status == status
    assert raised.value.request_id == "req_error"
    assert raised.value.retryable is retryable
    assert raised.value.output_started is False


async def test_redirect_response_is_never_interpreted_as_provider_success() -> None:
    transport = Transport(
        [(302, {}, b'{"id":"redirect-body","output":[],"usage":{}}')]
    )
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=transport,
        streaming=False,
        max_retries=0,
    )

    with pytest.raises(BackplaneError, match="HTTP 302") as raised:
        _ = [event async for event in plane.stream(request())]

    assert raised.value.status == 302
    assert raised.value.retryable is False


@pytest.mark.parametrize("status", [True, 99, 600])
async def test_transport_status_must_be_an_http_status_integer(status: Any) -> None:
    transport = Transport(
        [(status, {}, b'{"id":"invalid-status","output":[],"usage":{}}')]
    )
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=transport,
        streaming=False,
        max_retries=0,
    )

    with pytest.raises(BackplaneError, match="invalid response"):
        _ = [event async for event in plane.stream(request())]


async def test_invalid_response_metadata_closes_body_and_cannot_inject_request_id() -> None:
    body = ClosableBody(b'{"error":{"message":"refused"}}')
    transport = Transport(
        [(400, {"x-request-id": "request\r\ninjected"}, body)]
    )
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=transport,
        streaming=False,
        max_retries=0,
    )

    with pytest.raises(BackplaneError, match="invalid response header") as raised:
        _ = [event async for event in plane.stream(request())]

    assert raised.value.request_id is None
    assert body.closed is True


async def test_each_invalid_response_header_form_is_refused_and_closed() -> None:
    headers: tuple[Mapping[Any, Any], ...] = (
        {7: "value"},
        {"": "value"},
        {"bad name": "value"},
        {"caf\N{LATIN SMALL LETTER E WITH ACUTE}": "value"},
        {"x-value": 7},
        {"x-value": "line\rbreak"},
        {"x-value": "delete\x7f"},
    )
    for raw_headers in headers:
        body = ClosableBody(b"ignored")
        plane = OpenAIResponsesBackplane(
            api_key="secret",
            transport=Transport([(400, cast(Any, raw_headers), body)]),
            streaming=False,
            max_retries=0,
        )

        with pytest.raises(BackplaneError, match="invalid response header"):
            _ = [event async for event in plane.stream(request())]

        assert body.closed is True


async def test_response_header_horizontal_tab_remains_valid_field_whitespace() -> None:
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=Transport(
            [(200, {"x-detail": "one\ttwo"}, b'{"id":"ok","output":[],"usage":{}}')]
        ),
        streaming=False,
        max_retries=0,
    )

    events = [event async for event in plane.stream(request())]

    assert events[-1].kind == "completed"


async def test_invalid_response_status_closes_streaming_body() -> None:
    body = ClosableBody(b"ignored")
    transport = Transport([(True, {}, body)])
    plane = OpenAIResponsesBackplane(
        api_key="secret",
        transport=transport,
        streaming=False,
        max_retries=0,
    )

    with pytest.raises(BackplaneError, match="invalid response"):
        _ = [event async for event in plane.stream(request())]

    assert body.closed is True
