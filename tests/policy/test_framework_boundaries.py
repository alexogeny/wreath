from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from wreath._auth.models import Identity
from wreath.authorization import human
from wreath.cache_control import CacheControl
from wreath.crud import Access, crud_router
from wreath.exceptions import BadRequest
from wreath.graphql import GraphQL
from wreath.orm import Registry
from wreath.policy import CachePolicy
from wreath.progress import ProgressRegistry, progress_stream, push_progress, status_response
from wreath.request import Request
from wreath.response import Response
from wreath.response_cache import cached
from wreath.state import BODY_CHECK_SLOT
from wreath.streams import Streams, push_stream


def test_agent_resource_ownership_includes_identity_namespace() -> None:
    from wreath._agents.artifacts import AgentArtifactManager
    from wreath._agents.durable import DurableTurn

    manager = AgentArtifactManager(object(), max_bytes=1024, max_artifacts=1)

    def context(namespace: str) -> SimpleNamespace:
        return SimpleNamespace(
            tenant="tenant-a",
            conversation="conversation-a",
            correlation_id=None,
            principal=human(Identity("shared-subject", namespace=namespace)),
        )

    first = context("https://issuer-a.example")
    second = context("https://issuer-b.example")

    assert manager.key(first, "report", ordinal=0) != manager.key(second, "report", ordinal=0)
    assert (
        DurableTurn.from_invocation(first, prompt="summarize", message_id="message-a").turn_id
        != DurableTurn.from_invocation(second, prompt="summarize", message_id="message-a").turn_id
    )


def test_idempotency_ownership_includes_identity_namespace() -> None:
    from wreath.policy.idempotency import IdempotencyPolicy

    class IdempotentRequest:
        method = "POST"
        path = "/charges"
        _context = None
        _scope = {"raw_path": b"/charges"}
        query_string = b""
        state: dict[str, object] = {}

        def __init__(self, namespace: str) -> None:
            self.identity = Identity("shared-subject", namespace=namespace)

        @staticmethod
        def header(name: str) -> str | None:
            return "charge-a" if name == "idempotency-key" else None

    policy = IdempotencyPolicy()

    assert policy._key(
        IdempotentRequest("https://issuer-a.example"), b'{"amount":10}'
    ) != policy._key(IdempotentRequest("https://issuer-b.example"), b'{"amount":10}')


def test_principal_rate_limit_includes_identity_namespace() -> None:
    from wreath.policy.ratelimit import principal_key

    first = SimpleNamespace(
        identity=Identity("shared-subject", namespace="https://issuer-a.example"),
        client=None,
    )
    second = SimpleNamespace(
        identity=Identity("shared-subject", namespace="https://issuer-b.example"),
        client=None,
    )

    assert principal_key(first) != principal_key(second)


def _model():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Record(Model, table="security_contract_records"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)

    return Record


class _Request:
    def __init__(self, method: str, body: bytes = b"") -> None:
        self.method = method
        self.path = "/records"
        self.query_string = b""
        self.headers = [(b"content-type", b"application/json")]
        self._body = body

    async def body(self) -> bytes:
        return self._body


def test_graphql_requires_an_explicit_model_exposure() -> None:
    registry = Registry(object(), [], validate_schema="off")

    with pytest.raises(ValueError, match=r"models=.*explicit"):
        GraphQL(registry)

    assert GraphQL(registry, models=[]).schema.types == {}


def test_graphql_router_requires_an_explicit_access_rule() -> None:
    graphql = GraphQL(Registry(object(), [], validate_schema="off"), models=[])

    with pytest.raises(ValueError, match=r"authorizer.*public=True"):
        graphql.router()

    assert graphql.router(public=True).routes


