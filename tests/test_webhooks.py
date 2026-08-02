from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from wreath import Wreath
from wreath.http_client import ClientResponse, ConnectError
from wreath.middleware import CSRFMiddleware
from wreath.testing import TestClient
from wreath.webhooks import (
    HMACWebhookSigner,
    HMACWebhookVerifier,
    LocalReplayStore,
    PostgresWebhookInbox,
    PostgresWebhookOutbox,
    WebhookContext,
    WebhookDispatcher,
    WebhookEnvelope,
    WebhookLimits,
    _retention_purge_pass,
)

KEYS = {"current": b"a sufficiently long webhook test secret"}


def test_retention_purge_requires_a_primary_key() -> None:
    with pytest.raises(ValueError, match="at least one primary-key column"):
        _retention_purge_pass(table="events", key=())
    assert _retention_purge_pass(table="events", key=("event_id",)).name == "purge_events"


def _envelope(body: bytes = b'{"value":1}') -> WebhookEnvelope:
    return WebhookEnvelope(
        id="evt-1",
        type="widget.changed",
        version="1",
        timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        content_type="application/json",
        body=body,
    )


def test_hmac_signer_and_verifier_cover_exact_body() -> None:
    signer = HMACWebhookSigner(KEYS, key_id="current")
    verifier = HMACWebhookVerifier(KEYS, max_age=300)
    envelope = _envelope()
    headers = dict(signer.headers(envelope))

    verified = verifier.verify(
        body=envelope.body,
        headers=headers,
        now=envelope.timestamp + timedelta(seconds=30),
    )

    assert verified.id == envelope.id
    assert verified.type == envelope.type
    assert verified.body == envelope.body
    with pytest.raises(ValueError, match="signature"):
        verifier.verify(
            body=b'{"value":2}',
            headers=headers,
            now=envelope.timestamp + timedelta(seconds=30),
        )


def test_invalid_signature_uses_constant_time_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _envelope()
    headers = dict(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))
    compared: list[tuple[bytes, bytes]] = []
    original = __import__("hmac").compare_digest

    def compare(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr("wreath.webhooks.hmac.compare_digest", compare)
    with pytest.raises(ValueError, match="signature"):
        HMACWebhookVerifier(KEYS).verify(
            body=b"tampered",
            headers=headers,
            now=envelope.timestamp,
        )
    assert len(compared) == 1
    assert compared[0][0].startswith(b"v1=")


def test_verifier_rejects_stale_timestamp_and_unknown_key() -> None:
    envelope = _envelope()
    headers = dict(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))
    verifier = HMACWebhookVerifier(KEYS, max_age=10)

    with pytest.raises(ValueError, match="timestamp"):
        verifier.verify(
            body=envelope.body,
            headers=headers,
            now=envelope.timestamp + timedelta(seconds=11),
        )
    headers[b"wreath-webhook-key-id"] = b"missing"
    with pytest.raises(ValueError, match="key"):
        verifier.verify(body=envelope.body, headers=headers, now=envelope.timestamp)


def test_webhook_key_rotation_accepts_current_and_previous_keys() -> None:
    keys = {"current": b"current-secret-material", "previous": b"previous-secret-material"}
    envelope = _envelope()
    verifier = HMACWebhookVerifier(keys)
    for key_id in keys:
        headers = dict(HMACWebhookSigner(keys, key_id=key_id).headers(envelope))
        assert verifier.verify(
            body=envelope.body,
            headers=headers,
            now=envelope.timestamp,
        ).id == envelope.id

    previous_headers = dict(
        HMACWebhookSigner(keys, key_id="previous").headers(envelope)
    )
    with pytest.raises(ValueError, match="key"):
        HMACWebhookVerifier({"current": keys["current"]}).verify(
            body=envelope.body,
            headers=previous_headers,
            now=envelope.timestamp,
        )


@pytest.mark.parametrize(
    "missing",
    [
        b"wreath-webhook-id",
        b"wreath-webhook-type",
        b"wreath-webhook-version",
        b"wreath-webhook-timestamp",
        b"wreath-webhook-key-id",
        b"wreath-webhook-signature",
    ],
)
def test_verifier_rejects_each_missing_required_header(missing: bytes) -> None:
    envelope = _envelope()
    headers = dict(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))
    del headers[missing]
    with pytest.raises(ValueError, match="missing webhook header"):
        HMACWebhookVerifier(KEYS).verify(
            body=envelope.body,
            headers=headers,
            now=envelope.timestamp,
        )


@pytest.mark.asyncio
async def test_local_replay_store_is_bounded_and_expires() -> None:
    store = LocalReplayStore(max_entries=2, ttl=10)
    assert await store.claim("source", "one", now=0)
    assert not await store.claim("source", "one", now=1)
    assert await store.claim("source", "two", now=1)
    assert await store.claim("source", "three", now=2)
    assert store.size == 2
    assert await store.claim("source", "one", now=20)


