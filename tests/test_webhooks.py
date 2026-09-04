from __future__ import annotations

import asyncio
import hashlib
import hmac
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _pgfidelity import check_for

from wreath import Wreath
from wreath.http_client import ClientResponse, ConnectError
from wreath.policy import CsrfPolicy, HttpPolicy
from wreath.testing import TestClient
from wreath.webhooks import (
    GitHubWebhookVerifier,
    HMACWebhookSigner,
    HMACWebhookVerifier,
    LocalReplayStore,
    PostgresWebhookInbox,
    PostgresWebhookOutbox,
    StandardWebhookVerifier,
    StripeWebhookVerifier,
    WebhookContext,
    WebhookDispatcher,
    WebhookEnvelope,
    WebhookLimits,
    WebhookSource,
    _legacy_signature_base,
    _outbox_delivery,
    _retention_purge_pass,
)

KEYS = {"current": b"a sufficiently long webhook test secret"}


async def _raw_post(
    app: Wreath, path: str, body: bytes, headers: list[tuple[bytes, bytes]]
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "https",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "server": ("test", 443),
        "client": ("127.0.0.1", 1),
        "root_path": "",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("body", "not-bytes", "webhook body must be bytes-like"),
        ("relay_path", "not-a-sequence", "relay_path must be a tuple or list"),
    ],
)
def test_webhook_envelope_refuses_invalid_container_types(
    field: str, value: object, message: str
) -> None:
    values: Any = {
        "id": "evt-1",
        "type": "widget.changed",
        "version": "1",
        "timestamp": datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        "content_type": "application/json",
        "body": b"{}",
    }
    values[field] = value

    with pytest.raises(TypeError, match=message):
        WebhookEnvelope(**values)


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
    assert compared[0][0].startswith(b"v2=")


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
    keys = {"current": b"c" * 32, "previous": b"p" * 32}
    envelope = _envelope()
    verifier = HMACWebhookVerifier(keys)
    for key_id in keys:
        headers = dict(HMACWebhookSigner(keys, key_id=key_id).headers(envelope))
        assert (
            verifier.verify(
                body=envelope.body,
                headers=headers,
                now=envelope.timestamp,
            ).id
            == envelope.id
        )

    previous_headers = dict(HMACWebhookSigner(keys, key_id="previous").headers(envelope))
    with pytest.raises(ValueError, match="key"):
        HMACWebhookVerifier({"current": keys["current"]}).verify(
            body=envelope.body,
            headers=previous_headers,
            now=envelope.timestamp,
        )


@pytest.mark.parametrize("secret", [b"", b"x", b"x" * 31])
def test_wreath_webhook_profile_refuses_short_hmac_keys(secret: bytes) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        HMACWebhookVerifier({"current": secret})


def test_webhook_signer_validates_every_rotation_key() -> None:
    with pytest.raises(ValueError, match="previous.*at least 32 bytes"):
        HMACWebhookSigner({"current": b"c" * 32, "previous": b""}, key_id="current")


@pytest.mark.parametrize("key_id", ["forged\r\nheader", "forged\x7fheader"])
def test_webhook_verifier_refuses_control_characters_in_key_ids(key_id: str) -> None:
    with pytest.raises(ValueError, match="control character"):
        HMACWebhookVerifier({key_id: b"k" * 32})


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
async def test_local_replay_store_refuses_key_spray_without_evicting_live_claims() -> None:
    store = LocalReplayStore(max_entries=2, ttl=60)

    assert await store.claim("trusted", "original", now=0)
    assert await store.claim("attacker", "spray-1", now=1)
    assert not await store.claim("attacker", "spray-2", now=2)
    assert not await store.claim("trusted", "original", now=3)


@pytest.mark.asyncio
async def test_local_replay_store_is_bounded_and_expires() -> None:
    store = LocalReplayStore(max_entries=2, ttl=10)
    assert await store.claim("source", "one", now=0)
    assert not await store.claim("source", "one", now=1)
    assert await store.claim("source", "two", now=1)
    assert not await store.claim("source", "three", now=2)
    assert store.size == 2
    assert await store.claim("source", "one", now=20)


@pytest.mark.asyncio
async def test_local_replay_store_covers_the_exact_verifier_window_boundary() -> None:
    store = LocalReplayStore(max_entries=1, ttl=10)

    assert await store.claim("source", "event", now=0)
    assert not await store.claim("source", "event", now=10)
    assert await store.claim("source", "event", now=10.000001)


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
        expected = key not in model and len(model) < 7
        if expected:
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


async def test_inbound_source_refuses_duplicate_signature_headers() -> None:
    app = Wreath()
    source = app.webhooks("partners").source(
        "sender", path="/hooks/sender", verifier=HMACWebhookVerifier(KEYS)
    )
    seen: list[dict[str, Any]] = []

    @source.event("widget.changed", payload=dict)
    async def changed(_context: WebhookContext, event: dict[str, Any]) -> None:
        seen.append(event)

    envelope = WebhookEnvelope(
        id="evt-live",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"{}",
    )
    headers = list(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))
    headers.append((b"wreath-webhook-signature", b"sha256=" + b"0" * 64))
    sent = await _raw_post(app, "/hooks/sender", envelope.body, headers)

    assert sent[0]["status"] == 401
    assert seen == []


