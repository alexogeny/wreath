"""Focused objections for webhook source and destination boundaries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast

import pytest

from wreath.http_client import ClientResponse
from wreath.request import Request
from wreath.webhooks import (
    HMACWebhookSigner,
    InboxClaim,
    LocalReplayStore,
    OutboxDelivery,
    PostgresWebhookInbox,
    PostgresWebhookOutbox,
    WebhookContext,
    WebhookDeliveryResult,
    WebhookDestination,
    WebhookDispatcher,
    WebhookEnvelope,
    WebhookHub,
    WebhookLimits,
    WebhookSource,
    _WebhookDispatcherService,
)

KEYS = {"key": b"webhook mutation control secret"}
NOW = datetime(2026, 8, 25, 4, 5, 6, tzinfo=UTC)


class _RouteApp:
    def __init__(self) -> None:
        self.endpoint = None

    def post(self, _path: str, **_options):
        def register(endpoint):
            self.endpoint = endpoint
            return endpoint

        return register


class _Request:
    def __init__(self, body: bytes, headers: tuple[tuple[bytes, bytes], ...] = ()) -> None:
        self._body = body
        self.headers = headers
        self.method = "POST"
        self.path = "/hooks"

    async def body(self) -> bytes:
        return self._body


def _request(
    body: bytes, headers: tuple[tuple[bytes, bytes], ...] = ()
) -> Request:
    return cast(Request, _Request(body, headers))


class _Verifier:
    max_age = 30.0

    def __init__(self, envelope: WebhookEnvelope) -> None:
        self.envelope = envelope
        self.calls = 0

    def verify(self, **_options) -> WebhookEnvelope:
        self.calls += 1
        return self.envelope


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, tuple[tuple[bytes, bytes], ...]]] = []

    async def post(
        self,
        path: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        body: bytes,
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        self.calls.append((path, body, headers))
        return ClientResponse(202, (), b"", "1.1")


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self):
        return self


class _Inbox:
    def __init__(self, claim: InboxClaim) -> None:
        self.result = claim
        self.completed = False

    async def claim(self, *_args, **_kwargs) -> InboxClaim:
        return self.result

    async def complete(self, *_args, **_kwargs) -> None:
        self.completed = True


class _Outbox:
    def __init__(self) -> None:
        self.envelopes: list[WebhookEnvelope] = []

    async def enqueue(self, _session: object, **values: Any) -> str:
        self.envelopes.append(values["envelope"])
        return "delivery"


class _DispatchOutbox:
    def __init__(self, delivery: OutboxDelivery | None) -> None:
        self.delivery = delivery
        self.actions: list[str] = []

    async def claim_due(self, *_args, **_kwargs) -> OutboxDelivery | None:
        delivery = self.delivery
        self.delivery = None
        return delivery

    async def mark_sending(self, *_args, **_kwargs) -> None:
        self.actions.append("sending")

    async def mark_delivered(self, *_args, **_kwargs) -> None:
        self.actions.append("delivered")

    async def mark_unknown(self, *_args, **_kwargs) -> None:
        self.actions.append("unknown")

    async def mark_retry(self, *_args, **_kwargs) -> None:
        self.actions.append("retry")

    async def mark_failed(self, *_args, **_kwargs) -> None:
        self.actions.append("failed")


class _ResultDestination:
    def __init__(self, result: WebhookDeliveryResult) -> None:
        self.result = result

    async def _send_envelope(self, *_args, **_kwargs) -> WebhookDeliveryResult:
        return self.result


def _envelope(
    *,
    event_id: str = "evt",
    event_type: str = "created",
    correlation_id: str | None = None,
    relay_path: tuple[str, ...] = (),
) -> WebhookEnvelope:
    return WebhookEnvelope(
        event_id,
        event_type,
        "1",
        NOW,
        "application/json",
        b'{"value":1}',
        correlation_id=correlation_id,
        relay_path=relay_path,
    )


def _dispatch_delivery(*, attempts: int = 1) -> OutboxDelivery:
    return OutboxDelivery(
        "delivery",
        "evt",
        "receiver",
        "created",
        NOW,
        "1",
        b"{}",
        "application/json",
        "key",
        attempts,
        2,
    )


def _source(
    *,
    verifier: _Verifier | None = None,
    limits: WebhookLimits | None = None,
    inbox=None,
    session_factory=None,
    lease_owner: str = "worker",
    lease_seconds: float = 5,
    replay: LocalReplayStore | None = None,
) -> WebhookSource:
    selected = verifier or _Verifier(_envelope())
    return WebhookSource(
        _RouteApp(),
        "sender",
        path="/hooks",
        verifier=selected,
        replay=replay,
        limits=limits or WebhookLimits(),
        inbox=inbox,
        session_factory=session_factory,
        lease_owner=lease_owner,
        lease_seconds=lease_seconds,
    )


def test_source_requires_inbox_and_session_factory_together() -> None:
    with pytest.raises(ValueError, match="require inbox and session_factory"):
        _source(inbox=PostgresWebhookInbox())
    with pytest.raises(ValueError, match="require inbox and session_factory"):
        _source(session_factory=lambda: _Session())


def test_durable_source_requires_a_nonempty_lease_owner() -> None:
    with pytest.raises(ValueError, match="lease configuration is invalid"):
        _source(
            inbox=PostgresWebhookInbox(),
            session_factory=lambda: _Session(),
            lease_owner="",
        )


def test_durable_source_requires_a_positive_lease() -> None:
    with pytest.raises(ValueError, match="lease configuration is invalid"):
        _source(
            inbox=PostgresWebhookInbox(),
            session_factory=lambda: _Session(),
            lease_seconds=0,
        )


def test_nondurable_source_does_not_apply_durable_lease_rules() -> None:
    assert _source(lease_owner="", lease_seconds=0)._inbox is None


def test_source_preserves_explicit_replay_store() -> None:
    replay = LocalReplayStore(max_entries=3, ttl=7)

    source = _source(replay=replay)

    assert source._replay is replay
    assert source._replay.max_entries == 3


def test_source_default_replay_store_has_the_documented_bound() -> None:
    source = _source()

    assert source._replay.max_entries == 10_000
    assert source._replay.ttl == 30


@pytest.mark.asyncio
async def test_source_refuses_too_many_headers_before_verification() -> None:
    verifier = _Verifier(_envelope())
    source = _source(verifier=verifier, limits=WebhookLimits(max_headers=1))

    response = await source._receive(
        _request(b"{}", ((b"one", b"1"), (b"two", b"2")))
    )

    assert response.status == 413
    assert verifier.calls == 0


@pytest.mark.asyncio
async def test_source_refuses_excess_header_bytes_before_verification() -> None:
    verifier = _Verifier(_envelope())
    source = _source(verifier=verifier, limits=WebhookLimits(max_header_bytes=3))

    response = await source._receive(_request(b"{}", ((b"name", b"value"),)))

    assert response.status == 413
    assert verifier.calls == 0


@pytest.mark.asyncio
async def test_source_refuses_oversized_verified_event_id() -> None:
    source = _source(
        verifier=_Verifier(_envelope(event_id="long")),
        limits=WebhookLimits(max_event_id_bytes=3),
    )

    called = False

    @source.event("created", payload=dict[str, int])
    async def handler(_context: WebhookContext, _payload: dict[str, int]) -> None:
        nonlocal called
        called = True

    assert (await source._receive(_request(b'{"value":1}'))).status == 400
    assert not called


@pytest.mark.asyncio
async def test_source_refuses_unregistered_verified_event_type() -> None:
    source = _source(verifier=_Verifier(_envelope(event_type="unknown")))

    assert (await source._receive(_request(b"{}"))).status == 400


def _durable_source(
    outcome: Literal["claimed", "duplicate", "active", "failed"],
    status: int | None = None,
) -> tuple[WebhookSource, _Inbox]:
    inbox = _Inbox(InboxClaim(outcome, 2, status))

    @asynccontextmanager
    async def session_factory():
        yield _Session()

    source = _source(inbox=inbox, session_factory=session_factory)

    @source.event("created", payload=dict[str, int])
    async def handler(_context: WebhookContext, _payload: dict[str, int]) -> None:
        inbox.completed = True

    return source, inbox


@pytest.mark.asyncio
async def test_durable_source_replays_duplicate_status() -> None:
    source, inbox = _durable_source("duplicate", 208)

    response = await source._receive(_request(b'{"value":1}'))

    assert response.status == 208
    assert not inbox.completed


@pytest.mark.asyncio
async def test_durable_source_refuses_active_and_failed_claims() -> None:
    active, active_inbox = _durable_source("active")
    failed, failed_inbox = _durable_source("failed")

    assert (await active._receive(_request(b'{"value":1}'))).status == 409
    assert (await failed._receive(_request(b'{"value":1}'))).status == 409
    assert not active_inbox.completed
    assert not failed_inbox.completed


def _destination(
    *,
    path: str = "/hooks",
    relay_id: str = "receiver",
    max_relay_hops: int = 32,
    outbox: PostgresWebhookOutbox | None = None,
) -> tuple[WebhookDestination, _Client]:
    client = _Client()
    return (
        WebhookDestination(
            "receiver",
            client=client,
            path=path,
            signer=HMACWebhookSigner(KEYS, key_id="key"),
            outbox=outbox,
            relay_id=relay_id,
            max_relay_hops=max_relay_hops,
        ),
        client,
    )


def test_destination_requires_a_single_slash_relative_path() -> None:
    with pytest.raises(ValueError, match="origin-relative"):
        _destination(path="relative")
    with pytest.raises(ValueError, match="origin-relative"):
        _destination(path="//other-host/path")


def test_destination_refuses_invalid_relay_id() -> None:
    with pytest.raises(ValueError, match="relay_id is invalid"):
        _destination(relay_id="bad relay")


def test_destination_refuses_each_hop_limit_boundary() -> None:
    with pytest.raises(ValueError, match="between 1 and 32"):
        _destination(max_relay_hops=0)
    with pytest.raises(ValueError, match="between 1 and 32"):
        _destination(max_relay_hops=33)


@pytest.mark.asyncio
async def test_send_supplies_current_timestamp_when_omitted() -> None:
    destination, client = _destination()
    before = datetime.now(UTC)

    await destination.send("created", {}, event_id="evt")

    headers = dict(client.calls[0][2])
    timestamp = datetime.fromisoformat(
        headers[b"wreath-webhook-timestamp"].decode("ascii").replace("Z", "+00:00")
    )
    assert before <= timestamp <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_enqueue_requires_an_outbox() -> None:
    destination, _client = _destination()

    with pytest.raises(RuntimeError, match="no durable outbox"):
        await destination.enqueue(object(), "created", {})


def test_next_relay_path_refuses_a_direct_loop() -> None:
    destination, _client = _destination()

    with pytest.raises(ValueError, match="relay loop detected"):
        destination._next_relay_path(_envelope(relay_path=("receiver",)))


@pytest.mark.asyncio
async def test_enqueue_preserves_bytes_and_explicit_timestamp() -> None:
    outbox = _Outbox()
    destination, _client = _destination(
        outbox=cast(PostgresWebhookOutbox, outbox)
    )

    result = await destination.enqueue(
        object(), "created", bytearray(b"opaque"), event_id="evt", timestamp=NOW
    )

    assert result == "delivery"
    assert outbox.envelopes[0].body == b"opaque"
    assert outbox.envelopes[0].timestamp == NOW


@pytest.mark.asyncio
async def test_enqueue_relay_preserves_existing_correlation() -> None:
    outbox = _Outbox()
    destination, _client = _destination(
        outbox=cast(PostgresWebhookOutbox, outbox)
    )

    await destination.enqueue_relay(
        object(), _envelope(correlation_id="correlation"), "forwarded", {}
    )

    assert outbox.envelopes[0].correlation_id == "correlation"
    assert outbox.envelopes[0].causation_id == "evt"


@pytest.mark.asyncio
async def test_relay_starts_correlation_from_inbound_event_id() -> None:
    destination, client = _destination()

    await destination.relay(_envelope(), "forwarded", {}, timestamp=NOW)

    headers = dict(client.calls[0][2])
    assert headers[b"wreath-correlation-id"] == b"evt"
    assert headers[b"wreath-causation-id"] == b"evt"


def test_dispatcher_refuses_retry_cap_below_base_delay() -> None:
    with pytest.raises(ValueError, match="retry_cap cannot be below retry_delay"):
        WebhookDispatcher(
            PostgresWebhookOutbox(),
            {},
            worker_id="worker",
            retry_delay=2,
            retry_cap=1,
        )


@pytest.mark.asyncio
async def test_dispatcher_refuses_nonpositive_idle_delay_before_opening_session() -> None:
    opened = False

    @asynccontextmanager
    async def session_factory():
        nonlocal opened
        opened = True
        yield _Session()

    with pytest.raises(ValueError, match="idle_delay must be positive"):
        await WebhookDispatcher(
            PostgresWebhookOutbox(), {}, worker_id="worker"
        ).run(session_factory, asyncio.Event(), idle_delay=0)

    assert not opened


def test_hub_schema_owners_include_only_configured_durable_stores() -> None:
    app = _RouteApp()
    hub = WebhookHub(app, "hub")
    assert hub.schema_owners == ()
    inbox = PostgresWebhookInbox()
    outbox = PostgresWebhookOutbox()
    hub.source("local", path="/local", verifier=_Verifier(_envelope()))
    hub.source(
        "source",
        path="/hooks",
        verifier=_Verifier(_envelope()),
        inbox=inbox,
        session_factory=lambda: _Session(),
    )
    hub.destination(
        "destination",
        client=_Client(),
        path="/target",
        signer=HMACWebhookSigner(KEYS, key_id="key"),
        outbox=outbox,
    )

    assert hub.schema_owners == (inbox, outbox)


def test_hub_csrf_exemption_requires_post_and_registered_path() -> None:
    hub = WebhookHub(_RouteApp(), "hub")
    hub.source(
        "source", path="/hooks", verifier=_Verifier(_envelope())
    )
    request = _Request(b"")

    assert hub.csrf_exempt(cast(Request, request))
    request.method = "GET"
    assert not hub.csrf_exempt(cast(Request, request))
    request.method = "POST"
    request.path = "/other"
    assert not hub.csrf_exempt(cast(Request, request))


def test_hub_refuses_duplicate_source_and_destination_names() -> None:
    hub = WebhookHub(_RouteApp(), "hub")
    hub.source("same", path="/one", verifier=_Verifier(_envelope()))
    with pytest.raises(ValueError, match="duplicate webhook source"):
        hub.source("same", path="/two", verifier=_Verifier(_envelope()))

    client = _Client()
    signer = HMACWebhookSigner(KEYS, key_id="key")
    hub.destination("same", client=client, path="/target", signer=signer)
    with pytest.raises(ValueError, match="duplicate webhook destination"):
        hub.destination("same", client=client, path="/target", signer=signer)


def test_hub_preserves_custom_relay_id_and_defaults_to_name() -> None:
    hub = WebhookHub(_RouteApp(), "hub")
    client = _Client()
    signer = HMACWebhookSigner(KEYS, key_id="key")
    defaulted = hub.destination(
        "defaulted", client=client, path="/target", signer=signer
    )
    custom = hub.destination(
        "custom",
        client=client,
        path="/target",
        signer=signer,
        relay_id="service-id",
    )

    assert defaulted._relay_id == "defaulted"
    assert custom._relay_id == "service-id"


def _dispatcher(
    outbox: _DispatchOutbox,
    result: WebhookDeliveryResult,
    *,
    max_attempts: int = 3,
) -> WebhookDispatcher:
    destination = _ResultDestination(result)
    return WebhookDispatcher(
        cast(PostgresWebhookOutbox, outbox),
        {"receiver": cast(WebhookDestination, destination)},
        worker_id="worker",
        max_attempts=max_attempts,
    )


@pytest.mark.asyncio
async def test_dispatcher_refuses_delivered_result_without_status() -> None:
    dispatcher = _dispatcher(
        _DispatchOutbox(_dispatch_delivery()),
        WebhookDeliveryResult("delivered", "evt"),
    )

    with pytest.raises(RuntimeError, match="requires an HTTP status"):
        await dispatcher.run_once(object())


@pytest.mark.asyncio
async def test_dispatcher_attempt_limit_prevents_retry() -> None:
    outbox = _DispatchOutbox(_dispatch_delivery(attempts=3))
    dispatcher = _dispatcher(
        outbox,
        WebhookDeliveryResult("failed", "evt", status=503),
        max_attempts=3,
    )

    await dispatcher.run_once(object())

    assert outbox.actions == ["sending", "failed"]


@pytest.mark.asyncio
async def test_dispatcher_only_retries_configured_statuses() -> None:
    outbox = _DispatchOutbox(_dispatch_delivery(attempts=1))
    dispatcher = _dispatcher(
        outbox, WebhookDeliveryResult("failed", "evt", status=400)
    )

    await dispatcher.run_once(object())

    assert outbox.actions == ["sending", "failed"]


@pytest.mark.asyncio
async def test_dispatcher_cancels_renewal_task_after_send() -> None:
    dispatcher = _dispatcher(
        _DispatchOutbox(_dispatch_delivery()),
        WebhookDeliveryResult("delivered", "evt", status=202),
    )

    @asynccontextmanager
    async def renewal_factory():
        yield object()

    await dispatcher.run_once(object(), renewal_session_factory=renewal_factory)
    await asyncio.sleep(0)

    assert not any(
        task.get_name() == "wreath-webhook-lease-delivery"
        for task in asyncio.all_tasks()
    )


class _TrackingEvent(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.waits = 0

    async def wait(self) -> Literal[True]:
        self.waits += 1
        return await super().wait()


class _LoopOutbox(_DispatchOutbox):
    def __init__(self, stopping: asyncio.Event, *, first_delivery: bool) -> None:
        super().__init__(_dispatch_delivery() if first_delivery else None)
        self.stopping = stopping

    async def claim_due(self, *_args, **_kwargs) -> OutboxDelivery | None:
        delivery = await super().claim_due()
        if delivery is None:
            self.stopping.set()
        return delivery


@pytest.mark.asyncio
async def test_dispatcher_does_not_idle_after_delivery_or_after_stop() -> None:
    stopping = _TrackingEvent()
    outbox = _LoopOutbox(stopping, first_delivery=True)
    dispatcher = _dispatcher(
        outbox, WebhookDeliveryResult("delivered", "evt", status=202)
    )

    @asynccontextmanager
    async def session_factory():
        yield object()

    await dispatcher.run(session_factory, stopping, idle_delay=0.001)

    assert stopping.waits == 0


@pytest.mark.asyncio
async def test_dispatcher_service_propagates_immediate_start_failure() -> None:
    dispatcher = WebhookDispatcher(
        PostgresWebhookOutbox(), {}, worker_id="worker"
    )

    @asynccontextmanager
    async def failing_factory():
        raise RuntimeError("cannot open webhook session")
        yield object()

    class _Supervisor:
        stopping = asyncio.Event()

        def spawn(self, _name: str, coroutine):
            return asyncio.create_task(coroutine)

    service = _WebhookDispatcherService(dispatcher, failing_factory, 0.01)
    with pytest.raises(RuntimeError, match="cannot open webhook session"):
        await service.start(_Supervisor())