@pytest.mark.asyncio
async def test_local_replay_randomized_transition_and_expiry_model() -> None:
    rng = random.Random(0x4E454F)
    store = LocalReplayStore(max_entries=7, ttl=5)
    model: dict[tuple[str, str], tuple[float, int]] = {}
    sequence = 0
    now = 0.0
    for _ in range(2_000):
        now += rng.random() * 1.5
        key = (f"source-{rng.randrange(3)}", f"event-{rng.randrange(20)}")
        model = {key_: value for key_, value in model.items() if value[0] > now}
        expected = key not in model
        if expected:
            while len(model) >= 7:
                oldest = min(model, key=lambda item: model[item])
                del model[oldest]
            sequence += 1
            model[key] = (now + 5, sequence)
        assert await store.claim(*key, now=now) is expected
        if rng.randrange(4) == 0:
            await store.complete(*key, "completed")
        assert store.size == len(model)
        assert store.size <= store.max_entries


@dataclass
class WidgetChanged:
    value: int


@pytest.mark.asyncio
async def test_inbound_source_verifies_decodes_dispatches_and_rejects_replay() -> None:
    app = Wreath()
    hooks = app.webhooks("partners")
    source = hooks.source(
        "sender",
        path="/hooks/sender",
        verifier=HMACWebhookVerifier(KEYS, max_age=300),
        replay=LocalReplayStore(max_entries=10, ttl=300),
    )
    seen: list[tuple[str, int]] = []

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        seen.append((context.envelope.id, event.value))

    envelope = WebhookEnvelope(
        id="evt-live",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":7}',
    )
    headers = {
        name.decode("ascii"): value.decode("ascii")
        for name, value in HMACWebhookSigner(KEYS, key_id="current").headers(envelope)
    }
    headers["content-type"] = "application/json"

    async with TestClient(app) as client:
        response = await client.post("/hooks/sender", headers=headers, content=envelope.body)
        duplicate = await client.post("/hooks/sender", headers=headers, content=envelope.body)

    assert response.status == 204
    assert duplicate.status == 409
    assert seen == [("evt-live", 7)]


@pytest.mark.asyncio
async def test_inbound_source_uses_normalized_headers_and_compiled_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath.binding import _body_validator as compile_validator

    app = Wreath()
    normalized_calls = 0
    compile_calls = 0
    validation_calls = 0

    class TrackingVerifier(HMACWebhookVerifier):
        def verify(self, **options: object) -> WebhookEnvelope:
            raise AssertionError("source must not renormalize headers in public verify")

        def _verify_normalized(self, **options: object) -> WebhookEnvelope:
            nonlocal normalized_calls
            normalized_calls += 1
            return super()._verify_normalized(**options)

    def tracked_compile(annotation: object):
        nonlocal compile_calls
        compile_calls += 1
        validator = compile_validator(annotation)

        def tracked_validate(value: object, loc: tuple[object, ...]) -> object:
            nonlocal validation_calls
            validation_calls += 1
            return validator(value, loc)

        return tracked_validate

    monkeypatch.setattr("wreath.webhooks._body_validator", tracked_compile)
    source = app.webhooks("optimized").source(
        "sender",
        path="/hooks/optimized",
        verifier=TrackingVerifier(KEYS),
    )

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        assert event.value == 1

    signer = HMACWebhookSigner(KEYS, key_id="current")
    async with TestClient(app) as client:
        for index in range(2):
            envelope = WebhookEnvelope(
                id=f"evt-optimized-{index}",
                type="widget.changed",
                version="1",
                timestamp=datetime.now(UTC),
                content_type="application/json",
                body=b'{"value":1}',
            )
            headers = {
                name.decode(): value.decode()
                for name, value in signer.headers(envelope)
            }
            assert (
                await client.post(
                    "/hooks/optimized", headers=headers, content=envelope.body
                )
            ).status == 204

    assert compile_calls == 1
    assert validation_calls == 2
    assert normalized_calls == 2


@pytest.mark.asyncio
async def test_inbound_source_rejects_invalid_signature_before_handler() -> None:
    app = Wreath()
    hooks = app.webhooks("partners")
    source = hooks.source(
        "sender",
        path="/hooks/sender",
        verifier=HMACWebhookVerifier(KEYS, max_age=300),
    )
    called = False

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        nonlocal called
        called = True

    envelope = WebhookEnvelope(
        id="evt-bad",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":7}',
    )
    headers = {
        name.decode("ascii"): value.decode("ascii")
        for name, value in HMACWebhookSigner(KEYS, key_id="current").headers(envelope)
    }
    headers["wreath-webhook-signature"] = "v1=bad"

    async with TestClient(app) as client:
        response = await client.post("/hooks/sender", headers=headers, content=envelope.body)

    assert response.status == 401
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json at all", b"", b'{"value":', b"\xff\xfe"])
async def test_a_correctly_signed_body_that_is_not_json_is_a_400(body: bytes) -> None:
    """The signature proves the sender; a malformed payload is still theirs."""
    app = Wreath()
    source = app.webhooks("partners").source(
        "sender",
        path="/hooks/malformed",
        verifier=HMACWebhookVerifier(KEYS, max_age=300),
    )
    called = False

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        nonlocal called
        called = True

    envelope = WebhookEnvelope(
        id="evt-malformed",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=body,
    )
    headers = {
        name.decode("ascii"): value.decode("ascii")
        for name, value in HMACWebhookSigner(KEYS, key_id="current").headers(envelope)
    }

    async with TestClient(app) as client:
        response = await client.post("/hooks/malformed", headers=headers, content=body)

    assert response.status == 400
    assert not called


