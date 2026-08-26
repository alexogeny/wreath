"""Signed inbound and outbound webhook primitives.

A webhook is somebody else's HTTP request arriving with a claim about who sent
it, or one of yours leaving with the same claim attached. Both halves are here,
and both are built around the same signature profile.

**The profile.** Wreath signs `wreath-v1`: HMAC-SHA256 over the exact request
body joined with the event's timestamp, id and type. The MAC covers the bytes
that were sent, not a re-serialization of them, so a verifier that reformats the
JSON before checking would fail -- which is the point. Signed fields are refused
if they contain a control character, because the signature base joins them with
newlines and an embedded newline would make the split ambiguous. Verification
also bounds the timestamp (`HMACWebhookVerifier.max_age`), so a captured
request cannot be replayed forever. Keys are a mapping of key id to secret, and
the id travels in a header, which is what makes rotation an ordinary deployment
rather than a cutover.

**Receiving.** `WebhookHub` registers a `WebhookSource` per sender
and a handler per event type. A request is bounded, verified, deduplicated and
dispatched, in that order. Deduplication has two forms and they are not
equivalent: `LocalReplayStore` is per process, and
`PostgresWebhookInbox` is a claim in a table every replica shares, with a
fencing token so a lease that expired cannot complete over the top of the worker
that took it over.

**Sending.** `WebhookDestination` signs and posts. `send` does it
now, and reports the outcome as `delivered`, `failed` or `unknown` --
three states because a transport failure genuinely does not say whether the peer
processed the request. `enqueue` instead commits the intent to
`PostgresWebhookOutbox` inside the caller's transaction, so the delivery
exists exactly when the change that caused it does, and
`WebhookDispatcher` sends it afterwards with leases, retries and backoff.

**Relaying.** An event received can cause an event sent. `relay` and
`enqueue_relay` carry correlation and causation ids forward and append this
service to the envelope's relay path, which is itself signed. A path that would
repeat a service, or grow past the hop limit, is refused -- that is what stops
two services that subscribe to each other from generating traffic forever.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from ._capability_map import CapabilityMap
from ._jobcore import compute_backoff
from ._json import dumps, loads
from ._leased import claim_sql
from ._pgname import validate_unquoted_identifier
from .binding import _body_validator
from .http_client import ClientError
from .request import Request
from .response import Response

_HEADER_ID = b"wreath-webhook-id"
_HEADER_TYPE = b"wreath-webhook-type"
_HEADER_VERSION = b"wreath-webhook-version"
_HEADER_TIMESTAMP = b"wreath-webhook-timestamp"
_HEADER_KEY_ID = b"wreath-webhook-key-id"
_HEADER_SIGNATURE = b"wreath-webhook-signature"
_HEADER_CORRELATION = b"wreath-correlation-id"
_HEADER_CAUSATION = b"wreath-causation-id"
_HEADER_RELAY_PATH = b"wreath-webhook-relay-path"
_RELAY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _schema_component(name: str, table: str, statements: tuple[str, ...]) -> Any:
    """Build the one schema-claim shape shared by inbox and outbox."""
    from .schema import Component, Step

    return Component(
        name=name,
        schema="",
        relations=(table,),
        steps=(Step(version=1, statements=statements),),
    )


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    """One event, in the form the signature covers. Immutable; validated at construction.

    Both directions use it: a signer turns one into headers, and a verifier
    returns one after the MAC checks out. Holding the raw `body` rather than a
    decoded payload is deliberate -- the signature is over those exact bytes, and
    re-encoding them would break it.

    Construction refuses anything that would make the signature base ambiguous or
    the relay path unusable, so an envelope that exists is one that can be signed.

    Args:
        id: Unique per event, and the deduplication key on the receiving side.
        type: Event type; selects the handler on the receiving side.
        version: Payload schema version, carried through and signed.
        timestamp: Must be timezone-aware. Bounds replay via the verifier's window.
        content_type: Sent as `Content-Type`; not covered by the signature.
        body: The exact bytes signed and sent.
        correlation_id: Ties an event to the chain it belongs to.
        causation_id: The id of the event that caused this one.
        ordering_key: Advisory grouping stored on an outbox row. Not signed, not enforced.
        relay_path: Services this event has already passed through. Signed, and loop-checked.

    Raises:
        ValueError: An empty or control-character field, a naive timestamp, or a bad relay path.
    """

    id: str
    type: str
    version: str
    timestamp: datetime
    content_type: str
    body: bytes
    correlation_id: str | None = None
    causation_id: str | None = None
    ordering_key: str | None = None
    relay_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.type or not self.version:
            raise ValueError("webhook id, type, and version are required")
        # The signature base joins these with newlines, so a newline inside one
        # of them lets a single MAC cover more than one (timestamp, id, type,
        # body) split -- the fields stop being unambiguously recoverable from
        # what was signed. Refused here rather than escaped, because no real
        # event id or type contains a control character.
        for name, value in (("id", self.id), ("type", self.type), ("version", self.version)):
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
                raise ValueError(f"webhook {name} contains a control character")
        if self.timestamp.tzinfo is None:
            raise ValueError("webhook timestamp must include a timezone")
        if len(self.relay_path) > 32 or any(
            not _RELAY_ID.fullmatch(item) for item in self.relay_path
        ):
            raise ValueError("webhook relay path is invalid or too long")
        if len(set(self.relay_path)) != len(self.relay_path):
            raise ValueError("webhook relay path contains a loop")


@dataclass(frozen=True, slots=True)
class WebhookLimits:
    """What one inbound webhook request may cost. Validated at construction.

    Applied before the signature is checked, and that order matters: verifying a
    MAC over a body an unauthenticated caller controls the size of is work the
    caller chose for you. Every breach answers `413` -- except an oversized
    event id, which answers `400`, because it is a valid signed request that
    this receiver will not store.

    Args:
        max_body_bytes: Body ceiling, checked once the body is read and before the MAC.
        max_headers: Header fields accepted; over it the request is refused unread.
        max_header_bytes: Total header name+value bytes, accumulated as they are scanned.
        max_event_id_bytes: UTF-8 bytes in the event id, checked after verification.

    Raises:
        ValueError: Any limit is non-positive.
    """

    max_body_bytes: int = 1024 * 1024
    max_headers: int = 32
    max_header_bytes: int = 16 * 1024
    max_event_id_bytes: int = 256

    def __post_init__(self) -> None:
        if (
            min(
                self.max_body_bytes,
                self.max_headers,
                self.max_header_bytes,
                self.max_event_id_bytes,
            )
            <= 0
        ):
            raise ValueError("webhook limits must be positive")


@dataclass(frozen=True, slots=True)
class WebhookContext:
    """What a receiving handler is told about the event it is handling.

    The second argument to a handler is the validated payload; this is the
    first, and carries everything around it.

    `session` is the distinction that matters. On a durable source it is the
    same transaction the inbox claim was made in, so anything the handler writes
    commits atomically with "this event was processed" -- there is no window in
    which the work landed but the deduplication did not. On a non-durable source
    it is None and the handler must open its own.

    Args:
        source: The registered source name; also the deduplication namespace.
        request: The live request, for headers the envelope does not carry.
        session: The claim's transaction on a durable source, else None.
    """

    source: str
    envelope: WebhookEnvelope
    request: Request
    session: Any | None = None


@dataclass(frozen=True, slots=True)
class WebhookDeliveryResult:
    """The outcome of one delivery attempt.

    Three outcomes, not two, because a transport failure is genuinely not a
    failed delivery: a request that timed out may well have been processed. Only
    `failed` means the peer answered and refused; `unknown` means nobody
    knows, which is why the dispatcher settles those rows separately instead of
    retrying them into a duplicate.

    Args:
        outcome: `delivered` (2xx), `failed` (a non-2xx answer), or `unknown` (no answer).
        event_id: The envelope id this attempt was for.
        status: The response status; None when no response arrived.
        failure: A short failure code, e.g. a client exception name; None when the peer answered.
    """

    outcome: Literal["delivered", "failed", "unknown"]
    event_id: str
    status: int | None = None
    failure: str | None = None


class HMACWebhookSigner:
    """Sign Wreath's versioned exact-body HMAC-SHA256 profile.

    Holds every key it may ever have to sign with, and one id to sign with by
    default. The others are not spares: a redelivery from the outbox must be
    signed with the key the row was enqueued under, or a receiver that has since
    rotated would reject a delivery it had already accepted. That is why
    `headers` takes a `key_id` at all.

    Keys are copied at construction, so the caller's mapping may change
    afterwards without affecting an in-flight signer.

    Args:
        keys: Key id to secret. Must contain `key_id`, and that secret must be non-empty.
        key_id: The key used when a caller does not name one.

    Raises:
        ValueError: `key_id` is not in `keys`, or its secret is empty.
    """

    __slots__ = ("_key_id", "_keys")

    def __init__(self, keys: Mapping[str, bytes], *, key_id: str) -> None:
        if key_id not in keys:
            raise ValueError("webhook signing key id is not configured")
        if not keys[key_id]:
            raise ValueError("webhook signing key cannot be empty")
        self._keys = dict(keys)
        self._key_id = key_id

    @property
    def key_id(self) -> str:
        """The default signing key id. Recorded on an outbox row at enqueue time."""
        return self._key_id

    def headers(
        self, envelope: WebhookEnvelope, *, key_id: str | None = None
    ) -> tuple[tuple[bytes, bytes], ...]:
        """The signed header set for `envelope`. Does not include `Content-Type`.

        Always carries the id, type, version, timestamp, key id and signature;
        correlation, causation and relay-path headers appear only when the
        envelope has them. The timestamp is normalized to UTC with microsecond
        precision and a `Z` suffix, and it is the normalized form that is
        signed -- so the header must be transmitted byte-for-byte as returned.

        Args:
            key_id: Sign with this key instead of the default, e.g. redelivering an old row.

        Returns:
            Raw `(name, value)` byte pairs, ready to pass to the HTTP client.

        Raises:
            ValueError: `key_id` names a key this signer does not hold.
        """
        selected_key = self._key_id if key_id is None else key_id
        key = self._keys.get(selected_key)
        if key is None:
            raise ValueError("recorded webhook signing key is unavailable")
        timestamp = _format_timestamp(envelope.timestamp)
        signature = hmac.new(
            key,
            _signature_base(
                timestamp,
                envelope.id,
                envelope.type,
                envelope.body,
                envelope.relay_path,
            ),
            hashlib.sha256,
        ).hexdigest()
        headers: list[tuple[bytes, bytes]] = [
            (_HEADER_ID, envelope.id.encode("utf-8")),
            (_HEADER_TYPE, envelope.type.encode("utf-8")),
            (_HEADER_VERSION, envelope.version.encode("utf-8")),
            (_HEADER_TIMESTAMP, timestamp),
            (_HEADER_KEY_ID, selected_key.encode("utf-8")),
            (_HEADER_SIGNATURE, f"v1={signature}".encode("ascii")),
        ]
        if envelope.correlation_id is not None:
            headers.append((_HEADER_CORRELATION, envelope.correlation_id.encode("utf-8")))
        if envelope.causation_id is not None:
            headers.append((_HEADER_CAUSATION, envelope.causation_id.encode("utf-8")))
        if envelope.relay_path:
            headers.append((_HEADER_RELAY_PATH, ",".join(envelope.relay_path).encode("ascii")))
        return tuple(headers)


@runtime_checkable
class WebhookVerifier(Protocol):
    """A provider signature profile consumed by `WebhookSource`.

    The hub has already lower-cased and bounded the headers before calling the
    verifier. A verifier returns a complete trusted envelope or
    raises `ValueError`; boolean and partially verified results are not part of
    the contract. `max_age` also sizes the default in-process replay ledger.
    """

    max_age: float

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[bytes, bytes],
        now: datetime | None = None,
    ) -> WebhookEnvelope: ...


class _NormalizedWebhookVerifier:
    """One public verifier entry point; profiles implement normalized checks."""

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[bytes, bytes],
        now: datetime | None = None,
    ) -> WebhookEnvelope:
        normalized = {name.lower(): value for name, value in headers.items()}
        return self._verify_normalized(body=body, headers=normalized, now=now)

    def _verify_normalized(
        self,
        *,
        body: bytes,
        headers: Mapping[bytes, bytes],
        now: datetime | None = None,
    ) -> WebhookEnvelope:
        raise NotImplementedError


class HMACWebhookVerifier(_NormalizedWebhookVerifier):
    """Verify Wreath's HMAC profile with a bounded timestamp window.

    Holding several keys is how rotation works: a sender switches key id when it
    is ready, and both are accepted until the old one is dropped. The key id
    arrives in a header and selects the secret, so an unknown id is rejected
    before any MAC is computed.

    `max_age` bounds replay in both directions -- a request whose timestamp is
    too far in the *future* is refused too, so a clock-skewed or forged forward
    timestamp cannot buy an attacker an arbitrarily long window. It does not
    stop a replay inside the window; that is what a replay store or the inbox is
    for.

    Args:
        keys: Key id to secret. At least one, and none may be empty.
        max_age: Seconds a timestamp may differ from now, either way.

    Raises:
        ValueError: `keys` is empty or holds an empty secret, or `max_age` is non-positive.
    """

    __slots__ = ("_keys", "max_age")

    def __init__(self, keys: Mapping[str, bytes], *, max_age: float = 300.0) -> None:
        if not keys or any(not value for value in keys.values()):
            raise ValueError("at least one non-empty webhook verification key is required")
        if max_age <= 0:
            raise ValueError("webhook max_age must be positive")
        self._keys = dict(keys)
        self.max_age = max_age

    def _verify_normalized(
        self,
        *,
        body: bytes,
        headers: Mapping[bytes, bytes],
        now: datetime | None = None,
    ) -> WebhookEnvelope:
        event_id = _required_header(headers, _HEADER_ID)
        event_type = _required_header(headers, _HEADER_TYPE)
        version = _required_header(headers, _HEADER_VERSION)
        timestamp_data = _required_header(headers, _HEADER_TIMESTAMP)
        key_id_data = _required_header(headers, _HEADER_KEY_ID)
        supplied = _required_header(headers, _HEADER_SIGNATURE)
        relay_path = _parse_relay_path(headers.get(_HEADER_RELAY_PATH))
        event_id_text = event_id.decode("utf-8")
        event_type_text = event_type.decode("utf-8")
        version_text = version.decode("utf-8")
        # Checked before the MAC is computed, for the reason in
        # `WebhookEnvelope.__post_init__`: a framing character in a signed field
        # makes the split ambiguous, so it must not reach `_signature_base`.
        for name, value in (
            ("id", event_id_text),
            ("type", event_type_text),
            ("version", version_text),
        ):
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
                raise ValueError(f"webhook {name} contains a control character")
        try:
            key_id = key_id_data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("invalid webhook key id") from error
        key = self._keys.get(key_id)
        if key is None:
            raise ValueError("unknown webhook key id")
        timestamp = _parse_timestamp(timestamp_data)
        current = datetime.now(UTC) if now is None else now.astimezone(UTC)
        if abs((current - timestamp).total_seconds()) > self.max_age:
            raise ValueError("webhook timestamp is outside the accepted window")
        expected = b"v1=" + hmac.new(
            key,
            _signature_base(
                timestamp_data,
                event_id_text,
                event_type_text,
                body,
                relay_path,
            ),
            hashlib.sha256,
        ).hexdigest().encode("ascii")
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid webhook signature")
        content_type = headers.get(b"content-type", b"application/json").decode("latin-1")
        return WebhookEnvelope(
            id=event_id_text,
            type=event_type_text,
            version=version_text,
            timestamp=timestamp,
            content_type=content_type,
            body=body,
            correlation_id=_optional_text(headers, _HEADER_CORRELATION),
            causation_id=_optional_text(headers, _HEADER_CAUSATION),
            relay_path=relay_path,
        )


def _provider_event(body: bytes) -> tuple[str, str, str]:
    try:
        value = loads(body)
    except ValueError as exc:
        raise ValueError("verified webhook body is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("verified webhook body must be a JSON object")
    event_id = value.get("id")
    event_type = value.get("type")
    version = value.get("api_version", value.get("version", "1"))
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("verified webhook body has no string id")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("verified webhook body has no string type")
    if not isinstance(version, str):
        version = str(version)
    return event_id, event_type, version


def _unix_timestamp(value: bytes, now: datetime | None, max_age: float) -> datetime:
    try:
        seconds = int(value.decode("ascii"))
        timestamp = datetime.fromtimestamp(seconds, UTC)
    except (UnicodeDecodeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("invalid webhook Unix timestamp") from exc
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    if abs((current - timestamp).total_seconds()) > max_age:
        raise ValueError("webhook timestamp is outside the accepted window")
    return timestamp


def _constant_time_signature_match(expected: Iterable[bytes], supplied: Iterable[bytes]) -> bool:
    """Match two signature collections without their Cartesian product.

    SHA-256 is only an index: a hit is still authenticated with
    ``compare_digest``. Buckets preserve correctness even under a digest
    collision, while ordinary distinct signatures cost one fixed-width hash
    and one dictionary operation apiece.
    """
    indexed: dict[bytes, list[bytes]] = {}
    for candidate in supplied:
        indexed.setdefault(hashlib.sha256(candidate).digest(), []).append(candidate)
    for wanted in expected:
        for candidate in indexed.get(hashlib.sha256(wanted).digest(), ()):
            if hmac.compare_digest(wanted, candidate):
                return True
    return False


class StandardWebhookVerifier(_NormalizedWebhookVerifier):
    """Verify the Standard Webhooks HMAC-SHA256 profile.

    `whsec_` secrets are decoded as their standard base64 payload; raw bytes
    are accepted for secret stores that already decoded them. Multiple secrets
    permit rotation, and multiple `v1` signatures in the header are checked.
    Event type/version come from the verified JSON body because the standard
    header set carries only id, timestamp, and signature.
    """

    __slots__ = ("_secrets", "max_age")

    def __init__(
        self, secrets: bytes | str | tuple[bytes | str, ...], *, max_age: float = 300.0
    ) -> None:
        supplied = secrets if isinstance(secrets, tuple) else (secrets,)
        if not supplied:
            raise ValueError("at least one non-empty Standard Webhooks secret is required")
        decoded: list[bytes] = []
        for secret in supplied:
            if isinstance(secret, str):
                token = secret.removeprefix("whsec_")
                try:
                    value = base64.b64decode(token, validate=True)
                except ValueError as exc:
                    raise ValueError("Standard Webhooks secret is not base64") from exc
            else:
                value = bytes(secret)
            if not value:
                raise ValueError("Standard Webhooks secret cannot be empty")
            decoded.append(value)
        if max_age <= 0:
            raise ValueError("webhook max_age must be positive")
        self._secrets = tuple(decoded)
        self.max_age = max_age

    def _verify_normalized(
        self, *, body: bytes, headers: Mapping[bytes, bytes], now: datetime | None = None
    ) -> WebhookEnvelope:
        event_id_data = _required_header(headers, b"webhook-id")
        timestamp_data = _required_header(headers, b"webhook-timestamp")
        signatures = _required_header(headers, b"webhook-signature").split()
        timestamp = _unix_timestamp(timestamp_data, now, self.max_age)
        signed = event_id_data + b"." + timestamp_data + b"." + body
        supplied: list[bytes] = []
        for item in signatures:
            if not item.startswith(b"v1,"):
                continue
            try:
                supplied.append(base64.b64decode(item[3:], validate=True))
            except ValueError as exc:
                raise ValueError("invalid Standard Webhooks signature") from exc
        if not supplied or not _constant_time_signature_match(
            (hmac.digest(secret, signed, "sha256") for secret in self._secrets),
            supplied,
        ):
            raise ValueError("invalid Standard Webhooks signature")
        event_id, event_type, version = _provider_event(body)
        if event_id.encode("utf-8") != event_id_data:
            raise ValueError("Standard Webhooks body id differs from webhook-id")
        return WebhookEnvelope(
            event_id,
            event_type,
            version,
            timestamp,
            headers.get(b"content-type", b"application/json").decode("latin-1"),
            body,
        )


class StripeWebhookVerifier(_NormalizedWebhookVerifier):
    """Verify Stripe's `Stripe-Signature` `t=...,v1=...` profile."""

    __slots__ = ("_secrets", "max_age")

    def __init__(
        self,
        secrets: bytes | str | tuple[bytes | str, ...],
        *,
        max_age: float = 300.0,
    ) -> None:
        supplied = secrets if isinstance(secrets, tuple) else (secrets,)
        if not supplied or any(not secret for secret in supplied):
            raise ValueError("at least one non-empty Stripe webhook secret is required")
        if max_age <= 0:
            raise ValueError("webhook max_age must be positive")
        self._secrets = tuple(
            secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
            for secret in supplied
        )
        self.max_age = max_age

    def _verify_normalized(
        self, *, body: bytes, headers: Mapping[bytes, bytes], now: datetime | None = None
    ) -> WebhookEnvelope:
        parts: dict[bytes, list[bytes]] = {}
        for item in _required_header(headers, b"stripe-signature").split(b","):
            key, separator, value = item.strip().partition(b"=")
            if not separator:
                raise ValueError("invalid Stripe-Signature field")
            parts.setdefault(key, []).append(value)
        timestamps = parts.get(b"t", [])
        signatures = parts.get(b"v1", [])
        if len(timestamps) != 1 or not signatures:
            raise ValueError("Stripe-Signature needs one t and at least one v1")
        timestamp = _unix_timestamp(timestamps[0], now, self.max_age)
        signed = timestamps[0] + b"." + body
        expected = (
            hmac.new(secret, signed, hashlib.sha256).hexdigest().encode("ascii")
            for secret in self._secrets
        )
        if not _constant_time_signature_match(expected, signatures):
            raise ValueError("invalid Stripe webhook signature")
        event_id, event_type, version = _provider_event(body)
        return WebhookEnvelope(
            event_id,
            event_type,
            version,
            timestamp,
            headers.get(b"content-type", b"application/json").decode("latin-1"),
            body,
        )