@pytest.mark.asyncio
async def test_replay_is_claimed_before_payload_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_calls = 0

    def compile_validator(_payload: object):
        def validate(value: object, _path: tuple[str, ...]) -> object:
            nonlocal validation_calls
            validation_calls += 1
            return value

        return validate

    monkeypatch.setattr("wreath.webhooks._body_validator", compile_validator)
    app = Wreath()
    source = app.webhooks("partners").source(
        "sender", path="/hooks/sender", verifier=HMACWebhookVerifier(KEYS)
    )

    @source.event("widget.changed", payload=dict)
    async def changed(_context: WebhookContext, _event: object) -> None:
        pass

    envelope = WebhookEnvelope(
        id="evt-replayed",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"{}",
    )
    headers = list(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))

    first = await _raw_post(app, "/hooks/sender", envelope.body, headers)
    duplicate = await _raw_post(app, "/hooks/sender", envelope.body, headers)

    assert first[0]["status"] == 204
    assert duplicate[0]["status"] == 409
    assert validation_calls == 1


@pytest.mark.asyncio
async def test_replay_identity_is_scoped_to_its_hub() -> None:
    app = Wreath()
    replay = LocalReplayStore(max_entries=8, ttl=300)
    calls: list[str] = []
    for hub_name in ("tenant-a", "tenant-b"):
        source = app.webhooks(hub_name).source(
            "provider",
            path=f"/hooks/{hub_name}",
            verifier=HMACWebhookVerifier(KEYS),
            replay=replay,
        )

        @source.event("widget.changed", payload=WidgetChanged)
        async def changed(context: WebhookContext, _event: WidgetChanged) -> None:
            calls.append(context.source)

    envelope = WebhookEnvelope(
        id="provider-event-id",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":1}',
    )
    headers = list(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))

    first = await _raw_post(app, "/hooks/tenant-a", envelope.body, headers)
    second = await _raw_post(app, "/hooks/tenant-b", envelope.body, headers)

    assert first[0]["status"] == 204
    assert second[0]["status"] == 204
    assert calls == ["provider", "provider"]


@pytest.mark.asyncio
async def test_direct_sources_retain_distinct_default_replay_namespaces() -> None:
    app = Wreath()
    replay = LocalReplayStore(max_entries=8, ttl=300)
    calls: list[str] = []
    for name in ("provider-a", "provider-b"):
        source = WebhookSource(
            app,
            name,
            path=f"/hooks/{name}",
            verifier=HMACWebhookVerifier(KEYS),
            replay=replay,
            limits=WebhookLimits(),
            inbox=None,
            session_factory=None,
            lease_owner="receiver",
            lease_seconds=30,
        )

        @source.event("widget.changed", payload=WidgetChanged)
        async def changed(context: WebhookContext, _event: WidgetChanged) -> None:
            calls.append(context.source)

    envelope = WebhookEnvelope(
        id="provider-event-id",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":1}',
    )
    headers = list(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))

    first = await _raw_post(app, "/hooks/provider-a", envelope.body, headers)
    second = await _raw_post(app, "/hooks/provider-b", envelope.body, headers)

    assert first[0]["status"] == 204
    assert second[0]["status"] == 204
    assert calls == ["provider-a", "provider-b"]


@pytest.mark.asyncio
async def test_github_replay_cannot_change_its_unsigned_delivery_id() -> None:
    secret = b"github webhook secret"
    body = b'{"value":7}'
    signature = b"sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest().encode("ascii")
    app = Wreath()
    source = app.webhooks("github").source(
        "github",
        path="/hooks/github",
        verifier=GitHubWebhookVerifier(secret),
    )
    calls = 0

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(_context: WebhookContext, _event: WidgetChanged) -> None:
        nonlocal calls
        calls += 1

    common = [
        (b"x-hub-signature-256", signature),
        (b"x-github-event", b"widget.changed"),
    ]
    first = await _raw_post(
        app,
        "/hooks/github",
        body,
        [*common, (b"x-github-delivery", b"delivery-1")],
    )
    replay = await _raw_post(
        app,
        "/hooks/github",
        body,
        [*common, (b"x-github-delivery", b"attacker-changed-it")],
    )

    assert first[0]["status"] == 204
    assert replay[0]["status"] == 409
    assert calls == 1