@pytest.mark.asyncio
async def test_inbound_source_applies_webhook_body_limit_before_dispatch() -> None:
    app = Wreath()
    hooks = app.webhooks("bounded")
    source = hooks.source(
        "sender",
        path="/hooks/bounded",
        verifier=HMACWebhookVerifier(KEYS),
        limits=WebhookLimits(max_body_bytes=3),
    )
    called = False

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        nonlocal called
        called = True

    envelope = WebhookEnvelope(
        id="evt-large",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"1234",
    )
    headers = {
        name.decode("ascii"): value.decode("ascii")
        for name, value in HMACWebhookSigner(KEYS, key_id="current").headers(envelope)
    }
    async with TestClient(app) as client:
        response = await client.post(
            "/hooks/bounded", headers=headers, content=envelope.body
        )

    assert response.status == 413
    assert not called


class _FailingHTTPClient:
    async def post(self, *args: object, **kwargs: object) -> ClientResponse:
        raise ConnectError("uncertain transport outcome")


class _FakeHTTPClient:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.calls: list[tuple[str, tuple[tuple[bytes, bytes], ...], bytes, str | None]] = []

    async def post(
        self,
        path: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        body: bytes,
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        self.calls.append((path, headers, body, idempotency_key))
        return ClientResponse(self.status, (), b"", "1.1")


class _SlowHTTPClient(_FakeHTTPClient):
    def __init__(self) -> None:
        super().__init__(202)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def post(
        self,
        path: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        body: bytes,
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        self.calls.append((path, headers, body, idempotency_key))
        self.started.set()
        await self.release.wait()
        return ClientResponse(self.status, (), b"", "1.1")


class _StoppingHTTPClient(_FakeHTTPClient):
    def __init__(self, stopping: asyncio.Event) -> None:
        super().__init__(202)
        self.stopping = stopping

    async def post(
        self,
        path: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        body: bytes,
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        response = await super().post(
            path,
            headers=headers,
            body=body,
            idempotency_key=idempotency_key,
        )
        self.stopping.set()
        return response


@pytest.mark.asyncio
async def test_outbound_destination_serializes_signs_and_sends() -> None:
    app = Wreath()
    fake = _FakeHTTPClient()
    destination = app.webhooks("partners").destination(
        "receiver",
        client=fake,
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
    )

    result = await destination.send(
        "widget.changed",
        {"value": 9},
        event_id="evt-out",
        timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
    )

    assert result.outcome == "delivered"
    assert result.status == 202
    path, headers, body, idempotency_key = fake.calls[0]
    assert path == "/callbacks"
    assert body == b'{"value":9}'
    assert idempotency_key == "evt-out"
    verified = HMACWebhookVerifier(KEYS, max_age=300).verify(
        body=body,
        headers=dict(headers),
        now=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
    )
    assert verified.id == "evt-out"


@pytest.mark.asyncio
async def test_webhook_hub_exposes_narrow_csrf_exemption() -> None:
    app = Wreath()
    hooks = app.webhooks("csrf")
    source = hooks.source(
        "sender",
        path="/hooks/sender",
        verifier=HMACWebhookVerifier(KEYS),
    )

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        pass

    app.add_middleware(
        CSRFMiddleware("s" * 32, secure=False, exempt=hooks.csrf_exempt)
    )
    envelope = WebhookEnvelope(
        id="evt-csrf",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":1}',
    )
    headers = {
        name.decode("ascii"): value.decode("ascii")
        for name, value in HMACWebhookSigner(KEYS, key_id="current").headers(envelope)
    }
    async with TestClient(app) as client:
        accepted = await client.post(
            "/hooks/sender", headers=headers, content=envelope.body
        )
        rejected = await client.post("/not-a-webhook", content=b"{}")

    assert accepted.status == 204
    assert rejected.status == 403


def test_webhook_limits_are_positive_and_hubs_are_unique() -> None:
    with pytest.raises(ValueError):
        WebhookLimits(max_body_bytes=0)
    app = Wreath()
    app.webhooks("partners")
    with pytest.raises(ValueError, match="duplicate webhook hub"):
        app.webhooks("partners")

@pytest.mark.asyncio
async def test_relay_creates_new_id_and_preserves_correlation_and_causation() -> None:
    fake = _FakeHTTPClient()
    app = Wreath()
    destination = app.webhooks("relay").destination(
        "receiver",
        client=fake,
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
    )
    inbound = WebhookEnvelope(
        id="evt-in",
        type="source.changed",
        version="1",
        timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        content_type="application/json",
        body=b"{}",
        correlation_id="corr-1",
    )

    result = await destination.relay(
        inbound,
        "target.changed",
        {"value": 10},
        timestamp=datetime(2026, 7, 16, 10, 1, tzinfo=UTC),
    )

    assert result.event_id != inbound.id
    _path, headers, body, _idempotency_key = fake.calls[0]
    verified = HMACWebhookVerifier(KEYS, max_age=300).verify(
        body=body,
        headers=dict(headers),
        now=datetime(2026, 7, 16, 10, 1, tzinfo=UTC),
    )
    assert verified.correlation_id == "corr-1"
    assert verified.causation_id == "evt-in"
    assert verified.relay_path == ("receiver",)
    generated_names = {name for name, _value in headers}
    assert b"authorization" not in generated_names
    assert b"cookie" not in generated_names
    assert b"wreath-webhook-signature" in generated_names


@pytest.mark.asyncio
async def test_relay_rejects_signed_cycles_and_hop_limit() -> None:
    fake = _FakeHTTPClient()
    destination = Wreath().webhooks("relay-loops").destination(
        "receiver",
        client=fake,
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        max_relay_hops=2,
    )
    cycle = WebhookEnvelope(
        id="evt-cycle",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"{}",
        relay_path=("receiver",),
    )
    with pytest.raises(ValueError, match="loop"):
        await destination.relay(cycle, "widget.forwarded", {"value": 1})

    exhausted = WebhookEnvelope(
        id="evt-hops",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"{}",
        relay_path=("first", "second"),
    )
    with pytest.raises(ValueError, match="hop limit"):
        await destination.relay(exhausted, "widget.forwarded", {"value": 1})
    assert fake.calls == []


def test_relay_path_is_covered_by_signature_without_body_boundary_collision() -> None:
    envelope = WebhookEnvelope(
        id="evt-relay-signature",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"{}",
        relay_path=("first",),
    )
    signer = HMACWebhookSigner(KEYS, key_id="current")
    headers = dict(signer.headers(envelope))
    no_relay = WebhookEnvelope(
        id=envelope.id,
        type=envelope.type,
        version=envelope.version,
        timestamp=envelope.timestamp,
        content_type=envelope.content_type,
        body=envelope.body + b"\nrelay=first",
    )
    assert dict(signer.headers(no_relay))[b"wreath-webhook-signature"] != headers[
        b"wreath-webhook-signature"
    ]
    headers[b"wreath-webhook-relay-path"] = b"attacker"
    with pytest.raises(ValueError, match="signature"):
        HMACWebhookVerifier(KEYS).verify(
            body=envelope.body,
            headers=headers,
            now=envelope.timestamp,
        )


class _FakeRaw:
    def __init__(
        self,
        session: _FakeSession,
        sql: str,
        args: tuple[object, ...],
    ) -> None:
        self.session = session
        self.sql = sql
        self.args = args

    async def execute(self) -> str:
        self.session.calls.append((self.sql, self.args))
        return "INSERT 0 1"

    async def fetchrow(self) -> object:
        self.session.calls.append((self.sql, self.args))
        value = self.session.rows.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def fetchval(self) -> object:
        self.session.calls.append((self.sql, self.args))
        value = self.session.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class _FakeSession:
    def __init__(
        self,
        *,
        rows: list[object] | None = None,
        values: list[object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows = [] if rows is None else rows
        self.values = [] if values is None else values
        self.transactions: list[str] = []

    @asynccontextmanager
    async def begin(self):
        self.transactions.append("begin")
        try:
            yield self
        except BaseException:
            self.transactions.append("rollback")
            raise
        else:
            self.transactions.append("commit")

    def raw(self, sql: str, *args: object) -> _FakeRaw:
        return _FakeRaw(self, sql, args)


@pytest.mark.asyncio
async def test_durable_source_claim_handler_side_effect_and_completion_share_transaction() -> None:
    app = Wreath()
    session = _FakeSession(rows=[{"fencing_token": 7}], values=[1])

    @asynccontextmanager
    async def session_factory():
        yield session

    source = app.webhooks("transactional").source(
        "sender",
        path="/hooks/transactional",
        verifier=HMACWebhookVerifier(KEYS),
        inbox=PostgresWebhookInbox(),
        session_factory=session_factory,
        lease_owner="receiver-a",
    )

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        assert context.session is session
        await context.session.raw(
            "INSERT INTO handler_effect(value) VALUES ($1)", event.value
        ).execute()

    envelope = WebhookEnvelope(
        id="evt-transactional",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":9}',
    )
    headers = {
        name.decode(): value.decode()
        for name, value in HMACWebhookSigner(KEYS, key_id="current").headers(envelope)
    }
    async with TestClient(app) as client:
        response = await client.post(
            "/hooks/transactional", headers=headers, content=envelope.body
        )

    assert response.status == 204
    assert session.transactions == ["begin", "commit"]
    assert "INSERT INTO wreath_webhook_inbox" in session.calls[0][0]
    assert session.calls[1][0].startswith("INSERT INTO handler_effect")
    assert "state='completed'" in session.calls[2][0]


@pytest.mark.asyncio
async def test_durable_source_rolls_back_claim_and_side_effect_when_handler_fails() -> None:
    app = Wreath()
    session = _FakeSession(rows=[{"fencing_token": 1}])

    @asynccontextmanager
    async def session_factory():
        yield session

    source = app.webhooks("transaction-failure").source(
        "sender",
        path="/hooks/transaction-failure",
        verifier=HMACWebhookVerifier(KEYS),
        inbox=PostgresWebhookInbox(),
        session_factory=session_factory,
    )

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        await context.session.raw(
            "INSERT INTO handler_effect(value) VALUES ($1)", event.value
        ).execute()
        raise RuntimeError("handler failed")

    envelope = WebhookEnvelope(
        id="evt-rollback",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":9}',
    )
    headers = {
        name.decode(): value.decode()
        for name, value in HMACWebhookSigner(KEYS, key_id="current").headers(envelope)
    }
    async with TestClient(app) as client:
        response = await client.post(
            "/hooks/transaction-failure", headers=headers, content=envelope.body
        )

    assert response.status == 500
    assert session.transactions == ["begin", "rollback"]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_durable_destination_enqueues_exact_payload_in_caller_session() -> None:
    session = _FakeSession()
    outbox = PostgresWebhookOutbox()
    destination = Wreath().webhooks("durable").destination(
        "receiver",
        client=_FakeHTTPClient(),
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=outbox,
    )

    delivery_id = await destination.enqueue(
        session,
        "widget.changed",
        {"value": 11},
        event_id="evt-durable",
        timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        correlation_id="corr-1",
    )

    assert len(delivery_id) == 32
    sql, args = session.calls[0]
    assert sql.startswith("INSERT INTO wreath_webhook_outbox")
    assert args[1:4] == ("evt-durable", "receiver", "widget.changed")
    assert args[5:8] == ("1", b'{"value":11}', "application/json")
    assert args[10] == "evt-durable"
    assert args[12] == "corr-1"
    assert args[14] == ""


@pytest.mark.asyncio
async def test_durable_relay_persists_signed_loop_path() -> None:
    session = _FakeSession()
    destination = Wreath().webhooks("durable-relay").destination(
        "receiver",
        client=_FakeHTTPClient(),
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=PostgresWebhookOutbox(),
    )
    inbound = WebhookEnvelope(
        id="evt-inbound",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"{}",
        relay_path=("first",),
    )
    await destination.enqueue_relay(
        session, inbound, "widget.forwarded", {"value": 2}
    )
    _sql, args = session.calls[0]
    assert args[12] == "evt-inbound"
    assert args[13] == "evt-inbound"
    assert args[14] == "first,receiver"


def test_outbox_schema_is_explicit_and_identifier_is_validated() -> None:
    sql = PostgresWebhookOutbox().schema_sql()
    assert "CREATE TABLE IF NOT EXISTS wreath_webhook_outbox" in sql
    assert "CHECK (state IN" in sql
    assert "WHERE state IN ('pending','retry_wait')" in sql
    with pytest.raises(ValueError, match="identifier"):
        PostgresWebhookOutbox("bad; DROP TABLE users")


def test_inbox_schema_is_explicit_and_identifier_is_validated() -> None:
    sql = PostgresWebhookInbox().schema_sql()
    assert "CREATE TABLE IF NOT EXISTS wreath_webhook_inbox" in sql
    assert "PRIMARY KEY (source, message_id)" in sql
    assert "fencing_token bigint NOT NULL DEFAULT 1" in sql
    with pytest.raises(ValueError, match="identifier"):
        PostgresWebhookInbox("bad-name")


@pytest.mark.asyncio
async def test_inbox_claims_new_and_reclaims_stale_delivery_with_fencing() -> None:
    inbox = PostgresWebhookInbox()
    session = _FakeSession(rows=[{"fencing_token": 1}, {"fencing_token": 4}])
    envelope = _envelope()

    first = await inbox.claim(
        session,
        source="sender",
        envelope=envelope,
        lease_owner="worker-a",
        lease_seconds=30,
    )
    reclaimed = await inbox.claim(
        session,
        source="sender",
        envelope=envelope,
        lease_owner="worker-b",
        lease_seconds=30,
    )

    assert first.outcome == "claimed"
    assert first.fencing_token == 1
    assert reclaimed.outcome == "claimed"
    assert reclaimed.fencing_token == 4
    assert "ON CONFLICT (source, message_id)" in session.calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [("completed", "duplicate"), ("processing", "active"), ("failed", "failed")],
)
async def test_inbox_classifies_existing_delivery(state: str, expected: str) -> None:
    session = _FakeSession(
        rows=[None, {"state": state, "fencing_token": 3, "result_status": 204}]
    )
    claim = await PostgresWebhookInbox().claim(
        session,
        source="sender",
        envelope=_envelope(),
        lease_owner="worker",
        lease_seconds=30,
    )
    assert claim.outcome == expected
    assert claim.fencing_token == 3
    assert claim.result_status == 204


@pytest.mark.asyncio
async def test_inbox_completion_requires_current_fencing_token() -> None:
    inbox = PostgresWebhookInbox()
    current = _FakeSession(values=[1])
    await inbox.complete(
        current,
        source="sender",
        message_id="evt-1",
        fencing_token=2,
        result_status=204,
    )
    assert "fencing_token=$3" in current.calls[0][0]

    stale = _FakeSession(values=[None])
    with pytest.raises(RuntimeError, match="stale"):
        await inbox.complete(
            stale,
            source="sender",
            message_id="evt-1",
            fencing_token=1,
            result_status=204,
        )


def _delivery_row(*, attempts: int = 1, destination: str = "receiver") -> dict[str, object]:
    return {
        "delivery_id": "delivery-1",
        "event_id": "evt-dispatch",
        "destination": destination,
        "event_type": "widget.changed",
        "event_timestamp": datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        "payload_version": "1",
        "payload_bytes": b'{"value":12}',
        "content_type": "application/json",
        "key_id": "current",
        "attempts": attempts,
        "fencing_token": 4,
        "ordering_key": None,
        "correlation_id": "corr-1",
        "causation_id": None,
        "relay_path": "",
    }


@pytest.mark.asyncio
async def test_dispatcher_claims_sends_and_fenced_marks_delivered() -> None:
    session = _FakeSession(rows=[_delivery_row()], values=[1, 1])
    outbox = PostgresWebhookOutbox()
    fake = _FakeHTTPClient(202)
    destination = Wreath().webhooks("dispatch").destination(
        "receiver",
        client=fake,
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=outbox,
    )
    dispatcher = WebhookDispatcher(
        outbox,
        {"receiver": destination},
        worker_id="worker-a",
    )

    result = await dispatcher.run_once(session)

    assert result is not None and result.outcome == "delivered"
    assert "FOR UPDATE SKIP LOCKED" in session.calls[0][0]
    assert "state='sending'" in session.calls[1][0]
    assert "state='delivered'" in session.calls[2][0]
    assert session.calls[2][1][:2] == ("delivery-1", 4)
    assert fake.calls[0][2] == b'{"value":12}'


@pytest.mark.asyncio
async def test_dispatcher_schedules_retry_for_retryable_status() -> None:
    session = _FakeSession(rows=[_delivery_row(attempts=2)], values=[1, 1])
    outbox = PostgresWebhookOutbox()
    destination = Wreath().webhooks("dispatch").destination(
        "receiver",
        client=_FakeHTTPClient(503),
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=outbox,
    )
    dispatcher = WebhookDispatcher(
        outbox,
        {"receiver": destination},
        worker_id="worker-a",
        retry_delay=2,
    )

    result = await dispatcher.run_once(session)

    assert result is not None and result.outcome == "failed"
    assert "state='retry_wait'" in session.calls[2][0]
    assert session.calls[2][1][2] == 4
    assert session.calls[2][1][3] == 503


@pytest.mark.asyncio
async def test_dispatcher_terminally_fails_unknown_destination() -> None:
    session = _FakeSession(
        rows=[_delivery_row(destination="removed")],
        values=[1],
    )
    dispatcher = WebhookDispatcher(
        PostgresWebhookOutbox(),
        {},
        worker_id="worker-a",
    )

    result = await dispatcher.run_once(session)

    assert result is not None and result.failure == "UnknownDestination"
    assert "state='failed'" in session.calls[1][0]


@pytest.mark.asyncio
async def test_dispatcher_records_uncertain_transport_as_unknown() -> None:
    session = _FakeSession(rows=[_delivery_row()], values=[1, 1])
    outbox = PostgresWebhookOutbox()
    destination = Wreath().webhooks("dispatch").destination(
        "receiver",
        client=_FailingHTTPClient(),
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=outbox,
    )
    dispatcher = WebhookDispatcher(
        outbox,
        {"receiver": destination},
        worker_id="worker-a",
    )

    result = await dispatcher.run_once(session)

    assert result is not None and result.outcome == "unknown"
    assert result.failure == "ConnectError"
    assert "state='unknown'" in session.calls[2][0]


@pytest.mark.asyncio
async def test_dispatcher_renews_lease_while_delivery_is_in_flight() -> None:
    primary = _FakeSession(rows=[_delivery_row()], values=[1, 1])
    renewal = _FakeSession(values=[1, 1, 1])
    client = _SlowHTTPClient()
    destination = Wreath().webhooks("lease-renewal").destination(
        "receiver",
        client=client,
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=PostgresWebhookOutbox(),
    )
    dispatcher = WebhookDispatcher(
        PostgresWebhookOutbox(),
        {"receiver": destination},
        worker_id="worker-renew",
        lease_seconds=0.03,
    )

    @asynccontextmanager
    async def renewal_factory():
        yield renewal

    task = asyncio.create_task(
        dispatcher.run_once(primary, renewal_session_factory=renewal_factory)
    )
    await client.started.wait()
    await asyncio.sleep(0.025)
    assert dispatcher.readiness.in_flight == 1
    client.release.set()
    result = await task

    assert result is not None and result.outcome == "delivered"
    assert any("lease_expires_at=clock_timestamp()" in sql for sql, _ in renewal.calls)
    assert dispatcher.readiness.in_flight == 0


@pytest.mark.asyncio
async def test_remote_acceptance_before_ack_failure_is_reclaimed_and_redelivered() -> None:
    client = _FakeHTTPClient()
    outbox = PostgresWebhookOutbox()
    destination = Wreath().webhooks("ack-loss").destination(
        "receiver",
        client=client,
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=outbox,
    )
    dispatcher = WebhookDispatcher(
        outbox, {"receiver": destination}, worker_id="worker-a"
    )
    failed_ack = _FakeSession(rows=[_delivery_row()], values=[1, None])
    with pytest.raises(RuntimeError, match="stale webhook outbox"):
        await dispatcher.run_once(failed_ack)
    assert len(client.calls) == 1

    reclaimed_row = _delivery_row(attempts=2)
    recovered = _FakeSession(rows=[reclaimed_row], values=[1, 1])
    result = await dispatcher.run_once(recovered)
    assert result is not None and result.outcome == "delivered"
    assert len(client.calls) == 2
    assert "state IN ('leased','sending')" in recovered.calls[0][0]


@pytest.mark.asyncio
async def test_claim_and_pre_send_persistence_failures_do_not_send() -> None:
    client = _FakeHTTPClient()
    outbox = PostgresWebhookOutbox()
    destination = Wreath().webhooks("persistence-loss").destination(
        "receiver",
        client=client,
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=outbox,
    )
    dispatcher = WebhookDispatcher(
        outbox, {"receiver": destination}, worker_id="worker-a"
    )
    with pytest.raises(ConnectionError, match="claim lost"):
        await dispatcher.run_once(
            _FakeSession(rows=[ConnectionError("claim lost")])
        )
    with pytest.raises(RuntimeError, match="stale webhook outbox"):
        await dispatcher.run_once(
            _FakeSession(rows=[_delivery_row()], values=[None])
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_dispatcher_lifespan_management_exposes_readiness() -> None:
    app = Wreath()
    session = _FakeSession(rows=[None, None])

    @asynccontextmanager
    async def session_factory():
        yield session

    dispatcher = WebhookDispatcher(
        PostgresWebhookOutbox(), {}, worker_id="managed-worker"
    )
    dispatcher.manage(app, session_factory, idle_delay=1)
    with pytest.raises(RuntimeError, match="already managed"):
        dispatcher.manage(app, session_factory)
    async with TestClient(app):
        assert dispatcher.readiness.ready
        assert dispatcher.readiness.running
        assert app.state.webhook_dispatcher_managed_worker is dispatcher
    assert not dispatcher.readiness.running


@pytest.mark.asyncio
async def test_inbox_and_outbox_retention_purge_are_bounded() -> None:
    inbox_session = _FakeSession(values=[3])
    outbox_session = _FakeSession(values=[4])
    assert await PostgresWebhookInbox().purge(inbox_session, limit=25) == 3
    assert await PostgresWebhookOutbox().purge(outbox_session, limit=50) == 4
    assert "FOR UPDATE SKIP LOCKED LIMIT $1" in inbox_session.calls[0][0]
    assert inbox_session.calls[0][1] == (25,)
    assert "state IN ('delivered','failed','cancelled','unknown')" in outbox_session.calls[0][0]
    assert outbox_session.calls[0][1] == (50,)
    with pytest.raises(ValueError, match="limit"):
        await PostgresWebhookInbox().purge(inbox_session, limit=0)


@pytest.mark.asyncio
async def test_dispatcher_run_loop_is_owned_and_stops_cleanly() -> None:
    stopping = asyncio.Event()
    session = _FakeSession(rows=[_delivery_row()], values=[1, 1])
    outbox = PostgresWebhookOutbox()
    destination = Wreath().webhooks("dispatch").destination(
        "receiver",
        client=_StoppingHTTPClient(stopping),
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
        outbox=outbox,
    )
    dispatcher = WebhookDispatcher(
        outbox,
        {"receiver": destination},
        worker_id="worker-a",
    )

    @asynccontextmanager
    async def session_factory():
        yield session

    await dispatcher.run(session_factory, stopping, idle_delay=0.01)

    assert stopping.is_set()
    assert "state='delivered'" in session.calls[2][0]


@pytest.mark.asyncio
async def test_idle_dispatcher_observes_stop_without_leaking_tasks() -> None:
    stopping = asyncio.Event()
    session = _FakeSession(rows=[None])
    dispatcher = WebhookDispatcher(
        PostgresWebhookOutbox(),
        {},
        worker_id="worker-a",
    )

    @asynccontextmanager
    async def session_factory():
        yield session

    task = asyncio.create_task(dispatcher.run(session_factory, stopping, idle_delay=1))
    await asyncio.sleep(0)
    stopping.set()
    await task
    assert task.done()


def test_webhook_purge_pass_builders_take_no_database() -> None:
    """Both inbox and outbox took a positional database and discarded it."""
    import inspect

    for owner in (PostgresWebhookInbox, PostgresWebhookOutbox):
        parameters = inspect.signature(owner.purge_pass).parameters
        assert "database" not in parameters, owner.__name__
        assert [
            name
            for name, p in parameters.items()
            if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        ] == ["self"], owner.__name__


# --- the envelope and key checks nothing was watching -------------------------
#
# `wreath mutant` survived or never reached every guard in
# `WebhookEnvelope.__post_init__` and in the two HMAC classes' constructors: the
# suite builds well-formed envelopes with good keys, which is the right thing
# for it to do and the reason none of these had ever refused anything. Two of
# them defend the same signature-base ambiguity from opposite ends.


@pytest.mark.parametrize("field", ["id", "type", "version"])
def test_an_envelope_missing_an_identifying_field_is_refused(field: str) -> None:
    """All three are joined into the signature base, so none may be absent."""
    fields = {"id": "evt-1", "type": "widget.changed", "version": "1"}
    fields[field] = ""
    with pytest.raises(ValueError, match="id, type, and version are required"):
        WebhookEnvelope(
            timestamp=datetime.now(UTC), content_type="application/json",
            body=b"{}", **fields,
        )


@pytest.mark.parametrize("field", ["id", "type", "version"])
@pytest.mark.parametrize("char", ["\n", "\r", "\x00", "\x1f", "\x7f"])
def test_a_control_character_in_a_signed_field_is_refused(field: str, char: str) -> None:
    """The ambiguity attack the module's own comment describes.

    `_signature_base` joins these with newlines, so a newline inside one of them
    lets a single MAC cover more than one `(timestamp, id, type, body)` split --
    the signed fields stop being unambiguously recoverable from what was signed.
    Refused rather than escaped, because no real event id or type contains one.
    """
    fields = {"id": "evt-1", "type": "widget.changed", "version": "1"}
    fields[field] = fields[field] + char
    with pytest.raises(ValueError, match=f"{field} contains a control character"):
        WebhookEnvelope(
            timestamp=datetime.now(UTC), content_type="application/json",
            body=b"{}", **fields,
        )


def test_the_verifier_refuses_a_control_character_before_it_computes_a_mac() -> None:
    """The same refusal from the other end, on headers an attacker supplies.

    The signer cannot be trusted to have applied it: these bytes arrive over the
    wire. It has to happen before `_signature_base` sees them, so a forged split
    is never MAC'd at all.
    """
    envelope = _envelope()
    headers = dict(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))
    for header, name in (
        (b"wreath-webhook-id", "id"),
        (b"wreath-webhook-type", "type"),
        (b"wreath-webhook-version", "version"),
    ):
        tampered = dict(headers)
        tampered[header] = tampered[header] + b"\nwreath-webhook-id: evt-2"
        with pytest.raises(ValueError, match=f"{name} contains a control character"):
            HMACWebhookVerifier(KEYS).verify(
                body=envelope.body, headers=tampered, now=envelope.timestamp,
            )


def test_a_naive_timestamp_is_refused() -> None:
    """Replay is bounded by comparing timestamps, and a naive one has no meaning."""
    with pytest.raises(ValueError, match="must include a timezone"):
        WebhookEnvelope(
            id="evt-1", type="widget.changed", version="1",
            timestamp=datetime(2026, 7, 16, 10, 0),   # naive, which is the point
            content_type="application/json", body=b"{}",
        )


def test_a_relay_path_that_is_too_long_or_malformed_is_refused() -> None:
    """The hop bound, and the format that makes a hop id unambiguous."""
    common = {
        "id": "evt-1", "type": "widget.changed", "version": "1",
        "timestamp": datetime.now(UTC), "content_type": "application/json",
        "body": b"{}",
    }
    assert WebhookEnvelope(relay_path=tuple(f"h{n}" for n in range(32)), **common)
    with pytest.raises(ValueError, match="invalid or too long"):
        WebhookEnvelope(relay_path=tuple(f"h{n}" for n in range(33)), **common)
    for bad in ("has space", "has,comma", "", "has\nnewline"):
        with pytest.raises(ValueError, match="invalid or too long"):
            WebhookEnvelope(relay_path=("ok", bad), **common)


def test_a_relay_path_that_revisits_a_hop_is_refused_as_a_loop() -> None:
    """Distinct from the length bound: a two-hop cycle never reaches 32."""
    with pytest.raises(ValueError, match="contains a loop"):
        WebhookEnvelope(
            id="evt-1", type="widget.changed", version="1",
            timestamp=datetime.now(UTC), content_type="application/json",
            body=b"{}", relay_path=("a", "b", "a"),
        )


def test_a_signer_asked_for_a_key_it_does_not_hold_refuses() -> None:
    """Redelivering an old row names its original key, which may be retired."""
    signer = HMACWebhookSigner(KEYS, key_id="current")
    with pytest.raises(ValueError, match="signing key is unavailable"):
        signer.headers(_envelope(), key_id="retired")
    # The default is used when none is named, and it is the one it was built with.
    assert dict(signer.headers(_envelope()))[b"wreath-webhook-key-id"] == b"current"


def test_a_verifier_with_no_usable_key_is_refused_at_construction() -> None:
    """A verifier holding an empty secret would MAC everything to the same value."""
    with pytest.raises(ValueError, match="non-empty webhook verification key"):
        HMACWebhookVerifier({})
    with pytest.raises(ValueError, match="non-empty webhook verification key"):
        HMACWebhookVerifier({"current": b""})
    # One bad entry poisons the mapping, exactly as for origins elsewhere.
    with pytest.raises(ValueError, match="non-empty webhook verification key"):
        HMACWebhookVerifier({"current": KEYS["current"], "previous": b""})


def test_a_verifier_replay_window_that_can_never_hold_is_refused() -> None:
    """`max_age` bounds replay; zero or less accepts nothing and is a typo."""
    with pytest.raises(ValueError, match="max_age must be positive"):
        HMACWebhookVerifier(KEYS, max_age=0)
    with pytest.raises(ValueError, match="max_age must be positive"):
        HMACWebhookVerifier(KEYS, max_age=-1)


def test_a_dispatcher_with_impossible_limits_is_refused() -> None:
    """Three bounds behind one message, and only two of them had ever been read.

    `retry_delay` is the one a mutation sweep found: dropping `retry_delay < 0`
    left a dispatcher that sleeps a negative number of seconds between attempts,
    which `asyncio.sleep` accepts and returns from immediately -- so a failing
    destination is retried in a tight loop until `max_attempts`, at whatever
    rate the event loop can manage. That is the shape of an accidental
    denial-of-service aimed at somebody else's endpoint, and it configures
    without complaint.

    All three clauses are asserted here rather than only the new one, because a
    single `or` chain behind a single message is exactly where a test that
    checks one arm reports the other two as covered.
    """
    outbox = PostgresWebhookOutbox()
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="w", retry_delay=-0.5)
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="w", lease_seconds=0)
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="w", max_attempts=0)
    with pytest.raises(ValueError, match="worker_id cannot be empty"):
        WebhookDispatcher(outbox, {}, worker_id="")
    # Zero delay is not negative: retrying immediately is a choice, not a typo.
    assert WebhookDispatcher(outbox, {}, worker_id="w", retry_delay=0.0) is not None