class GitHubWebhookVerifier(_NormalizedWebhookVerifier):
    """Verify GitHub's SHA-256 webhook signature and delivery identity.

    GitHub signs no timestamp, so freshness comes from the replay ledger keyed by
    `X-GitHub-Delivery`. `max_age` is therefore the retention time for that
    ledger rather than a signature check.
    """

    __slots__ = ("_secret", "max_age")

    def __init__(self, secret: bytes | str, *, replay_ttl: float = 86_400.0) -> None:
        if not secret:
            raise ValueError("GitHub webhook secret cannot be empty")
        if replay_ttl <= 0:
            raise ValueError("GitHub webhook replay_ttl must be positive")
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        self.max_age = replay_ttl

    def _verify_normalized(
        self, *, body: bytes, headers: Mapping[bytes, bytes], now: datetime | None = None
    ) -> WebhookEnvelope:
        supplied = _required_header(headers, b"x-hub-signature-256")
        expected = b"sha256=" + hmac.new(self._secret, body, hashlib.sha256).hexdigest().encode(
            "ascii"
        )
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid GitHub webhook signature")
        try:
            event_id = _required_header(headers, b"x-github-delivery").decode("ascii")
            event_type = _required_header(headers, b"x-github-event").decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid GitHub webhook identity header") from exc
        timestamp = datetime.now(UTC) if now is None else now.astimezone(UTC)
        return WebhookEnvelope(
            event_id,
            event_type,
            "1",
            timestamp,
            headers.get(b"content-type", b"application/json").decode("latin-1"),
            body,
        )