@pytest.mark.asyncio
async def test_inbound_source_uses_public_verifier_protocol_and_compiled_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath.binding import _body_validator as compile_validator

    app = Wreath()
    verifier_calls = 0
    compile_calls = 0
    validation_calls = 0

    class TrackingVerifier:
        def __init__(self) -> None:
            self._delegate = HMACWebhookVerifier(KEYS)
            self.max_age = self._delegate.max_age

        def verify(self, **options: object) -> WebhookEnvelope:
            nonlocal verifier_calls
            verifier_calls += 1
            return self._delegate.verify(**options)

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
        verifier=TrackingVerifier(),
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
            headers = {name.decode(): value.decode() for name, value in signer.headers(envelope)}
            assert (
                await client.post("/hooks/optimized", headers=headers, content=envelope.body)
            ).status == 204

    assert compile_calls == 1
    assert validation_calls == 2
    assert verifier_calls == 2


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
        response = await client.post("/hooks/bounded", headers=headers, content=envelope.body)

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
async def test_outbound_destination_sends_a_bytearray_verbatim() -> None:
    fake = _FakeHTTPClient()
    destination = (
        Wreath()
        .webhooks("partners-bytes")
        .destination(
            "receiver",
            client=fake,
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
        )
    )

    result = await destination.send(
        "widget.changed",
        bytearray(b"opaque payload"),
        event_id="evt-bytes",
        timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
    )

    assert result.outcome == "delivered"
    assert fake.calls[0][2] == b"opaque payload"


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

    app.configure_http_policy(
        HttpPolicy(csrf=CsrfPolicy("s" * 32, secure=False, exempt=hooks.csrf_exempt))
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
        accepted = await client.post("/hooks/sender", headers=headers, content=envelope.body)
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


@pytest.mark.parametrize("value", [True, 1.5, float("nan"), float("inf")])
def test_webhook_limits_require_positive_integers(value: Any) -> None:
    for field in (
        "max_body_bytes",
        "max_headers",
        "max_header_bytes",
        "max_event_id_bytes",
    ):
        with pytest.raises(ValueError, match=f"webhook limit {field} must be a positive integer"):
            WebhookLimits(**{field: value})


def test_source_refuses_a_replay_store_shorter_than_the_signature_window() -> None:
    app = Wreath()

    @asynccontextmanager
    async def session_factory():
        yield object()

    with pytest.raises(ValueError, match="replay store ttl must cover"):
        app.webhooks("partners").source(
            "sender",
            path="/hooks/sender",
            verifier=HMACWebhookVerifier(KEYS, max_age=300),
            replay=LocalReplayStore(max_entries=10, ttl=299),
        )

    durable_app = Wreath()
    with pytest.raises(ValueError, match="inbox retention must cover"):
        durable_app.webhooks("partners").source(
            "sender",
            path="/hooks/sender",
            verifier=HMACWebhookVerifier(KEYS, max_age=300),
            inbox=PostgresWebhookInbox(retention_seconds=299),
            session_factory=session_factory,
        )


def test_webhook_hub_without_durable_stores_has_no_schema_owners() -> None:
    app = Wreath()
    hooks = app.webhooks("ephemeral")
    hooks.source(
        "sender",
        path="/hooks/ephemeral",
        verifier=HMACWebhookVerifier(KEYS),
    )
    hooks.destination(
        "receiver",
        client=_FakeHTTPClient(),
        path="/callbacks",
        signer=HMACWebhookSigner(KEYS, key_id="current"),
    )

    assert hooks.schema_owners == ()


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
    destination = (
        Wreath()
        .webhooks("relay-loops")
        .destination(
            "receiver",
            client=fake,
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            max_relay_hops=2,
        )
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
    assert (
        dict(signer.headers(no_relay))[b"wreath-webhook-signature"]
        != headers[b"wreath-webhook-signature"]
    )
    headers[b"wreath-webhook-relay-path"] = b"attacker"
    with pytest.raises(ValueError, match="signature"):
        HMACWebhookVerifier(KEYS).verify(
            body=envelope.body,
            headers=headers,
            now=envelope.timestamp,
        )


@pytest.mark.parametrize(
    ("header", "replacement"),
    (
        (b"wreath-webhook-version", b"2"),
        (b"wreath-correlation-id", b"attacker-correlation"),
        (b"wreath-causation-id", b"attacker-cause"),
    ),
)
def test_wreath_signature_authenticates_semantic_metadata(
    header: bytes, replacement: bytes
) -> None:
    envelope = WebhookEnvelope(
        id="evt-metadata",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"{}",
        correlation_id="trusted-correlation",
        causation_id="trusted-cause",
    )
    headers = dict(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))
    headers[header] = replacement

    with pytest.raises(ValueError, match="invalid webhook signature"):
        HMACWebhookVerifier(KEYS).verify(
            body=envelope.body,
            headers=headers,
            now=envelope.timestamp,
        )


@pytest.mark.parametrize(
    "header", [b"wreath-correlation-id", b"wreath-causation-id"]
)
def test_wreath_signature_refuses_empty_unsigned_lineage_metadata(header: bytes) -> None:
    envelope = _envelope()
    headers = dict(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))
    headers[header] = b""

    with pytest.raises(ValueError, match="must not be empty"):
        HMACWebhookVerifier(KEYS).verify(
            body=envelope.body,
            headers=headers,
            now=envelope.timestamp,
        )


def test_legacy_wreath_signature_is_limited_to_metadata_free_version_one() -> None:
    envelope = _envelope()
    headers = dict(HMACWebhookSigner(KEYS, key_id="current").headers(envelope))
    timestamp = headers[b"wreath-webhook-timestamp"]
    legacy_base = b"\n".join(
        (b"wreath-v1", timestamp, b"evt-1", b"widget.changed", envelope.body)
    )
    assert (
        _legacy_signature_base(timestamp, envelope.id, envelope.type, envelope.body, ())
        == legacy_base
    )
    legacy = hmac.new(
        KEYS["current"],
        legacy_base,
        hashlib.sha256,
    ).hexdigest()
    headers[b"wreath-webhook-signature"] = f"v1={legacy}".encode("ascii")

    verified = HMACWebhookVerifier(KEYS).verify(
        body=envelope.body,
        headers=headers,
        now=envelope.timestamp,
    )
    assert verified.version == "1"

    unsigned_prefix = dict(headers)
    unsigned_prefix[b"wreath-webhook-signature"] = legacy.encode("ascii")
    wrong_version = dict(headers)
    wrong_version[b"wreath-webhook-version"] = b"2"
    unsigned_correlation = dict(headers)
    unsigned_correlation[b"wreath-correlation-id"] = b"unsigned"
    unsigned_causation = dict(headers)
    unsigned_causation[b"wreath-causation-id"] = b"unsigned"
    for refused in (
        unsigned_prefix,
        wrong_version,
        unsigned_correlation,
        unsigned_causation,
    ):
        with pytest.raises(ValueError, match="invalid webhook signature"):
            HMACWebhookVerifier(KEYS).verify(
                body=envelope.body,
                headers=refused,
                now=envelope.timestamp,
            )

    relay_headers = dict(headers)
    relay_headers[b"wreath-webhook-relay-path"] = b"sender"
    relay_base = b"\n".join(
        (
            b"wreath-v1-relay",
            timestamp,
            b"evt-1",
            b"widget.changed",
            b"sender",
            envelope.body,
        )
    )
    assert (
        _legacy_signature_base(
            timestamp, envelope.id, envelope.type, envelope.body, ("sender",)
        )
        == relay_base
    )
    relay_signature = hmac.new(
        KEYS["current"],
        relay_base,
        hashlib.sha256,
    ).hexdigest()
    relay_headers[b"wreath-webhook-signature"] = f"v1={relay_signature}".encode("ascii")
    relayed = HMACWebhookVerifier(KEYS).verify(
        body=envelope.body,
        headers=relay_headers,
        now=envelope.timestamp,
    )
    assert relayed.relay_path == ("sender",)