def test_realtime_readers_require_an_explicit_access_rule() -> None:
    progress = ProgressRegistry()
    streams = object.__new__(Streams)

    with pytest.raises(ValueError, match=r"authorize.*public=True"):
        status_response(progress, "task")
    with pytest.raises(ValueError, match=r"authorize.*public=True"):
        progress_stream(progress, "task")
    with pytest.raises(ValueError, match=r"authorize.*public=True"):
        streams.attach("stream")


@pytest.mark.asyncio
async def test_realtime_websocket_readers_require_an_explicit_access_rule() -> None:
    class Socket:
        async def send_text(self, _value: str) -> None:
            raise AssertionError("a reader without access must not send")

    with pytest.raises(ValueError, match=r"authorize.*public=True"):
        await push_progress(Socket(), ProgressRegistry(), "task")
    with pytest.raises(ValueError, match=r"authorize.*public=True"):
        await push_stream(Socket(), object.__new__(Streams), "stream")


def test_crud_requires_an_explicit_access_rule() -> None:
    with pytest.raises(ValueError, match=r"authorize.*Access\.public"):
        crud_router(_model(), lambda _request: None, operations=("list",))


def test_crud_requires_every_selected_operation_to_resolve() -> None:
    with pytest.raises(ValueError, match=r"create.*correct form"):
        crud_router(
            _model(),
            lambda _request: None,
            operations=("list", "create"),
            authorize={"read": Access.authenticated()},
        )


def test_response_cache_refuses_body_dependent_methods_without_a_body_key() -> None:
    with pytest.raises(ValueError, match=r"POST.*body"):

        @cached(methods=("POST",))
        async def handler(_request):
            return Response(b"never")


@pytest.mark.asyncio
async def test_query_targeted_invalidation_deletes_the_body_qualified_entry() -> None:
    calls: list[bytes] = []

    @cached(ttl=60)
    async def handler(request: _Request) -> Response:
        body = await request.body()
        calls.append(body)
        return Response(body)

    first = _Request("QUERY", b'{"term":"first"}')
    second = _Request("QUERY", b'{"term":"second"}')
    await handler(first)
    await handler(second)
    await handler(first)
    await handler(second)

    invalidated = handler.invalidate(first)
    if invalidated is not None:
        await invalidated

    await handler(first)
    await handler(second)

    assert calls == [first._body, second._body, first._body]


@pytest.mark.asyncio
async def test_non_query_targeted_invalidation_remains_synchronous() -> None:
    calls = 0

    @cached(ttl=60)
    async def handler(_request: _Request) -> Response:
        nonlocal calls
        calls += 1
        return Response(b"cached")

    request = _Request("GET")
    await handler(request)
    await handler(request)

    assert handler.invalidate(request) is None
    await handler(request)

    assert calls == 2


@pytest.mark.asyncio
async def test_signed_stream_refuses_a_mismatched_body_before_yielding_bytes() -> None:
    messages = iter(
        (
            {"type": "http.request", "body": b"for", "more_body": True},
            {"type": "http.request", "body": b"ged", "more_body": False},
        )
    )

    async def receive():
        return next(messages)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/signed",
            "query_string": b"",
            "headers": [],
        },
        receive,
    )
    request.state.__setattr__(
        BODY_CHECK_SLOT,
        ("sha-256", hashlib.sha256(b"honest").digest()),
    )
    effects: list[bytes] = []

    with pytest.raises(BadRequest, match="signed content-digest"):
        async for chunk in request.stream():
            effects.append(chunk)

    assert effects == []


@pytest.mark.asyncio
async def test_public_cache_defaults_are_private_for_an_authenticated_response() -> None:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/me",
            "query_string": b"",
            "headers": [],
        },
        receive,
    )
    request._set_identity(Identity("victim"))
    response = Response(b"victim profile")
    policy = CachePolicy(
        default=CacheControl(public=True, max_age=60),
        cdn_default=CacheControl(public=True, max_age=60),
    )

    await policy.after(request, response)

    assert (b"cache-control", b"private, no-store") in response.headers
    assert (b"cdn-cache-control", b"private, no-store") in response.headers