def _format_timestamp(value: datetime) -> bytes:
    utc = value.astimezone(UTC)
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z").encode("ascii")


def _parse_timestamp(value: bytes) -> datetime:
    try:
        text = value.decode("ascii")
        suffix = "+00:00" if text.endswith("Z") else ""
        parsed = datetime.fromisoformat(text.removesuffix("Z") + suffix)
    except ValueError as error:
        raise ValueError("invalid webhook timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("webhook timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _signature_base(
    timestamp: bytes,
    event_id: str,
    event_type: str,
    body: bytes,
    relay_path: tuple[str, ...] = (),
) -> bytes:
    if relay_path:
        return b"\n".join(
            (
                b"wreath-v1-relay",
                timestamp,
                event_id.encode("utf-8"),
                event_type.encode("utf-8"),
                ",".join(relay_path).encode("ascii"),
                body,
            )
        )
    return b"\n".join(
        (
            b"wreath-v1",
            timestamp,
            event_id.encode("utf-8"),
            event_type.encode("utf-8"),
            body,
        )
    )


def _parse_relay_path(value: bytes | None) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        path = tuple(value.decode("ascii").split(","))
    except UnicodeDecodeError as error:
        raise ValueError("invalid webhook relay path") from error
    if not path or len(path) > 32 or any(not _RELAY_ID.fullmatch(item) for item in path):
        raise ValueError("invalid webhook relay path")
    if len(set(path)) != len(path):
        raise ValueError("webhook relay path contains a loop")
    return path


def _required_header(headers: Mapping[bytes, bytes], name: bytes) -> bytes:
    value = headers.get(name)
    if value is None or not value:
        raise ValueError(f"missing webhook header {name.decode('ascii')}")
    return value


def _optional_text(headers: Mapping[bytes, bytes], name: bytes) -> str | None:
    value = headers.get(name)
    return None if value is None else value.decode("utf-8")


class LocalReplayStore:
    """Bounded webhook replay protection **in one process**.

    Enough for a single worker. Behind more than one it is a fast path rather
    than the guarantee: each worker has its own view, so the same event
    delivered twice to two workers is claimed twice and handled twice. Use
    `PostgresWebhookInbox` when the deduplication has to hold across
    replicas -- it is the same claim in a table every worker shares.

    Doubly bounded, because a replay store that grows without limit is a memory
    leak an unauthenticated sender controls: entries expire after `ttl`, and
    the oldest is evicted once `max_entries` is reached. Eviction means an
    event *can* be re-accepted before its TTL under sustained load. Size the
    store for the burst you expect rather than treating the bound as advisory.

    `ttl` should be at least the verifier's `max_age`: a request older than
    that window is rejected on the signature anyway, so a shorter TTL only
    creates a gap in which a replay is neither too old nor remembered.

    Args:
        max_entries: Claims retained. The oldest is evicted when the store is full.
        ttl: Seconds a claim is remembered, on the monotonic clock.

    Raises:
        ValueError: Either bound is non-positive.
    """

    __slots__ = ("_last_now", "_lock", "_table", "max_entries", "ttl")

    def __init__(self, *, max_entries: int, ttl: float) -> None:
        if max_entries <= 0 or ttl <= 0:
            raise ValueError("replay store bounds must be positive")
        self.max_entries = max_entries
        self.ttl = ttl
        self._last_now = time.monotonic()
        self._table = CapabilityMap(
            max_entries=max_entries,
            ttl=ttl,
            clock=time.monotonic,
            overflow="earliest",
        )
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        """Live claims held right now, expired ones included until they are swept."""
        return len(self._table)

    async def claim(self, source: str, event_id: str, *, now: float | None = None) -> bool:
        """Claim `(source, event_id)`, returning whether this caller won it.

        The whole check-and-insert happens under one lock, so two concurrent
        deliveries of the same event cannot both be told they won. Expiry and
        eviction run here, which is why the store needs no sweeper task.

        Args:
            source: Namespace, so two senders may use the same event id.
            now: Monotonic reference time. Defaults to `time.monotonic()`.

        Returns:
            True if this caller claimed it; False if it was already claimed.
        """
        current = time.monotonic() if now is None else now
        self._last_now = current
        key = (source, event_id)
        async with self._lock:
            return self._table.claim(key, "claimed", now=current)

    async def complete(self, source: str, event_id: str, outcome: str) -> None:
        """Record how a claimed event turned out. Does not extend or release the claim.

        The claim keeps its original expiry either way, so a handler that failed
        does *not* make the event redeliverable -- a retry from the sender is
        still refused until the TTL passes. Recording the outcome is for
        observability, not for control flow. A claim that has already expired is
        silently not updated.

        Args:
            outcome: A free-form label; the receiver writes `"completed"` or `"failed"`.
        """
        key = (source, event_id)
        async with self._lock:
            self._table.complete(key, outcome, now=self._last_now)


#: A delivery nobody is waiting on any more. Written once because the bounded
#: purge and the chunked purge pass must agree about it exactly: a row that is
#: still going to be retried is not rubbish.
_SETTLED_STATES = "state IN ('delivered','failed','cancelled','unknown')"


def _retention_purge_pass(
    *,
    table: str,
    key: tuple[str, ...],
    chunk: int = 1000,
    where: str | None = None,
    within: Any = "5s",
    shift: Any = "10s",
    pace: Any = None,
    schema: str = "wreath",
) -> Any:
    """The chunked pass behind the inbox's and the outbox's retention purge.

    Both walk `(retention_until, *primary key)`: the retention stamp because
    that is the ordered domain the frontier lives in, and the key appended
    because two rows can share a stamp and a boundary that is not unique either
    skips its siblings or loops on them.

    *key* is every column of the table's primary key, in order -- one for the
    outbox (`delivery_id`), two for the inbox (`source`, `message_id`). Passing
    only the last of a composite key is the failure this signature exists to
    make unspellable: the inbox once walked `(retention_until, message_id)` and
    declared `message_id` unique, which it is not -- one inbox serves every
    source on the hub, so two senders using the same event id put two rows on
    one boundary. Measured against PostgreSQL with six rows sharing a retention
    stamp: at `chunk=1` the walk visited three of them and silently left the
    other three in the table forever.
    """
    from .passes import ChunkedPass, Key, Purge, Rows, Sealed, Table

    if not key:
        raise ValueError("a retention purge needs at least one primary-key column")
    # `unique=True` on the last one: the walk's boundary is the whole tuple, and
    # it is the whole tuple that identifies a row.
    tiebreakers = tuple(
        Key(name, "text", unique=(index == len(key) - 1)) for index, name in enumerate(key)
    )
    return ChunkedPass(
        f"purge_{table}",
        over=Table(table),
        units=Rows(
            key=(
                Key("retention_until", "timestamptz", indexed=True),
                *tiebreakers,
            ),
            limit=chunk,
            within=within,
        ),
        frontier=Sealed(),
        work=Purge(where=where),
        pace=pace,
        # Stated rather than inherited. `halt` is the right default for a pass
        # with something irreversible at the end of it, because nothing should
        # be skipped by omission -- but a retention purge has no terminal step,
        # so there is no irreversible thing a skip could buy. One undeletable
        # row must not stop the inbox from being kept small forever. The hole is
        # still recorded and `wreath passes retry` still comes back for it; this
        # is the same call `keyed_purge_pass` makes for the three keyed stores,
        # and it is written out here because these two build their pass directly.
        on_chunk_failure="skip",
        shift=shift,
        schema=schema,
    )


@dataclass(frozen=True, slots=True)
class InboxClaim:
    """The result of trying to claim an inbound event in the shared inbox.

    Four outcomes, and only one of them means "run the handler":

    * `claimed` -- this worker owns the event; process it and complete it.
    * `duplicate` -- it was already processed; replay `result_status`.
    * `active` -- another worker holds an unexpired lease; answer 409 and
      let the sender retry rather than processing it twice.
    * `failed` -- a previous attempt recorded a failure and the row is not
      reclaimable; a human decides what happens next.

    Args:
        fencing_token: Rises on every claim; `PostgresWebhookInbox.complete` refuses a stale one.
        result_status: The status a completed attempt returned, when one is recorded.
    """

    outcome: Literal["claimed", "duplicate", "active", "failed"]
    fencing_token: int
    result_status: int | None = None


class PostgresWebhookInbox:
    """Transactional cross-replica webhook deduplication and fencing.

    A row per `(source, message_id)` in a table every replica shares, so
    "already handled" is a fact in the database rather than a fact in one
    process's memory. Claiming, handling and completing happen in the caller's
    transaction, which is what makes them atomic with whatever the handler
    writes: an event cannot be marked processed unless its effects committed,
    and its effects cannot commit unless it was marked processed.

    A crashed worker's lease expires and the event becomes claimable again, with
    the fencing token incremented. The old worker coming back cannot then
    complete the row over the top of its successor -- `complete` matches
    on the token and raises instead.

    Args:
        table: Table name. Must be a plain SQL identifier; it is interpolated, not bound.

    Raises:
        ValueError: `table` is not a plain SQL identifier.
    """

    __slots__ = ("table",)

    def __init__(self, table: str = "wreath_webhook_inbox") -> None:
        self.table = validate_unquoted_identifier(table, "webhook inbox table")

    def statements(self) -> tuple[str, ...]:
        """DDL creating the inbox table and its retention index. Idempotent.

        `IF NOT EXISTS` throughout, so it is safe to run at every boot -- which
        is what wreath now does, during lifespan, before the receiver starts.

        The table name is **unqualified**, so it lands in whatever `search_path`
        resolves to rather than in the `wreath` schema. That is where every
        existing deployment's rows already are, and moving them would not be
        additive. See `wreath.schema.Component.qualified`.

        Returns:
            One statement per element, in order.
        """
        table = self.table
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            "    source text NOT NULL,\n"
            "    message_id text NOT NULL,\n"
            "    payload_version text NOT NULL,\n"
            "    payload_hash bytea NOT NULL,\n"
            "    state text NOT NULL CHECK (state IN "
            "('processing','completed','failed')),\n"
            "    lease_owner text NOT NULL,\n"
            "    lease_expires_at timestamptz NOT NULL,\n"
            "    fencing_token bigint NOT NULL DEFAULT 1,\n"
            "    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
            "    completed_at timestamptz,\n"
            "    result_status integer,\n"
            "    failure_code text,\n"
            "    failure_summary text,\n"
            "    retention_until timestamptz,\n"
            "    PRIMARY KEY (source, message_id)\n"
            ")",
            # Leads with the retention stamp and carries the whole primary key,
            # because that tuple is the purge pass's boundary -- a stamp-only
            # index leaves the walk's tiebreaker columns off the scan. Deployed
            # before this shape, `{table}_retention_idx` is redundant with it
            # and can be dropped; `IF NOT EXISTS` will not replace it in place,
            # which is why this one has its own name.
            f"CREATE INDEX IF NOT EXISTS {table}_retention_walk_idx ON {table} "
            "(retention_until, source, message_id) "
            "WHERE retention_until IS NOT NULL",
        )

    def component(self) -> Any:
        """The inbox's claim on the wreath schema."""
        return _schema_component("webhook-inbox", self.table, self.statements())

    def schema_sql(self) -> str:
        """The inbox DDL, semicolon-joined. A derivation of `component()`."""
        return self.component().sql()

    async def claim(
        self,
        session: Any,
        *,
        source: str,
        envelope: WebhookEnvelope,
        lease_owner: str,
        lease_seconds: float,
    ) -> InboxClaim:
        """Claim `envelope` for processing, or report why it cannot be claimed.

        One statement does the whole decision: an insert that, on conflict, takes
        the row over only when the previous lease has actually expired. There is
        no read-then-write, so two replicas racing on the same event cannot both
        be told they claimed it -- the loser reads the existing row and gets
        `active`.

        Runs in the caller's transaction and commits with it. A handler that
        raises therefore rolls the claim back too, leaving the event
        redeliverable, which is the behaviour you want when the failure was
        transient.

        Args:
            session: An open session inside a transaction. Not committed here.
            lease_owner: Identifies the claiming worker; recorded on the row.
            lease_seconds: How long the claim is exclusive before another worker may take it.

        Returns:
            An `InboxClaim`; only `claimed` authorises running the handler.

        Raises:
            ValueError: `lease_seconds` is not positive.
            RuntimeError: The row vanished mid-transaction, which should not be reachable.
        """
        if lease_seconds <= 0:
            raise ValueError("webhook inbox lease_seconds must be positive")
        payload_hash = hashlib.sha256(envelope.body).digest()
        sql = (
            f"INSERT INTO {self.table} AS i "
            "(source, message_id, payload_version, payload_hash, state, "
            "lease_owner, lease_expires_at) "
            "VALUES ($1,$2,$3,$4,'processing',$5,"
            "clock_timestamp() + $6::float8 * interval '1 second') "
            "ON CONFLICT (source, message_id) DO UPDATE SET "
            "state='processing', lease_owner=EXCLUDED.lease_owner, "
            "lease_expires_at=EXCLUDED.lease_expires_at, "
            "fencing_token=i.fencing_token + 1 "
            "WHERE i.state='processing' AND i.lease_expires_at < clock_timestamp() "
            "RETURNING fencing_token"
        )
        row = await session.raw(
            sql,
            source,
            envelope.id,
            envelope.version,
            payload_hash,
            lease_owner,
            lease_seconds,
        ).fetchrow()
        if row is not None:
            return InboxClaim("claimed", int(_row_value(row, "fencing_token", 0)))
        existing = await session.raw(
            f"SELECT state, fencing_token, result_status FROM {self.table} "
            "WHERE source=$1 AND message_id=$2",
            source,
            envelope.id,
        ).fetchrow()
        if existing is None:
            raise RuntimeError("webhook inbox claim disappeared inside transaction")
        state = str(_row_value(existing, "state", 0))
        token = int(_row_value(existing, "fencing_token", 1))
        status_value = _row_value(existing, "result_status", 2)
        status = None if status_value is None else int(status_value)
        if state == "completed":
            return InboxClaim("duplicate", token, status)
        if state == "failed":
            return InboxClaim("failed", token, status)
        return InboxClaim("active", token, status)

    def purge_pass(self, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that drops inbox rows past their retention.

        The supported way to keep the inbox small:

        ```python
        jobs.drive(inbox.purge_pass(), cron="23 * * * *")
        ```

        `purge` has a chunk size and nothing else -- no cursor, so it
        starts from the beginning of the index every time; no resumption, so a
        redeploy loses where it was; and no pacing, so it competes with delivery
        for the same pool. The pass supplies all three, and keeps one
        transaction per chunk. See `wreath.passes`.

        Only rows with a `retention_until` in the past are eligible; a row
        whose retention was never stamped is never purged by either form.

        Takes no database: the pass is a declaration, and it is handed one when
        the scheduler drives it.

        Args:
            chunk: Rows per transaction.
            options: Forwarded to the pass -- `within`, `shift`, `pace`, `schema`.

        Returns:
            An unstarted `wreath.passes.ChunkedPass`; drive it from the scheduler.
        """
        return _retention_purge_pass(
            table=self.table, key=("source", "message_id"), chunk=chunk, **options
        )

    async def purge(self, session: Any, *, limit: int = 1000) -> int:
        """Delete up to *limit* rows past their retention, in the caller's transaction.

        One bounded chunk with no cursor, no resumption, and no pacing. It is
        the right tool when you already hold a session and want a bounded amount
        of work done right now; for keeping the table small forever, use
        `purge_pass`.

        Locks with `SKIP LOCKED`, so two callers running it at once delete
        disjoint rows instead of blocking on each other.

        Args:
            session: An open session inside a transaction. Not committed here.
            limit: Maximum rows deleted in this call.

        Returns:
            How many rows were deleted; 0 when nothing was past retention.

        Raises:
            ValueError: `limit` is not positive.
        """
        if limit <= 0:
            raise ValueError("webhook inbox purge limit must be positive")
        deleted = await session.raw(
            f"WITH expired AS (SELECT ctid FROM {self.table} "
            "WHERE retention_until < clock_timestamp() "
            "ORDER BY retention_until FOR UPDATE SKIP LOCKED LIMIT $1), "
            f"removed AS (DELETE FROM {self.table} AS i USING expired "
            "WHERE i.ctid=expired.ctid RETURNING 1) "
            "SELECT count(*) FROM removed",
            limit,
        ).fetchval()
        return int(deleted or 0)

    async def complete(
        self,
        session: Any,
        *,
        source: str,
        message_id: str,
        fencing_token: int,
        result_status: int,
    ) -> None:
        """Mark a claimed event completed, if this worker still holds the claim.

        The fencing check is the point. A worker whose lease expired while it was
        working has already been superseded, and letting it write `completed`
        would mark an event done that its successor is still processing --
        exactly the split-brain the lease exists to prevent. It raises instead,
        and the caller's transaction rolls back with it.

        Args:
            session: The same transaction the claim was made in.
            fencing_token: The token from the `InboxClaim`. A stale one is refused.
            result_status: Status recorded for replay on a later duplicate.

        Raises:
            RuntimeError: The lease was taken over, or the row is no longer processing.
        """
        updated = await session.raw(
            f"UPDATE {self.table} SET state='completed', completed_at=clock_timestamp(), "
            "result_status=$4, lease_expires_at=clock_timestamp() "
            "WHERE source=$1 AND message_id=$2 AND fencing_token=$3 "
            "AND state='processing' RETURNING 1",
            source,
            message_id,
            fencing_token,
            result_status,
        ).fetchval()
        if updated is None:
            raise RuntimeError("stale webhook inbox fencing token")


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except KeyError, TypeError:
        return row[index]


class PostgresWebhookOutbox:
    """Transactional durable intent for a supervised webhook dispatcher.

    The transactional outbox pattern. A delivery is *inserted* in the same
    transaction as the change that caused it, so the two commit together: there
    is no state in which the order shipped but the notification was lost, and
    none in which it was sent for an order that rolled back. Sending happens
    afterwards, out of band, by `WebhookDispatcher`.

    Rows move through `pending` -> `leased` -> `sending` -> one of
    `delivered`, `retry_wait`, `failed`, `unknown` or `cancelled`.
    Every transition is fenced on `(delivery_id, fencing_token)`, so a worker
    whose lease expired mid-send cannot write over the worker that took the row
    from it -- it raises instead. Claiming uses `FOR UPDATE SKIP LOCKED`, so
    workers do not contend.

    Because the transport can fail without an answer, `unknown` is a terminal
    state distinct from `failed`: the dispatcher does not retry it, since a
    request that may already have been processed must not be sent twice on a
    guess.

    Args:
        table: Table name. Must be a plain SQL identifier; it is interpolated, not bound.

    Raises:
        ValueError: `table` is not a plain SQL identifier.
    """

    __slots__ = ("table",)

    def __init__(self, table: str = "wreath_webhook_outbox") -> None:
        self.table = validate_unquoted_identifier(table, "webhook outbox table")

    def statements(self) -> tuple[str, ...]:
        """DDL creating the outbox table and its two indexes. Idempotent.

        `IF NOT EXISTS` throughout, plus an `ADD COLUMN IF NOT EXISTS` for
        `relay_path`, so running it against an older deployment upgrades it in
        place. One index serves the dispatcher's ready-row query and one serves
        the retention purge -- the latter was added because the chunked pass
        refuses to walk an unindexed key, which is how a purge that had always
        been a sequential scan came to light.

        The table name is **unqualified**, so it lands in whatever `search_path`
        resolves to rather than in the `wreath` schema -- where existing rows
        already are. See `wreath.schema.Component.qualified`.

        Returns:
            One statement per element, in order.
        """
        table = self.table
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            "    delivery_id text PRIMARY KEY,\n"
            "    event_id text NOT NULL,\n"
            "    destination text NOT NULL,\n"
            "    event_type text NOT NULL,\n"
            "    event_timestamp timestamptz NOT NULL,\n"
            "    payload_version text NOT NULL,\n"
            "    payload_bytes bytea NOT NULL,\n"
            "    content_type text NOT NULL,\n"
            "    signature_profile text NOT NULL,\n"
            "    key_id text NOT NULL,\n"
            "    state text NOT NULL DEFAULT 'pending' CHECK (state IN "
            "('pending','leased','sending','delivered','retry_wait','failed',"
            "'cancelled','unknown')),\n"
            "    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),\n"
            "    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
            "    lease_owner text,\n"
            "    lease_expires_at timestamptz,\n"
            "    fencing_token bigint NOT NULL DEFAULT 0,\n"
            "    idempotency_key text NOT NULL,\n"
            "    ordering_key text,\n"
            "    correlation_id text,\n"
            "    causation_id text,\n"
            "    relay_path text NOT NULL DEFAULT '',\n"
            "    last_response_status integer,\n"
            "    last_failure_code text,\n"
            "    last_failure_summary text,\n"
            "    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
            "    last_attempt_at timestamptz,\n"
            "    completed_at timestamptz,\n"
            "    retention_until timestamptz\n"
            ")",
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS relay_path text NOT NULL DEFAULT ''",
            f"CREATE INDEX IF NOT EXISTS {table}_ready_idx ON {table} "
            "(next_attempt_at, created_at) WHERE state IN ('pending','retry_wait')",
            # Retention has always been read by both purges and never had an
            # index under it, so every purge was a sequential scan and a sort.
            # The chunked pass refuses to walk an unindexed key, which is how
            # this surfaced.
            f"CREATE INDEX IF NOT EXISTS {table}_retention_idx ON {table} "
            "(retention_until) WHERE retention_until IS NOT NULL",
        )

    def component(self) -> Any:
        """The outbox's claim on the wreath schema."""
        return _schema_component("webhook-outbox", self.table, self.statements())

    def schema_sql(self) -> str:
        """The outbox DDL, semicolon-joined. A derivation of `component()`."""
        return self.component().sql()

    async def enqueue(
        self,
        session: Any,
        *,
        destination: str,
        envelope: WebhookEnvelope,
        key_id: str,
    ) -> str:
        """Insert one pending delivery in the caller's transaction.

        Writes only; nothing is sent here and no connection is opened. The row
        becomes visible to the dispatcher when the caller's transaction commits,
        and disappears if it rolls back -- which is the whole point of the
        pattern.

        The signing key id is recorded on the row rather than resolved at send
        time, so a delivery that sits in the outbox across a key rotation is
        still signed with the key its receiver was expecting.

        Args:
            session: An open session inside a transaction. Not committed here.
            destination: The registered destination name the dispatcher routes on.
            key_id: The signing key id to use when this row is eventually sent.

        Returns:
            The generated delivery id, distinct from the event id.
        """
        delivery_id = uuid.uuid4().hex
        sql = (
            f"INSERT INTO {self.table} "
            "(delivery_id, event_id, destination, event_type, event_timestamp, "
            "payload_version, payload_bytes, content_type, signature_profile, key_id, "
            "idempotency_key, ordering_key, correlation_id, causation_id, relay_path) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)"
        )
        await session.raw(
            sql,
            delivery_id,
            envelope.id,
            destination,
            envelope.type,
            envelope.timestamp,
            envelope.version,
            envelope.body,
            envelope.content_type,
            "wreath-v1-hmac-sha256",
            key_id,
            envelope.id,
            envelope.ordering_key,
            envelope.correlation_id,
            envelope.causation_id,
            ",".join(envelope.relay_path),
        ).execute()
        return delivery_id

    def purge_pass(self, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that drops settled deliveries past their retention.

        The supported way to keep the outbox small:

        ```python
        jobs.drive(outbox.purge_pass(), cron="43 * * * *")
        ```

        Only rows in a settled state are eligible, exactly as `purge`
        does it -- a delivery still waiting on a retry is not rubbish. What the
        pass adds is the cursor, the resumption, and the pacing that a bare
        `LIMIT` does not have. See `wreath.passes`.

        Takes no database: the pass is a declaration, and it is handed one when
        the scheduler drives it.

        Args:
            chunk: Rows per transaction.
            options: Forwarded to the pass -- `within`, `shift`, `pace`, `schema`.

        Returns:
            An unstarted `wreath.passes.ChunkedPass`; drive it from the scheduler.
        """
        return _retention_purge_pass(
            table=self.table,
            key=("delivery_id",),
            chunk=chunk,
            where=_SETTLED_STATES,
            **options,
        )

    async def purge(self, session: Any, *, limit: int = 1000) -> int:
        """Delete up to *limit* settled rows past retention, in the caller's transaction.

        One bounded chunk with no cursor, no resumption, and no pacing; see
        `purge_pass` for the form that keeps the table small forever.

        A delivery still waiting on a retry is never eligible, whatever its
        retention stamp says -- only `delivered`, `failed`, `cancelled` and
        `unknown` rows are rubbish.

        Args:
            session: An open session inside a transaction. Not committed here.
            limit: Maximum rows deleted in this call.

        Returns:
            How many rows were deleted; 0 when nothing was eligible.

        Raises:
            ValueError: `limit` is not positive.
        """
        if limit <= 0:
            raise ValueError("webhook outbox purge limit must be positive")
        deleted = await session.raw(
            f"WITH expired AS (SELECT ctid FROM {self.table} "
            "WHERE retention_until < clock_timestamp() "
            f"AND {_SETTLED_STATES} "
            "ORDER BY retention_until FOR UPDATE SKIP LOCKED LIMIT $1), "
            f"removed AS (DELETE FROM {self.table} AS o USING expired "
            "WHERE o.ctid=expired.ctid RETURNING 1) "
            "SELECT count(*) FROM removed",
            limit,
        ).fetchval()
        return int(deleted or 0)

    async def claim_due(
        self,
        session: Any,
        *,
        lease_owner: str,
        lease_seconds: float,
    ) -> OutboxDelivery | None:
        """Lease the single most overdue delivery, or return None when none is due.

        Two kinds of row are due: one that is `pending` or `retry_wait` with
        its `next_attempt_at` in the past, and one whose worker died holding it
        -- `leased` or `sending` with an expired lease. Recovering the second
        kind is why a crashed dispatcher does not strand deliveries.

        `FOR UPDATE SKIP LOCKED` on one row means concurrent dispatchers each
        get a different delivery rather than blocking, and the attempt counter
        and fencing token both advance as the lease is taken, so the row this
        returns is already fenced against its previous owner.

        Args:
            session: An open session inside a transaction. Not committed here.
            lease_owner: Identifies the claiming worker; recorded on the row.
            lease_seconds: How long the claim holds before another worker may take it.

        Returns:
            The leased delivery, or None when nothing was due.

        Raises:
            ValueError: `lease_seconds` is not positive.
        """
        if lease_seconds <= 0:
            raise ValueError("webhook outbox lease_seconds must be positive")
        table = self.table
        sql = claim_sql(
            table,
            key="delivery_id",
            alias="AS o",
            candidate="candidate",
            predicate=(
                "((state IN ('pending','retry_wait') "
                "AND next_attempt_at <= clock_timestamp()) OR "
                "(state IN ('leased','sending') "
                "AND lease_expires_at < clock_timestamp()))"
            ),
            order="next_attempt_at, created_at",
            limit="1",
            assignments=(
                "state='leased', attempts=o.attempts+1, lease_owner=$1, "
                "lease_expires_at=clock_timestamp() + "
                "$2::float8 * interval '1 second', fencing_token=o.fencing_token+1"
            ),
            returning=(
                "o.delivery_id,o.event_id,o.destination,o.event_type,"
                "o.event_timestamp,o.payload_version,o.payload_bytes,o.content_type,"
                "o.key_id,o.attempts,o.fencing_token,o.ordering_key,"
                "o.correlation_id,o.causation_id,o.relay_path"
            ),
        )
        row = await session.raw(sql, lease_owner, lease_seconds).fetchrow()
        return None if row is None else _outbox_delivery(row)

    async def mark_sending(self, session: Any, delivery: OutboxDelivery) -> None:
        """Move a leased delivery to `sending`, just before the request goes out.

        The state a recovering worker reads as "this may already have reached the
        peer". Like every transition here it is fenced, so a worker that lost its
        lease cannot re-enter `sending`.

        Raises:
            RuntimeError: The fencing token is stale, or the row is no longer leased.
        """
        await self._transition(
            session,
            delivery,
            "state='sending'",
            (),
        )

    async def renew_lease(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        lease_seconds: float,
    ) -> None:
        """Extend the lease on a delivery that is still `sending`.

        For a slow peer: without renewal a request that outlasts the lease would
        be handed to a second worker while the first is still waiting on it. Only
        `sending` rows renew, and the fencing token must still match, so a
        worker that has already been superseded cannot claw the row back.

        Args:
            session: A session for this renewal, separate from the one carrying the send.
            lease_seconds: New lease length, measured from now.

        Raises:
            ValueError: `lease_seconds` is not positive.
            RuntimeError: The fencing token is stale, or the row is no longer sending.
        """
        if lease_seconds <= 0:
            raise ValueError("webhook outbox lease_seconds must be positive")
        renewed = await session.raw(
            f"UPDATE {self.table} SET lease_expires_at=clock_timestamp()+"
            "$3::float8*interval '1 second' WHERE delivery_id=$1 "
            "AND fencing_token=$2 AND state='sending' RETURNING 1",
            delivery.delivery_id,
            delivery.fencing_token,
            lease_seconds,
        ).fetchval()
        if renewed is None:
            raise RuntimeError("stale webhook outbox fencing token")

    async def mark_delivered(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        status: int,
    ) -> None:
        """Settle a delivery as `delivered` and release its lease. Terminal.

        Args:
            status: The 2xx the peer answered with; recorded for audit.

        Raises:
            RuntimeError: The fencing token is stale, or the row is not leased or sending.
        """
        await self._transition(
            session,
            delivery,
            "state='delivered',completed_at=clock_timestamp(),"
            "last_response_status=$3,lease_owner=NULL,lease_expires_at=NULL",
            (status,),
        )

    async def mark_retry(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        delay: float,
        status: int | None,
        failure: str | None,
    ) -> None:
        """Return a delivery to `retry_wait`, due again after `delay` seconds.

        Not terminal: the row releases its lease and becomes claimable again once
        `next_attempt_at` passes. The attempt counter is not touched here --
        it advanced when the lease was taken -- so a row cannot be retried
        forever by a worker that keeps failing before it claims.

        Args:
            delay: Seconds from now until the row is due again. May be 0.
            status: The response status that triggered the retry, or None on a transport failure.
            failure: A short failure code, truncated to 256 characters.

        Raises:
            ValueError: `delay` is negative.
            RuntimeError: The fencing token is stale, or the row is not leased or sending.
        """
        if delay < 0:
            raise ValueError("webhook retry delay cannot be negative")
        await self._transition(
            session,
            delivery,
            "state='retry_wait',next_attempt_at=clock_timestamp()+"
            "$3::float8*interval '1 second',last_response_status=$4,"
            "last_failure_code=$5,lease_owner=NULL,lease_expires_at=NULL",
            (delay, status, _bounded_failure(failure)),
        )

    async def mark_unknown(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        failure: str | None,
    ) -> None:
        """Settle a delivery as `unknown`: nobody knows whether it arrived. Terminal.

        Deliberately not retried. The request may have been processed by the
        peer, and resending on that guess is how a transport failure becomes a
        duplicate charge. These rows are for a human or a reconciliation job to
        resolve against the receiver.

        Args:
            failure: A short failure code, truncated to 256 characters.

        Raises:
            RuntimeError: The fencing token is stale, or the row is not leased or sending.
        """
        await self._transition(
            session,
            delivery,
            "state='unknown',completed_at=clock_timestamp(),last_failure_code=$3,"
            "lease_owner=NULL,lease_expires_at=NULL",
            (_bounded_failure(failure),),
        )

    async def mark_failed(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        status: int | None,
        failure: str | None,
    ) -> None:
        """Settle a delivery as `failed`: the peer answered and refused. Terminal.

        Reached when the attempt budget is spent, when the status is not
        retryable, or when the row names a destination this dispatcher does not
        have. Distinct from `unknown` because here the peer's answer is known.

        Args:
            status: The refusing status, or None when there was no response to record.
            failure: A short failure code, truncated to 256 characters.

        Raises:
            RuntimeError: The fencing token is stale, or the row is not leased or sending.
        """
        await self._transition(
            session,
            delivery,
            "state='failed',completed_at=clock_timestamp(),"
            "last_response_status=$3,last_failure_code=$4,"
            "lease_owner=NULL,lease_expires_at=NULL",
            (status, _bounded_failure(failure)),
        )

    async def _transition(
        self,
        session: Any,
        delivery: OutboxDelivery,
        assignment: str,
        values: tuple[Any, ...],
    ) -> None:
        updated = await session.raw(
            f"UPDATE {self.table} SET {assignment} "
            "WHERE delivery_id=$1 AND fencing_token=$2 "
            "AND state IN ('leased','sending') RETURNING 1",
            delivery.delivery_id,
            delivery.fencing_token,
            *values,
        ).fetchval()
        if updated is None:
            raise RuntimeError("stale webhook outbox fencing token")


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    """One leased outbox row, as the dispatcher sees it. Immutable.

    Carries both keys and both is deliberate: `delivery_id` identifies the
    *attempt record*, `event_id` identifies the event, and a receiver
    deduplicates on the latter. `fencing_token` is what every later transition
    is matched on, so passing a stale copy of this object is refused rather than
    acted on.

    Args:
        delivery_id: Primary key of the outbox row.
        event_id: The envelope id, and the receiver's deduplication key.
        destination: Names the registered `WebhookDestination` to send through.
        attempts: Attempts made including this one; incremented as the lease was taken.
        fencing_token: Rises on every claim; every transition is matched on it.
        key_id: The signing key recorded at enqueue, so rotation cannot orphan the row.
    """

    delivery_id: str
    event_id: str
    destination: str
    event_type: str
    timestamp: datetime
    version: str
    body: bytes
    content_type: str
    key_id: str
    attempts: int
    fencing_token: int
    ordering_key: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    relay_path: tuple[str, ...] = ()


def _outbox_delivery(row: Any) -> OutboxDelivery:
    return OutboxDelivery(
        delivery_id=str(_row_value(row, "delivery_id", 0)),
        event_id=str(_row_value(row, "event_id", 1)),
        destination=str(_row_value(row, "destination", 2)),
        event_type=str(_row_value(row, "event_type", 3)),
        timestamp=_row_value(row, "event_timestamp", 4),
        version=str(_row_value(row, "payload_version", 5)),
        body=bytes(_row_value(row, "payload_bytes", 6)),
        content_type=str(_row_value(row, "content_type", 7)),
        key_id=str(_row_value(row, "key_id", 8)),
        attempts=int(_row_value(row, "attempts", 9)),
        fencing_token=int(_row_value(row, "fencing_token", 10)),
        ordering_key=_row_value(row, "ordering_key", 11),
        correlation_id=_row_value(row, "correlation_id", 12),
        causation_id=_row_value(row, "causation_id", 13),
        relay_path=_parse_stored_relay_path(_optional_row_value(row, "relay_path", 14, "")),
    )


def _optional_row_value(row: Any, key: str, index: int, default: Any) -> Any:
    try:
        return row[key]
    except IndexError, KeyError, TypeError:
        try:
            return row[index]
        except IndexError, KeyError, TypeError:
            return default


def _parse_stored_relay_path(value: Any) -> tuple[str, ...]:
    if value in (None, "", b""):
        return ()
    data = value.encode("ascii") if isinstance(value, str) else bytes(value)
    return _parse_relay_path(data)


def _bounded_failure(value: str | None) -> str | None:
    return None if value is None else value[:256]


WebhookHandler = Callable[[WebhookContext, Any], Awaitable[Any]]
_DEFAULT_WEBHOOK_LIMITS = WebhookLimits()


class WebhookSource:
    """One sender's webhooks: a route, a verifier, and a handler per event type.

    Created through `WebhookHub.source`, which also records the path for
    CSRF exemption -- constructing one directly registers the route but leaves
    the hub unaware of it. Construction registers a `POST` route immediately,
    so it must happen while the application is still accepting routes.

    A request is processed in a fixed order, and the order is the security
    property: bound the request, verify the signature, bound the event id,
    resolve the handler, deduplicate, then run. Nothing an unauthenticated caller
    sends reaches a handler, and nothing expensive happens before the MAC checks
    out.

    Responses are deliberately terse and carry no detail: `401` for any
    verification failure, `413` for anything over a limit, `400` for an
    unregistered event type, an over-long id, or a body that is not JSON,
    `409` when another worker holds the event, and `204` on success. A sender
    learns whether to retry and nothing else.

    A body that passes the signature check and then fails to parse is the
    sender's error, not this service's: the MAC proves they sent exactly those
    bytes. It answers `400`, so a retry of the same bytes is not invited.

    Deduplication is whichever of the two was configured. With `inbox` and
    `session_factory` the handler runs *inside* the claim's transaction, so its
    writes and the "processed" record commit together. Without them the claim is
    the in-process `LocalReplayStore`, which is a fast path rather than a
    guarantee behind more than one worker.

    Args:
        path: Route path for the receiver. Registered on `app` at construction.
        replay: In-process replay store; one sized to the verifier's window is made if None.
        inbox: Cross-replica deduplication. Requires `session_factory`.
        session_factory: Opens the session the claim and handler share. Requires `inbox`.
        lease_owner: Identifies this worker on an inbox claim. Only used with `inbox`.
        lease_seconds: Inbox claim lease. Only used with `inbox`.

    Raises:
        ValueError: `inbox` and `session_factory` were not both given, or the lease is invalid.
    """

    __slots__ = (
        "_handlers",
        "_inbox",
        "_lease_owner",
        "_lease_seconds",
        "_limits",
        "_name",
        "_replay",
        "_session_factory",
        "_verifier",
    )

    def __init__(
        self,
        app: Any,
        name: str,
        *,
        path: str,
        verifier: WebhookVerifier,
        replay: LocalReplayStore | None,
        limits: WebhookLimits,
        inbox: PostgresWebhookInbox | None,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None,
        lease_owner: str,
        lease_seconds: float,
    ) -> None:
        if (inbox is None) != (session_factory is None):
            raise ValueError("durable webhook sources require inbox and session_factory")
        if inbox is not None and (not lease_owner or lease_seconds <= 0):
            raise ValueError("durable webhook source lease configuration is invalid")
        self._name = name
        self._verifier = verifier
        self._replay = replay or LocalReplayStore(max_entries=10_000, ttl=verifier.max_age)
        self._limits = limits
        self._inbox = inbox
        self._session_factory = session_factory
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._handlers: dict[str, tuple[Any, WebhookHandler]] = {}

        @app.post(path, tags=("webhooks",), summary=f"Receive {name} webhooks")
        async def receive(request: Request) -> Response:
            return await self._receive(request)

    def event(self, event_type: str, *, payload: Any) -> Callable[[WebhookHandler], WebhookHandler]:
        """Decorator registering the handler for one event type.

        The handler is called as `handler(context, payload)` with the payload
        already decoded and validated against `payload`; a request whose body
        does not fit raises out of the receiver rather than reaching the handler.
        An event type with no registered handler is answered `400`, so a source
        never silently ignores an event it was sent.

        The handler's return value is discarded -- success is signalled by
        returning, failure by raising. Raising on a durable source rolls the
        inbox claim back with the handler's own writes; on a non-durable source
        the replay claim stands, so the sender's retry is refused until the TTL
        passes.

        Args:
            event_type: Matched against the `wreath-webhook-type` header. Cannot be empty.
            payload: A validation target; the same shapes a route body annotation accepts.

        Returns:
            The registering decorator, which returns the handler unchanged.

        Raises:
            ValueError: `event_type` is empty, or already has a handler.
        """
        if not event_type:
            raise ValueError("webhook event type cannot be empty")

        def register(handler: WebhookHandler) -> WebhookHandler:
            if event_type in self._handlers:
                raise ValueError(f"duplicate webhook event handler: {event_type}")
            self._handlers[event_type] = (_body_validator(payload), handler)
            return handler

        return register

    async def _receive(self, request: Request) -> Response:
        raw_headers = request.headers
        if len(raw_headers) > self._limits.max_headers:
            return Response(status=413)
        headers: dict[bytes, bytes] = {}
        header_bytes = 0
        for name, value in raw_headers:
            header_bytes += len(name) + len(value)
            if header_bytes > self._limits.max_header_bytes:
                return Response(status=413)
            headers.setdefault(name.lower(), value)
        body = await request.body()
        if len(body) > self._limits.max_body_bytes:
            return Response(status=413)
        try:
            envelope = self._verifier.verify(body=body, headers=headers)
        except UnicodeDecodeError, ValueError:
            return Response(status=401)
        if len(envelope.id.encode("utf-8")) > self._limits.max_event_id_bytes:
            return Response(status=400)
        registered = self._handlers.get(envelope.type)
        if registered is None:
            return Response(status=400)
        payload_validator, handler = registered
        try:
            decoded = loads(body)
        except ValueError:
            # The MAC checked out, so this body is the one the sender meant to
            # send -- it just is not JSON. That is the sender's mistake, not
            # ours, and a 500 would tell them to retry something that cannot
            # start working. `loads` raises ValueError for malformed JSON and
            # for bytes that are not UTF-8 alike; both mean the same thing here.
            return Response(status=400)
        payload = payload_validator(decoded, ("body",))
        if self._inbox is not None:
            if self._session_factory is None:
                raise RuntimeError("a webhook inbox requires a session factory")
            async with self._session_factory() as session:
                async with session.begin():
                    claim = await self._inbox.claim(
                        session,
                        source=self._name,
                        envelope=envelope,
                        lease_owner=self._lease_owner,
                        lease_seconds=self._lease_seconds,
                    )
                    if claim.outcome == "duplicate":
                        return Response(status=claim.result_status or 204)
                    if claim.outcome in {"active", "failed"}:
                        return Response(status=409)
                    await handler(WebhookContext(self._name, envelope, request, session), payload)
                    await self._inbox.complete(
                        session,
                        source=self._name,
                        message_id=envelope.id,
                        fencing_token=claim.fencing_token,
                        result_status=204,
                    )
            return Response(status=204)
        if not await self._replay.claim(self._name, envelope.id):
            return Response(status=409)
        try:
            await handler(WebhookContext(self._name, envelope, request), payload)
        except Exception:  # records the outcome and re-raises
            # Broad and re-raising, which is the shape that earns it: the
            # receiver's own code failed, every way it can fail means the same
            # thing to the replay claim, and nothing is swallowed -- the caller
            # still sees the original exception with its traceback intact.
            await self._replay.complete(self._name, envelope.id, "failed")
            raise
        await self._replay.complete(self._name, envelope.id, "completed")
        return Response(status=204)


class WebhookDestination:
    """One receiver: a client, a path on it, a signing key, and optionally an outbox.

    Created through `WebhookHub.destination`. The client is origin-pinned,
    so the destination owns only the path -- which is why the path must be
    origin-relative and is refused otherwise.

    Two ways to send, and they are not interchangeable. `send` posts
    immediately and hands back the outcome, which is right for something that has
    no durable consequence. `enqueue` writes the intent into the caller's
    transaction and returns; the delivery then survives a crash and is sent by
    `WebhookDispatcher` with leases and retries. If losing the webhook
    would leave the receiver's state wrong, it belongs in the outbox.

    `relay_id` names this service in a relayed envelope's path, and the path is
    signed. It defaults to the destination name.

    Args:
        client: An origin-pinned HTTP client, normally a `wreath.http_client.HTTPClient`.
        path: Origin-relative receiver path on that client. Must start with a single `/`.
        outbox: Enables `enqueue` and `enqueue_relay`. Without it they raise.
        relay_id: This service's name in a relay path. Must match the relay-id syntax.
        max_relay_hops: Hops permitted before a relay is refused. Between 1 and 32.

    Raises:
        ValueError: The path is not origin-relative, or `relay_id` or the hop limit is invalid.
    """

    __slots__ = (
        "_client",
        "_max_relay_hops",
        "_name",
        "_outbox",
        "_path",
        "_relay_id",
        "_signer",
    )

    def __init__(
        self,
        name: str,
        *,
        client: Any,
        path: str,
        signer: HMACWebhookSigner,
        outbox: PostgresWebhookOutbox | None,
        relay_id: str,
        max_relay_hops: int,
    ) -> None:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("webhook destination path must be origin-relative")
        if not _RELAY_ID.fullmatch(relay_id):
            raise ValueError("webhook destination relay_id is invalid")
        if not 1 <= max_relay_hops <= 32:
            raise ValueError("webhook destination max_relay_hops must be between 1 and 32")
        self._name = name
        self._client = client
        self._path = path
        self._signer = signer
        self._outbox = outbox
        self._relay_id = relay_id
        self._max_relay_hops = max_relay_hops

    async def send(
        self,
        event_type: str,
        payload: Any,
        *,
        event_id: str | None = None,
        version: str = "1",
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        relay_path: tuple[str, ...] = (),
    ) -> WebhookDeliveryResult:
        """Sign and post one event now, returning the outcome rather than raising.

        A transport failure is *reported*, not raised: `ClientError` becomes an
        `unknown` result, because a request that failed without an answer may
        still have been processed. A non-2xx answer becomes `failed`. Nothing
        here retries, and nothing here is durable -- if the process dies between
        the caller's commit and this call, the event is simply gone. Use
        `enqueue` when that matters.

        The event id doubles as the client's idempotency key, so a receiver that
        honours `Idempotency-Key` collapses a duplicate on its own.

        Args:
            payload: Bytes-like is sent verbatim; anything else is JSON-encoded.
            event_id: Defaults to a fresh UUID4 hex. Supply one to make the send repeatable.
            version: Payload schema version, signed and sent.
            timestamp: Defaults to now, UTC. Must be timezone-aware.
            relay_path: Prior hops. Prefer `relay`, which builds this correctly.

        Returns:
            The outcome -- `delivered`, `failed`, or `unknown`.

        Raises:
            ValueError: The envelope fields or the relay path are invalid.
        """
        body = (
            bytes(payload)
            if isinstance(payload, (bytes, bytearray, memoryview))
            else dumps(payload)
        )
        envelope = WebhookEnvelope(
            id=event_id or uuid.uuid4().hex,
            type=event_type,
            version=version,
            timestamp=datetime.now(UTC) if timestamp is None else timestamp,
            content_type="application/json",
            body=body,
            correlation_id=correlation_id,
            causation_id=causation_id,
            relay_path=relay_path,
        )
        return await self._send_envelope(envelope)

    async def _send_envelope(
        self,
        envelope: WebhookEnvelope,
        *,
        key_id: str | None = None,
    ) -> WebhookDeliveryResult:
        headers = (
            *self._signer.headers(envelope, key_id=key_id),
            (b"content-type", envelope.content_type.encode("latin-1")),
        )
        try:
            response = await self._client.post(
                self._path,
                headers=headers,
                body=envelope.body,
                idempotency_key=envelope.id,
            )
        except ClientError as error:
            return WebhookDeliveryResult("unknown", envelope.id, failure=type(error).__name__)
        if 200 <= response.status < 300:
            return WebhookDeliveryResult("delivered", envelope.id, response.status)
        return WebhookDeliveryResult("failed", envelope.id, response.status)

    async def enqueue(
        self,
        session: Any,
        event_type: str,
        payload: Any,
        *,
        event_id: str | None = None,
        version: str = "1",
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        ordering_key: str | None = None,
        relay_path: tuple[str, ...] = (),
    ) -> str:
        """Insert durable delivery intent in the caller's transaction.

        Nothing is sent and no connection is opened; this writes a row. The
        delivery becomes real exactly when the caller's transaction commits and
        vanishes if it rolls back, so the notification and the change that caused
        it can never disagree. `WebhookDispatcher` sends it afterwards.

        The signing key id is captured now, from the destination's signer, so a
        rotation between enqueue and send cannot orphan the row.

        Args:
            session: An open session inside a transaction. Not committed here.
            payload: Bytes-like is stored verbatim; anything else is JSON-encoded.
            event_id: Defaults to a fresh UUID4 hex; also becomes the idempotency key.
            ordering_key: Stored for grouping. Advisory -- nothing orders delivery by it.

        Returns:
            The delivery id of the new outbox row.

        Raises:
            RuntimeError: This destination was configured without an outbox.
            ValueError: The envelope fields or the relay path are invalid.
        """
        if self._outbox is None:
            raise RuntimeError("webhook destination has no durable outbox")
        body = (
            bytes(payload)
            if isinstance(payload, (bytes, bytearray, memoryview))
            else dumps(payload)
        )
        envelope = WebhookEnvelope(
            id=event_id or uuid.uuid4().hex,
            type=event_type,
            version=version,
            timestamp=datetime.now(UTC) if timestamp is None else timestamp,
            content_type="application/json",
            body=body,
            correlation_id=correlation_id,
            causation_id=causation_id,
            ordering_key=ordering_key,
            relay_path=relay_path,
        )
        return await self._outbox.enqueue(
            session,
            destination=self._name,
            envelope=envelope,
            key_id=self._signer.key_id,
        )

    async def enqueue_relay(
        self,
        session: Any,
        inbound: WebhookEnvelope,
        event_type: str,
        payload: Any,
        *,
        version: str = "1",
        timestamp: datetime | None = None,
        ordering_key: str | None = None,
    ) -> str:
        """Commit a loop-protected relay intent in the caller transaction.

        The durable form of `relay`, and the same lineage rules apply: the
        outbound event gets a new id, inherits `inbound`'s correlation id (or
        its id when it starts the chain), records `inbound.id` as its cause,
        and appends this service to the signed relay path.

        Args:
            session: An open session inside a transaction. Not committed here.
            inbound: The verified envelope this event is caused by.

        Returns:
            The delivery id of the new outbox row.

        Raises:
            ValueError: This service is already in the relay path, or the hop limit is reached.
            RuntimeError: This destination was configured without an outbox.
        """
        return await self.enqueue(
            session,
            event_type,
            payload,
            version=version,
            timestamp=timestamp,
            correlation_id=inbound.correlation_id or inbound.id,
            causation_id=inbound.id,
            ordering_key=ordering_key,
            relay_path=self._next_relay_path(inbound),
        )

    async def relay(
        self,
        inbound: WebhookEnvelope,
        event_type: str,
        payload: Any,
        *,
        version: str = "1",
        timestamp: datetime | None = None,
    ) -> WebhookDeliveryResult:
        """Emit a separately identified event caused by an inbound event.

        A relay is a *new* event, not a forward: it gets its own id and its own
        signature, and the receiver deduplicates it independently. What connects
        it to its cause is the correlation id (inherited, or `inbound.id` when
        this starts the chain), the causation id, and the relay path.

        The relay path is signed and checked before anything is sent, which is
        what makes a cycle of services impossible rather than merely unlikely: a
        service already in the path refuses, and so does a path at the hop limit.

        Args:
            inbound: The verified envelope this event is caused by.

        Returns:
            The outcome of the immediate send.

        Raises:
            ValueError: This service is already in the relay path, or the hop limit is reached.
        """
        return await self.send(
            event_type,
            payload,
            version=version,
            timestamp=timestamp,
            correlation_id=inbound.correlation_id or inbound.id,
            causation_id=inbound.id,
            relay_path=self._next_relay_path(inbound),
        )

    def _next_relay_path(self, inbound: WebhookEnvelope) -> tuple[str, ...]:
        if self._relay_id in inbound.relay_path:
            raise ValueError("webhook relay loop detected")
        if len(inbound.relay_path) >= self._max_relay_hops:
            raise ValueError("webhook relay hop limit exceeded")
        return (*inbound.relay_path, self._relay_id)


@dataclass(frozen=True, slots=True)
class DispatcherReadiness:
    """A health-endpoint view of one dispatcher.

    `ready` is the composite: running and no recorded error. The parts are kept
    separate because they fail differently -- a dispatcher that is not running at
    all is a startup problem, while one running with `last_error` set is a
    dispatcher that died and was restarted, or is about to stop.

    Args:
        ready: Running with no recorded error. The single value a probe should read.
        running: Whether the delivery loop is currently executing.
        in_flight: Deliveries currently awaiting a peer response.
        last_error: `"Type: message"` for the failure that ended the last run, or None.
    """

    ready: bool
    running: bool
    in_flight: int
    last_error: str | None


class _WebhookDispatcherService:
    """Supervisor adapter kept private so `WebhookDispatcher`'s API stays put."""

    __slots__ = ("_dispatcher", "_idle_delay", "_session_factory")

    def __init__(
        self,
        dispatcher: WebhookDispatcher,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        idle_delay: float,
    ) -> None:
        self._dispatcher = dispatcher
        self._session_factory = session_factory
        self._idle_delay = idle_delay

    async def start(self, supervisor: Any) -> None:
        dispatcher = self._dispatcher
        dispatcher._stopping = supervisor.stopping
        dispatcher._task = supervisor.spawn(
            f"wreath-webhook-{dispatcher._worker_id}",
            dispatcher.run(
                self._session_factory,
                supervisor.stopping,
                idle_delay=self._idle_delay,
            ),
        )
        await asyncio.sleep(0)
        if dispatcher._task.done():
            await dispatcher._task

    async def drain(self, deadline: float) -> None:
        dispatcher = self._dispatcher
        task = dispatcher._task
        if task is not None and not task.done():
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait((task,), timeout=remaining)
        if task is not None and task.done():
            await task
        dispatcher._task = None
        dispatcher._stopping = None


class WebhookDispatcher:
    """Fenced delivery loop with lease renewal and lifespan supervision hooks.

    Drains one outbox, routing each row to the destination it names. One
    dispatcher per worker; several may run against the same table without
    coordinating, because claiming uses `FOR UPDATE SKIP LOCKED` and every
    transition is fenced on the row's token.

    The loop is deliberately serial: one delivery at a time, per dispatcher.
    Throughput comes from running more workers rather than more concurrency
    inside one, which keeps the ordering and the lease reasoning simple.

    Retries are bounded by `max_attempts` and back off exponentially from
    `retry_delay`. Only a status in `retry_statuses` is retried; a transport
    failure with no answer settles as `unknown` and is *not* retried, because
    the peer may already have processed it.

    Args:
        outbox: The table to drain.
        destinations: Name to destination. A row naming an absent one settles as failed.
        worker_id: Identifies this worker on a lease. Cannot be empty.
        lease_seconds: Claim length; the lease is renewed at a third of this while sending.
        max_attempts: Attempts before a retryable failure settles as failed.
        retry_delay: Base backoff in seconds, doubled per attempt already made,
            capped at `retry_cap` and jittered ±20% by `wreath._jobcore.compute_backoff`
            -- the same calculation the job runner and the message bus retry on.
        retry_cap: Longest a retry may be deferred, however many attempts precede it.
        retry_statuses: Response statuses treated as retryable.

    Raises:
        ValueError: `worker_id` is empty, or a lease, attempt, or delay bound is invalid.
    """

    __slots__ = (
        "_destinations",
        "_in_flight",
        "_last_error",
        "_lease_seconds",
        "_managed",
        "_max_attempts",
        "_outbox",
        "_retry_cap",
        "_retry_delay",
        "_retry_statuses",
        "_running",
        "_stopping",
        "_task",
        "_worker_id",
    )

    def __init__(
        self,
        outbox: PostgresWebhookOutbox,
        destinations: Mapping[str, WebhookDestination],
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        max_attempts: int = 6,
        retry_delay: float = 1.0,
        retry_cap: float = 3600.0,
        retry_statuses: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504}),
    ) -> None:
        if not worker_id:
            raise ValueError("webhook dispatcher worker_id cannot be empty")
        if lease_seconds <= 0 or max_attempts <= 0 or retry_delay < 0:
            raise ValueError("webhook dispatcher limits are invalid")
        if retry_cap < retry_delay:
            raise ValueError(
                "webhook dispatcher retry_cap cannot be below retry_delay; a cap "
                "under the base would make the first retry shorter than asked for"
            )
        self._outbox = outbox
        self._destinations = dict(destinations)
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._retry_cap = retry_cap
        self._retry_statuses = retry_statuses
        self._running = False
        self._in_flight = 0
        self._last_error: str | None = None
        self._managed = False
        self._stopping: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def readiness(self) -> DispatcherReadiness:
        """A `DispatcherReadiness` snapshot. Synchronous, never blocks.

        `last_error` is not cleared by reading it; a new `run` clears it.
        """
        return DispatcherReadiness(
            ready=self._running and self._last_error is None,
            running=self._running,
            in_flight=self._in_flight,
            last_error=self._last_error,
        )

    def manage(
        self,
        app: Any,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        idle_delay: float = 0.25,
    ) -> None:
        """Attach the delivery loop to the application's lifespan.

        Registers the loop with the application's ordinary service supervisor,
        so startup, bounded drain, task ownership, and failure accounting use
        the same lifecycle as jobs, messaging, entities, and WebSockets. A loop
        that fails immediately is re-awaited during startup, turning "never
        started" into a failed boot instead of a quiet absence.

        The dispatcher is also published on `app.state` as
        `webhook_dispatcher_<worker_id>` (non-identifier characters replaced
        with `_`), so a health endpoint can find it without a global.

        A delivery loop that dies stays dead until the process restarts, with
        `readiness` reporting it. Call this once per dispatcher.

        Args:
            app: The application whose lifespan and state this binds to.
            session_factory: Opens a session per loop iteration and per lease renewal.
            idle_delay: Seconds to wait before re-polling when the outbox is empty.

        Raises:
            RuntimeError: This dispatcher is already managed.
        """
        if self._managed:
            raise RuntimeError("webhook dispatcher is already managed")
        self._managed = True
        state_name = re.sub(r"[^A-Za-z0-9_]", "_", self._worker_id)
        app.state.__setattr__(f"webhook_dispatcher_{state_name}", self)

        service = _WebhookDispatcherService(self, session_factory, idle_delay)
        # The adapter is an implementation detail, so do not publish a second
        # state attribute through `app.service()`. The application supervisor
        # consumes this same private registry during lifespan startup.
        app._services[f"__webhook_dispatcher_{id(self):x}"] = service

    async def run(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        stopping: asyncio.Event,
        *,
        idle_delay: float = 0.1,
    ) -> None:
        """Run until stopped; the application supervisor owns this coroutine.

        Each iteration opens a session, attempts one delivery, and closes it. An
        empty outbox waits on `stopping` for up to `idle_delay` rather than
        sleeping, so shutdown is prompt instead of costing a poll interval.

        Failures are *not* absorbed. Anything the loop raises is recorded in
        `readiness` and re-raised unchanged, because a delivery loop that
        keeps running while silently failing is worse than one that stops
        visibly. `CancelledError` propagates untouched.

        Args:
            session_factory: Opens one session per iteration; also used for lease renewal.
            stopping: Set it to end the loop after the current delivery settles.
            idle_delay: Seconds to wait before re-polling when the outbox is empty.

        Raises:
            ValueError: `idle_delay` is not positive.
            Exception: Whatever a delivery or the database raised, after recording it.
        """
        if idle_delay <= 0:
            raise ValueError("webhook dispatcher idle_delay must be positive")
        self._running = True
        self._last_error = None
        try:
            while not stopping.is_set():
                async with session_factory() as session:
                    result = await self.run_once(session, renewal_session_factory=session_factory)
                if result is None and not stopping.is_set():
                    try:
                        async with asyncio.timeout(idle_delay):
                            await stopping.wait()
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as error:  # records the outcome and re-raises
            # Same shape as the delivery path above: this only makes the failure
            # visible on the sender before letting it propagate unchanged.
            self._last_error = f"{type(error).__name__}: {error}"
            raise
        finally:
            self._running = False

    async def run_once(
        self,
        session: Any,
        *,
        renewal_session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    ) -> WebhookDeliveryResult | None:
        """Claim, send, and settle at most one delivery. The unit `run` repeats.

        Returns None when nothing is due, which is the caller's signal to idle.
        Otherwise it claims one row, marks it `sending`, posts it, and settles
        it into exactly one terminal or retry state before returning -- a row is
        never left leased by a successful call.

        Directly useful in tests and in a one-shot drain: it needs no event loop
        of its own and no lifespan.

        Lease renewal is opt-in via `renewal_session_factory`. Without it a
        response slower than `lease_seconds` lets another worker claim the row
        while this send is still outstanding, and the settle then fails on the
        fencing token. Pass a factory whenever peers can be slow. The renewal
        task is cancelled and awaited in a `finally`, so it cannot outlive the
        send it was protecting.

        Args:
            session: An open session inside a transaction. Not committed here.
            renewal_session_factory: Opens a separate session to renew the lease while sending.

        Returns:
            The attempt's outcome, or None when no delivery was due.

        Raises:
            RuntimeError: The lease was taken over mid-send, so the settle was refused.
        """
        delivery = await self._outbox.claim_due(
            session,
            lease_owner=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if delivery is None:
            return None
        destination = self._destinations.get(delivery.destination)
        if destination is None:
            result = WebhookDeliveryResult(
                "failed", delivery.event_id, failure="UnknownDestination"
            )
            await self._outbox.mark_failed(
                session,
                delivery,
                status=None,
                failure=result.failure,
            )
            return result
        await self._outbox.mark_sending(session, delivery)
        envelope = WebhookEnvelope(
            id=delivery.event_id,
            type=delivery.event_type,
            version=delivery.version,
            timestamp=delivery.timestamp,
            content_type=delivery.content_type,
            body=delivery.body,
            correlation_id=delivery.correlation_id,
            causation_id=delivery.causation_id,
            ordering_key=delivery.ordering_key,
            relay_path=delivery.relay_path,
        )
        renewal: asyncio.Task[None] | None = None
        if renewal_session_factory is not None:
            renewal = asyncio.create_task(
                self._renew_lease(delivery, renewal_session_factory),
                name=f"wreath-webhook-lease-{delivery.delivery_id}",
            )
        self._in_flight += 1
        try:
            result = await destination._send_envelope(envelope, key_id=delivery.key_id)
        finally:
            self._in_flight -= 1
            if renewal is not None:
                renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal
        if result.outcome == "delivered":
            if result.status is None:
                raise RuntimeError("a delivered webhook result requires an HTTP status")
            await self._outbox.mark_delivered(session, delivery, status=result.status)
        elif result.outcome == "unknown":
            await self._outbox.mark_unknown(session, delivery, failure=result.failure)
        elif delivery.attempts < self._max_attempts and result.status in self._retry_statuses:
            await self._outbox.mark_retry(
                session,
                delivery,
                delay=compute_backoff(
                    delivery.attempts,
                    kind="exp",
                    base=self._retry_delay,
                    # Bounded and jittered, where this was neither. An outage
                    # fails every pending delivery at once, so a lockstep retry
                    # is the thundering herd `compute_backoff` exists to break;
                    # the cap is what stops attempt 6 of a raised max_attempts
                    # scheduling a delivery days out.
                    cap=self._retry_cap,
                    jitter=0.2,
                ),
                status=result.status,
                failure=result.failure,
            )
        else:
            await self._outbox.mark_failed(
                session,
                delivery,
                status=result.status,
                failure=result.failure,
            )
        return result

    async def _renew_lease(
        self,
        delivery: OutboxDelivery,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    ) -> None:
        interval = max(0.01, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            async with session_factory() as session:
                await self._outbox.renew_lease(session, delivery, lease_seconds=self._lease_seconds)


class WebhookHub:
    """The registry an application declares its webhook sources and destinations on.

    Reached as `app.webhooks(name)` rather than constructed directly. Its job
    is to be the one place that knows every receiver route, which is what makes
    `csrf_exempt` answerable at all: a webhook is authenticated by its
    signature, not by a browser-origin token, so its route must be exempt from
    CSRF -- and an exemption is only safe when the set of exempt paths is closed
    and known.

    Names are unique in both directions and a duplicate raises, so a second
    registration cannot quietly shadow the first.

    Args:
        app: The application to register receiver routes on.
        name: Identifies this hub. Cannot be empty.

    Raises:
        ValueError: `name` is empty.
    """

    __slots__ = ("_app", "_destinations", "_name", "_source_paths", "_sources")

    def __init__(self, app: Any, name: str) -> None:
        if not name:
            raise ValueError("webhook hub name cannot be empty")
        self._app = app
        self._name = name
        self._sources: dict[str, WebhookSource] = {}
        self._source_paths: set[str] = set()
        self._destinations: dict[str, WebhookDestination] = {}

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """The inbox and outbox stores this hub's sources and destinations hold.

        A hub owns no tables itself; its stores do, and they are optional -- a
        hub with only a signing destination and no durable outbox has none. So
        the application asks the hub which of its parts have a claim rather than
        assuming a fixed pair, and a source registered without an inbox
        contributes nothing to bootstrap.
        """
        owners: list[Any] = []
        for source in self._sources.values():
            inbox = getattr(source, "_inbox", None)
            if inbox is not None:
                owners.append(inbox)
        for destination in self._destinations.values():
            outbox = getattr(destination, "_outbox", None)
            if outbox is not None:
                owners.append(outbox)
        return tuple(owners)

    def csrf_exempt(self, request: Request) -> bool:
        """Return true only for registered unsafe webhook receiver routes.

        Pass it to the CSRF middleware as its exemption predicate. It is narrow
        on purpose: `POST` only, and only for a path this hub registered as a
        source. A machine sender cannot hold a CSRF token, and the request is
        already authenticated by its HMAC signature, so the exemption costs
        nothing -- but it must not extend one path further than that.

        Returns:
            True only for a `POST` to a registered source path.
        """
        return request.method == "POST" and request.path in self._source_paths

    def source(
        self,
        name: str,
        *,
        path: str,
        verifier: WebhookVerifier,
        replay: LocalReplayStore | None = None,
        limits: WebhookLimits = _DEFAULT_WEBHOOK_LIMITS,
        inbox: PostgresWebhookInbox | None = None,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        lease_owner: str = "webhook-receiver",
        lease_seconds: float = 30.0,
    ) -> WebhookSource:
        """Register one sender's receiver route and record it as CSRF-exempt.

        The supported way to build a `WebhookSource`: constructing one
        directly registers the route but leaves this hub -- and therefore
        `csrf_exempt` -- unaware of the path.

        Args:
            name: Unique per hub. Also the deduplication namespace for this sender's ids.
            path: Route path for the receiver, registered on the application now.
            verifier: Holds the sender's keys and the replay window.
            replay: In-process replay store; one sized to the verifier's window is made if None.
            inbox: Cross-replica deduplication. Requires `session_factory`.
            session_factory: Opens the session the claim and handler share. Requires `inbox`.
            lease_owner: Identifies this worker on an inbox claim.
            lease_seconds: Inbox claim lease length.

        Returns:
            The registered source, to hang `WebhookSource.event` handlers on.

        Raises:
            ValueError: The name is already registered, or the source configuration is invalid.
        """
        if name in self._sources:
            raise ValueError(f"duplicate webhook source: {name}")
        source = WebhookSource(
            self._app,
            name,
            path=path,
            verifier=verifier,
            replay=replay,
            limits=limits,
            inbox=inbox,
            session_factory=session_factory,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )
        self._sources[name] = source
        self._source_paths.add(path)
        return source

    def destination(
        self,
        name: str,
        *,
        client: Any,
        path: str,
        signer: HMACWebhookSigner,
        outbox: PostgresWebhookOutbox | None = None,
        relay_id: str | None = None,
        max_relay_hops: int = 8,
    ) -> WebhookDestination:
        """Register one receiver to send to.

        `name` is what an outbox row stores, so a dispatcher routes on it --
        renaming a destination orphans the rows already enqueued under the old
        name, which settle as `failed` with `UnknownDestination`.

        Args:
            name: Unique per hub. Stored on outbox rows and used as the default relay id.
            client: An origin-pinned HTTP client for the receiver's origin.
            path: Origin-relative receiver path. Must start with a single `/`.
            signer: Holds the signing keys and the default key id.
            outbox: Enables durable enqueueing. Without it only the immediate `send` works.
            relay_id: This service's name in a relay path. Defaults to `name`.
            max_relay_hops: Hops permitted before a relay is refused. Between 1 and 32.

        Returns:
            The registered destination.

        Raises:
            ValueError: The name is already registered, or the destination config is invalid.
        """
        if name in self._destinations:
            raise ValueError(f"duplicate webhook destination: {name}")
        destination = WebhookDestination(
            name,
            client=client,
            path=path,
            signer=signer,
            outbox=outbox,
            relay_id=name if relay_id is None else relay_id,
            max_relay_hops=max_relay_hops,
        )
        self._destinations[name] = destination
        return destination


__all__ = [
    "DispatcherReadiness",
    "GitHubWebhookVerifier",
    "HMACWebhookSigner",
    "HMACWebhookVerifier",
    "InboxClaim",
    "LocalReplayStore",
    "OutboxDelivery",
    "PostgresWebhookInbox",
    "PostgresWebhookOutbox",
    "StandardWebhookVerifier",
    "StripeWebhookVerifier",
    "WebhookContext",
    "WebhookDeliveryResult",
    "WebhookDestination",
    "WebhookDispatcher",
    "WebhookEnvelope",
    "WebhookHub",
    "WebhookLimits",
    "WebhookSource",
    "WebhookVerifier",
]