def test_signer_can_redeliver_a_stored_legacy_outbox_profile() -> None:
    envelope = _envelope()
    headers = dict(
        HMACWebhookSigner(KEYS, key_id="current").headers(
            envelope, signature_profile="wreath-v1-hmac-sha256"
        )
    )

    assert headers[b"wreath-webhook-signature"].startswith(b"v1=")
    assert HMACWebhookVerifier(KEYS).verify(
        body=envelope.body, headers=headers, now=envelope.timestamp
    ) == envelope


@pytest.mark.parametrize(
    "envelope",
    [
        WebhookEnvelope(
            id="evt-1",
            type="widget.changed",
            version="2",
            timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            content_type="application/json",
            body=b"{}",
        ),
        WebhookEnvelope(
            id="evt-1",
            type="widget.changed",
            version="1",
            timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            content_type="application/json",
            body=b"{}",
            correlation_id="correlation",
        ),
        WebhookEnvelope(
            id="evt-1",
            type="widget.changed",
            version="1",
            timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            content_type="application/json",
            body=b"{}",
            causation_id="causation",
        ),
    ],
)
def test_legacy_signer_refuses_envelopes_with_unsigned_fields(
    envelope: WebhookEnvelope,
) -> None:
    with pytest.raises(ValueError, match="profile is unsupported"):
        HMACWebhookSigner(KEYS, key_id="current").headers(
            envelope, signature_profile="wreath-v1-hmac-sha256"
        )


def test_signer_refuses_an_unknown_stored_signature_profile() -> None:
    with pytest.raises(ValueError, match="profile is unsupported"):
        HMACWebhookSigner(KEYS, key_id="current").headers(
            _envelope(), signature_profile="unknown-profile"
        )


@pytest.mark.asyncio
async def test_inbox_claim_uses_the_authenticated_deduplication_identity() -> None:
    envelope = WebhookEnvelope(
        id="unsigned-provider-id",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b"{}",
        deduplication_id="authenticated-body-digest",
    )
    session = _FakeSession(rows=[{"fencing_token": 1}])

    await PostgresWebhookInbox().claim(
        session,
        source="provider",
        envelope=envelope,
        lease_owner="worker",
        lease_seconds=30,
    )

    assert session.calls[0][1][1] == "authenticated-body-digest"


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
        check_for(self, sql, args)
        return _FakeRaw(self, sql, args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "deduplication_id", "status"),
    [
        (b'{"value":1}', "authenticated-body-digest", 204),
        (b'{"value":1}', None, 204),
        (b"not-json", "authenticated-body-digest", 400),
        (b"not-json", None, 400),
    ],
)
async def test_durable_source_completes_the_selected_replay_identity(
    body: bytes, deduplication_id: str | None, status: int
) -> None:
    envelope = WebhookEnvelope(
        id="provider-event-id",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=body,
        deduplication_id=deduplication_id,
    )

    class Verifier:
        max_age = 300.0

        def verify(self, **_options: Any) -> WebhookEnvelope:
            return envelope

    app = Wreath()
    session = _FakeSession(rows=[{"fencing_token": 7}], values=[1])

    @asynccontextmanager
    async def session_factory():
        yield session

    source = app.webhooks("durable").source(
        "provider",
        path="/hooks/durable-identity",
        verifier=Verifier(),
        inbox=PostgresWebhookInbox(),
        session_factory=session_factory,
        lease_owner="receiver-a",
    )

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(_context: WebhookContext, _event: WidgetChanged) -> None:
        pass

    response = await _raw_post(app, "/hooks/durable-identity", body, [])

    assert response[0]["status"] == status
    completion = next(args for sql, args in session.calls if "SET state='completed'" in sql)
    assert completion[1] == (deduplication_id or envelope.id)


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
        response = await client.post("/hooks/transactional", headers=headers, content=envelope.body)

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
@pytest.mark.parametrize(
    ("result_status", "identity_matches", "expected"),
    [(202, True, 202), (None, True, 204), (202, False, 409)],
)
async def test_durable_source_classifies_a_completed_delivery_identity(
    result_status: int | None,
    identity_matches: bool,
    expected: int,
) -> None:
    app = Wreath()
    session = _FakeSession(
        rows=[
            None,
            {
                "state": "completed",
                "fencing_token": 3,
                "result_status": result_status,
                "identity_matches": identity_matches,
            },
        ]
    )

    @asynccontextmanager
    async def session_factory():
        yield session

    source = app.webhooks("completed-replay").source(
        "sender",
        path="/hooks/completed-replay",
        verifier=HMACWebhookVerifier(KEYS),
        inbox=PostgresWebhookInbox(),
        session_factory=session_factory,
    )

    @source.event("widget.changed", payload=WidgetChanged)
    async def changed(context: WebhookContext, event: WidgetChanged) -> None:
        raise AssertionError("a completed delivery must not be handled again")

    envelope = WebhookEnvelope(
        id="evt-completed",
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
            "/hooks/completed-replay", headers=headers, content=envelope.body
        )

    assert response.status == expected


@pytest.mark.asyncio
async def test_durable_destination_enqueues_exact_payload_in_caller_session() -> None:
    session = _FakeSession()
    outbox = PostgresWebhookOutbox()
    destination = (
        Wreath()
        .webhooks("durable")
        .destination(
            "receiver",
            client=_FakeHTTPClient(),
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=outbox,
        )
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
    destination = (
        Wreath()
        .webhooks("durable-relay")
        .destination(
            "receiver",
            client=_FakeHTTPClient(),
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=PostgresWebhookOutbox(),
        )
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
    await destination.enqueue_relay(session, inbound, "widget.forwarded", {"value": 2})
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
    with pytest.raises(ValueError, match="identifier"):
        PostgresWebhookOutbox("WebhookEvents")
    with pytest.raises(ValueError, match="63-byte"):
        PostgresWebhookOutbox("w" * 64)


def test_inbox_schema_is_explicit_and_identifier_is_validated() -> None:
    sql = PostgresWebhookInbox().schema_sql()
    assert "CREATE TABLE IF NOT EXISTS wreath_webhook_inbox" in sql
    assert "PRIMARY KEY (source, message_id)" in sql
    assert "fencing_token bigint NOT NULL DEFAULT 1" in sql
    with pytest.raises(ValueError, match="identifier"):
        PostgresWebhookInbox("bad-name")
    with pytest.raises(ValueError, match="identifier"):
        PostgresWebhookInbox("WebhookEvents")
    with pytest.raises(ValueError, match="63-byte"):
        PostgresWebhookInbox("w" * 64)


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
    envelope = _envelope()
    session = _FakeSession(
        rows=[
            None,
            {
                "state": state,
                "fencing_token": 3,
                "result_status": 204,
                "identity_matches": True,
            },
        ]
    )
    claim = await PostgresWebhookInbox().claim(
        session,
        source="sender",
        envelope=envelope,
        lease_owner="worker",
        lease_seconds=30,
    )
    assert claim.outcome == expected
    assert claim.fencing_token == 3
    assert claim.result_status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_version", "stored_body"),
    [("2", b'{"value":1}'), ("1", b'{"value":2}')],
)
async def test_inbox_refuses_message_id_reuse_for_a_different_event_identity(
    stored_version: str,
    stored_body: bytes,
) -> None:
    envelope = _envelope()
    def identity_hash(event_type: str, body: bytes) -> bytes:
        identity = hashlib.sha256()
        identity.update(b"wreath-webhook-inbox-v2\0")
        identity.update(event_type.encode("utf-8"))
        identity.update(b"\0")
        identity.update(body)
        return identity.digest()

    stored_hash = identity_hash(envelope.type, stored_body)
    payload_hash = identity_hash(envelope.type, envelope.body)
    session = _FakeSession(
        rows=[
            None,
            {
                "state": "completed",
                "fencing_token": 3,
                "result_status": 204,
                "identity_matches": (
                    stored_version == envelope.version and stored_hash == payload_hash
                ),
            },
        ]
    )
    claim = await PostgresWebhookInbox().claim(
        session,
        source="sender",
        envelope=envelope,
        lease_owner="worker",
        lease_seconds=30,
    )
    insert_sql = session.calls[0][0]
    select_sql, select_args = session.calls[1]
    assert "i.payload_version=EXCLUDED.payload_version" in insert_sql
    assert "i.payload_hash=EXCLUDED.payload_hash" in insert_sql
    assert "payload_version=$3 AND payload_hash=$4 AS identity_matches" in select_sql
    assert select_args == (
        "sender",
        "evt-1",
        envelope.version,
        payload_hash,
    )
    assert (stored_version, stored_hash) != (envelope.version, payload_hash)
    assert claim.outcome == "conflict"


@pytest.mark.asyncio
async def test_inbox_event_identity_includes_the_authenticated_event_type() -> None:
    inbox = PostgresWebhookInbox()
    original = _envelope()
    changed_type = WebhookEnvelope(
        id=original.id,
        type="widget.deleted",
        version=original.version,
        timestamp=original.timestamp,
        content_type=original.content_type,
        body=original.body,
    )
    session = _FakeSession(rows=[{"fencing_token": 1}, {"fencing_token": 2}])

    for envelope in (original, changed_type):
        await inbox.claim(
            session,
            source="sender",
            envelope=envelope,
            lease_owner="worker",
            lease_seconds=30,
        )

    first_identity_hash = session.calls[0][1][3]
    second_identity_hash = session.calls[1][1][3]
    assert first_identity_hash != second_identity_hash


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


@pytest.mark.asyncio
async def test_settled_webhook_rows_receive_a_bounded_retention_deadline() -> None:
    inbox = PostgresWebhookInbox(retention_seconds=60)
    inbox_session = _FakeSession(values=[1])
    await inbox.complete(
        inbox_session,
        source="sender",
        message_id="evt-1",
        fencing_token=2,
        result_status=204,
    )

    delivery = _outbox_delivery(_delivery_row())
    outbox = PostgresWebhookOutbox(retention_seconds=120)
    outbox_sessions = [_FakeSession(values=[1]) for _ in range(3)]
    await outbox.mark_delivered(outbox_sessions[0], delivery, status=204)
    await outbox.mark_unknown(outbox_sessions[1], delivery, failure="timeout")
    await outbox.mark_failed(outbox_sessions[2], delivery, status=400, failure="refused")

    assert "retention_until" in inbox_session.calls[0][0]
    assert inbox_session.calls[0][1][-1] == 60
    for session in outbox_sessions:
        assert "retention_until" in session.calls[0][0]
        assert session.calls[0][1][-1] == 120


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(True, TypeError), ("204", TypeError), (99, ValueError), (600, ValueError)],
)
async def test_inbox_refuses_an_invalid_replay_response_status(
    status: Any, error: type[Exception]
) -> None:
    session = _FakeSession()

    with pytest.raises(error, match="result_status must be an HTTP status"):
        await PostgresWebhookInbox().complete(
            session,
            source="sender",
            message_id="evt-1",
            fencing_token=2,
            result_status=status,
        )

    assert session.calls == []


@pytest.mark.parametrize("retention", [True, "60", 0, -1, float("nan"), float("inf")])
def test_webhook_storage_retention_must_be_positive_and_finite(retention: Any) -> None:
    with pytest.raises(ValueError, match="retention_seconds must be positive and finite"):
        PostgresWebhookInbox(retention_seconds=retention)
    with pytest.raises(ValueError, match="retention_seconds must be positive and finite"):
        PostgresWebhookOutbox(retention_seconds=retention)


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


def test_outbox_delivery_preserves_the_stored_signature_profile() -> None:
    row = _delivery_row()
    row["signature_profile"] = "wreath-v1-hmac-sha256"

    assert _outbox_delivery(row).signature_profile == "wreath-v1-hmac-sha256"


@pytest.mark.asyncio
async def test_dispatcher_claims_sends_and_fenced_marks_delivered() -> None:
    session = _FakeSession(rows=[_delivery_row()], values=[1, 1])
    outbox = PostgresWebhookOutbox()
    fake = _FakeHTTPClient(202)
    destination = (
        Wreath()
        .webhooks("dispatch")
        .destination(
            "receiver",
            client=fake,
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=outbox,
        )
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
    destination = (
        Wreath()
        .webhooks("dispatch")
        .destination(
            "receiver",
            client=_FakeHTTPClient(503),
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=outbox,
        )
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
    # attempt 2 of base 2 doubles to 4, then `compute_backoff` jitters it by
    # +/-20% -- so the assertion is the band, not the point. An exact 4 here
    # would mean every pending delivery retried in lockstep, which is what the
    # jitter exists to prevent when an outage fails them all at once.
    assert 3.2 <= session.calls[2][1][2] <= 4.8
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
    destination = (
        Wreath()
        .webhooks("dispatch")
        .destination(
            "receiver",
            client=_FailingHTTPClient(),
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=outbox,
        )
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
async def test_dispatcher_without_a_renewal_factory_creates_no_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(rows=[_delivery_row()], values=[1, 1])
    outbox = PostgresWebhookOutbox()
    destination = (
        Wreath()
        .webhooks("no-lease-renewal")
        .destination(
            "receiver",
            client=_FakeHTTPClient(202),
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=outbox,
        )
    )
    dispatcher = WebhookDispatcher(
        outbox,
        {"receiver": destination},
        worker_id="worker-no-renew",
    )

    def unexpected_create_task(coroutine: Any, *, name: str) -> None:
        coroutine.close()
        raise AssertionError(f"unexpected renewal task {name}")

    monkeypatch.setattr(asyncio, "create_task", unexpected_create_task)
    result = await dispatcher.run_once(session)

    assert result is not None and result.outcome == "delivered"


@pytest.mark.asyncio
async def test_dispatcher_renews_lease_while_delivery_is_in_flight() -> None:
    primary = _FakeSession(rows=[_delivery_row()], values=[1, 1])
    renewal = _FakeSession(values=[1, 1, 1])
    client = _SlowHTTPClient()
    destination = (
        Wreath()
        .webhooks("lease-renewal")
        .destination(
            "receiver",
            client=client,
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=PostgresWebhookOutbox(),
        )
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
    destination = (
        Wreath()
        .webhooks("ack-loss")
        .destination(
            "receiver",
            client=client,
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=outbox,
        )
    )
    dispatcher = WebhookDispatcher(outbox, {"receiver": destination}, worker_id="worker-a")
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
    destination = (
        Wreath()
        .webhooks("persistence-loss")
        .destination(
            "receiver",
            client=client,
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=outbox,
        )
    )
    dispatcher = WebhookDispatcher(outbox, {"receiver": destination}, worker_id="worker-a")
    with pytest.raises(ConnectionError, match="claim lost"):
        await dispatcher.run_once(_FakeSession(rows=[ConnectionError("claim lost")]))
    with pytest.raises(RuntimeError, match="stale webhook outbox"):
        await dispatcher.run_once(_FakeSession(rows=[_delivery_row()], values=[None]))
    assert client.calls == []


@pytest.mark.asyncio
async def test_dispatcher_lifespan_management_exposes_readiness() -> None:
    app = Wreath()
    session = _FakeSession(rows=[None, None])

    @asynccontextmanager
    async def session_factory():
        yield session

    dispatcher = WebhookDispatcher(PostgresWebhookOutbox(), {}, worker_id="managed-worker")
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
    destination = (
        Wreath()
        .webhooks("dispatch")
        .destination(
            "receiver",
            client=_StoppingHTTPClient(stopping),
            path="/callbacks",
            signer=HMACWebhookSigner(KEYS, key_id="current"),
            outbox=outbox,
        )
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
    import inspect

    for owner in (PostgresWebhookInbox, PostgresWebhookOutbox):
        parameters = inspect.signature(owner.purge_pass).parameters
        assert "database" not in parameters, owner.__name__
        assert [
            name
            for name, p in parameters.items()
            if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        ] == ["self"], owner.__name__


# `wreath mutant` survived or never reached every guard in
# `WebhookEnvelope.__post_init__` and in the two HMAC classes' constructors: the
# suite builds well-formed envelopes with good keys, which is the right thing
# for it to do and the reason none of these had ever refused anything. Two of
# them defend the same signature-base ambiguity from opposite ends.


@pytest.mark.parametrize("field", ["id", "type", "version"])
def test_an_envelope_missing_an_identifying_field_is_refused(field: str) -> None:
    fields = {"id": "evt-1", "type": "widget.changed", "version": "1"}
    fields[field] = ""
    with pytest.raises(ValueError, match="id, type, and version are required"):
        WebhookEnvelope(
            timestamp=datetime.now(UTC),
            content_type="application/json",
            body=b"{}",
            **fields,
        )


@pytest.mark.parametrize("field", ["id", "type", "version"])
@pytest.mark.parametrize("char", ["\n", "\r", "\x00", "\x1f", "\x7f"])
def test_a_control_character_in_a_signed_field_is_refused(field: str, char: str) -> None:
    fields = {"id": "evt-1", "type": "widget.changed", "version": "1"}
    fields[field] = fields[field] + char
    with pytest.raises(ValueError, match=f"{field} contains a control character"):
        WebhookEnvelope(
            timestamp=datetime.now(UTC),
            content_type="application/json",
            body=b"{}",
            **fields,
        )


def test_the_verifier_refuses_a_control_character_before_it_computes_a_mac() -> None:
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
                body=envelope.body,
                headers=tampered,
                now=envelope.timestamp,
            )


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="must include a timezone"):
        WebhookEnvelope(
            id="evt-1",
            type="widget.changed",
            version="1",
            timestamp=datetime(2026, 7, 16, 10, 0),  # naive, which is the point
            content_type="application/json",
            body=b"{}",
        )


def test_a_relay_path_that_is_too_long_or_malformed_is_refused() -> None:
    common = {
        "id": "evt-1",
        "type": "widget.changed",
        "version": "1",
        "timestamp": datetime.now(UTC),
        "content_type": "application/json",
        "body": b"{}",
    }
    assert WebhookEnvelope(relay_path=tuple(f"h{n}" for n in range(32)), **common)
    with pytest.raises(ValueError, match="invalid or too long"):
        WebhookEnvelope(relay_path=tuple(f"h{n}" for n in range(33)), **common)
    for bad in ("has space", "has,comma", "", "has\nnewline"):
        with pytest.raises(ValueError, match="invalid or too long"):
            WebhookEnvelope(relay_path=("ok", bad), **common)


def test_a_relay_path_that_revisits_a_hop_is_refused_as_a_loop() -> None:
    with pytest.raises(ValueError, match="contains a loop"):
        WebhookEnvelope(
            id="evt-1",
            type="widget.changed",
            version="1",
            timestamp=datetime.now(UTC),
            content_type="application/json",
            body=b"{}",
            relay_path=("a", "b", "a"),
        )


def test_a_signer_asked_for_a_key_it_does_not_hold_refuses() -> None:
    signer = HMACWebhookSigner(KEYS, key_id="current")
    with pytest.raises(ValueError, match="signing key is unavailable"):
        signer.headers(_envelope(), key_id="retired")
    # The default is used when none is named, and it is the one it was built with.
    assert dict(signer.headers(_envelope()))[b"wreath-webhook-key-id"] == b"current"


def test_a_signer_refuses_missing_or_malformed_key_configuration() -> None:
    secret = KEYS["current"]
    with pytest.raises(ValueError, match="key id is not configured"):
        HMACWebhookSigner(KEYS, key_id="missing")
    for key_id in ("", 7):
        keys: Any = {"current": secret, key_id: secret}
        with pytest.raises(ValueError, match="key ids must be non-empty strings"):
            HMACWebhookSigner(keys, key_id="current")
    keys = {"current": secret, "bad": "not-bytes"}
    with pytest.raises(TypeError, match="must be bytes"):
        HMACWebhookSigner(keys, key_id="current")


@pytest.mark.parametrize("key_id", ["line\nbreak", "carriage\rreturn", "delete\x7f"])
def test_signer_refuses_a_key_id_that_cannot_be_a_header_value(key_id: str) -> None:
    with pytest.raises(ValueError, match="key id contains a control character"):
        HMACWebhookSigner({key_id: b"s" * 32}, key_id=key_id)


def test_a_verifier_with_no_usable_key_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="non-empty webhook verification key"):
        HMACWebhookVerifier({})
    with pytest.raises(ValueError, match="at least 32 bytes"):
        HMACWebhookVerifier({"current": b""})
    # One bad entry poisons the mapping, exactly as for origins elsewhere.
    with pytest.raises(ValueError, match="at least 32 bytes"):
        HMACWebhookVerifier({"current": KEYS["current"], "previous": b""})


def test_a_verifier_refuses_malformed_key_ids_and_secret_types() -> None:
    secret = KEYS["current"]
    for key_id in ("", 7):
        keys: Any = {"current": secret, key_id: secret}
        with pytest.raises(ValueError, match="key ids must be non-empty strings"):
            HMACWebhookVerifier(keys)
    keys = {"current": secret, "bad": "not-bytes"}
    with pytest.raises(TypeError, match="must be bytes"):
        HMACWebhookVerifier(keys)


@pytest.mark.parametrize("field", ["correlation_id", "causation_id"])
def test_an_envelope_refuses_control_characters_in_outbound_headers(field: str) -> None:
    values: dict[str, Any] = {field: "trusted\r\nx-forged: yes"}

    with pytest.raises(ValueError, match=f"webhook {field} contains a control character"):
        WebhookEnvelope(
            id="evt-1",
            type="widget.changed",
            version="1",
            timestamp=datetime.now(UTC),
            content_type="application/json",
            body=b"{}",
            **values,
        )


@pytest.mark.parametrize("field", ["correlation_id", "causation_id", "deduplication_id"])
def test_an_envelope_refuses_empty_optional_security_identifiers(field: str) -> None:
    with pytest.raises(ValueError, match=f"webhook {field} must not be empty"):
        WebhookEnvelope(
            id="evt-1",
            type="widget.changed",
            version="1",
            timestamp=datetime.now(UTC),
            content_type="application/json",
            body=b"{}",
            **{field: ""},
        )


@pytest.mark.parametrize(
    "content_type", ["application/json\r\nx-forged: yes", "text/☃", "text/plain\x7f"]
)
def test_an_envelope_refuses_an_unsafe_content_type_header(content_type: str) -> None:
    with pytest.raises(ValueError, match="webhook content_type must be a Latin-1 header value"):
        WebhookEnvelope(
            id="evt-1",
            type="widget.changed",
            version="1",
            timestamp=datetime.now(UTC),
            content_type=content_type,
            body=b"{}",
        )


def test_webhook_envelope_snapshots_mutable_body_and_relay_inputs() -> None:
    body = bytearray(b"trusted")
    relay_path = ["origin"]
    envelope = WebhookEnvelope(
        id="evt-1",
        type="widget.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=body,
        relay_path=relay_path,
    )

    body[:] = b"forged!"
    relay_path[0] = "attacker"

    assert envelope.body == b"trusted"
    assert envelope.relay_path == ("origin",)


def test_a_verifier_replay_window_that_can_never_hold_is_refused() -> None:
    with pytest.raises(ValueError, match="max_age must be positive"):
        HMACWebhookVerifier(KEYS, max_age=0)
    with pytest.raises(ValueError, match="max_age must be positive"):
        HMACWebhookVerifier(KEYS, max_age=-1)


def test_wreath_verifier_replay_window_is_immutable() -> None:
    verifier = HMACWebhookVerifier(KEYS)

    with pytest.raises(AttributeError):
        verifier.max_age = 3600


@pytest.mark.parametrize("window", [float("nan"), float("inf")])
def test_webhook_freshness_and_local_replay_windows_must_be_finite(window: float) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        HMACWebhookVerifier(KEYS, max_age=window)
    with pytest.raises(ValueError, match="positive and finite"):
        LocalReplayStore(max_entries=8, ttl=window)


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [float("nan"), float("inf")])
async def test_webhook_database_lease_windows_must_be_finite(window: float) -> None:
    session = _FakeSession()
    with pytest.raises(ValueError, match="positive and finite"):
        PostgresWebhookInbox(
            session_factory=lambda: None,
            lease_owner="worker",
            lease_seconds=window,
        )
    with pytest.raises(ValueError, match="positive and finite"):
        await PostgresWebhookInbox().claim(
            session,
            source="sender",
            envelope=_envelope(),
            lease_owner="worker",
            lease_seconds=window,
        )
    with pytest.raises(ValueError, match="positive and finite"):
        await PostgresWebhookOutbox().claim_due(
            session,
            lease_owner="worker",
            lease_seconds=window,
        )
    with pytest.raises(ValueError, match="positive and finite"):
        await PostgresWebhookOutbox().renew_lease(
            session,
            _outbox_delivery(_delivery_row()),
            lease_seconds=window,
        )
    with pytest.raises(ValueError, match="non-negative and finite"):
        await PostgresWebhookOutbox().mark_retry(
            session,
            _outbox_delivery(_delivery_row()),
            delay=window,
            status=None,
            failure="unavailable",
        )
    with pytest.raises(ValueError, match="positive and finite"):
        await WebhookDispatcher(PostgresWebhookOutbox(), {}, worker_id="worker").run(
            lambda: None, asyncio.Event(), idle_delay=window
        )
    assert session.calls == []


@pytest.mark.parametrize("window", [float("nan"), float("inf")])
def test_webhook_dispatcher_windows_must_be_finite(window: float) -> None:
    outbox = PostgresWebhookOutbox()
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="w", lease_seconds=window)
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="w", retry_delay=window, retry_cap=window)
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="w", retry_cap=window)
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="w", max_attempts=window)


@pytest.mark.parametrize("window", [True, "1"])
def test_webhook_time_windows_require_numeric_nonboolean_values(window: Any) -> None:
    builders = (
        lambda: HMACWebhookVerifier(KEYS, max_age=window),
        lambda: StandardWebhookVerifier(b"secret", max_age=window),
        lambda: StripeWebhookVerifier(b"secret", max_age=window),
        lambda: GitHubWebhookVerifier(b"secret", replay_ttl=window),
        lambda: WebhookDispatcher(
            PostgresWebhookOutbox(), {}, worker_id="worker", lease_seconds=window
        ),
    )
    for build in builders:
        with pytest.raises(ValueError, match="positive.*finite"):
            build()


@pytest.mark.parametrize("window", [True, "1"])
def test_dispatcher_retry_windows_require_numeric_nonboolean_values(window: Any) -> None:
    outbox = PostgresWebhookOutbox()
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="worker", retry_delay=window)
    with pytest.raises(ValueError, match="limits are invalid"):
        WebhookDispatcher(outbox, {}, worker_id="worker", retry_cap=window)


def test_dispatcher_snapshots_retry_statuses() -> None:
    statuses = {429, 503}
    dispatcher = WebhookDispatcher(
        PostgresWebhookOutbox(), {}, worker_id="worker", retry_statuses=statuses
    )

    statuses.clear()

    assert dispatcher._retry_statuses == frozenset({429, 503})


def test_a_dispatcher_with_impossible_limits_is_refused() -> None:
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


@pytest.mark.parametrize("worker_id", [True, 1, b"worker"])
def test_dispatcher_worker_id_must_be_a_nonempty_string(worker_id: Any) -> None:
    with pytest.raises(ValueError, match="worker_id.*non-empty string"):
        WebhookDispatcher(PostgresWebhookOutbox(), {}, worker_id=worker_id)
