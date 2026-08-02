"""Notifications: one declaration, several channels, one preference per person.

Every application eventually rebuilds the same paragraph of behaviour -- *email
me immediately, push me only when I am away, never tell me the same thing
twice, and batch the noisy ones* -- and rebuilds it badly, because it started
from a `send_email(...)` call at the point where something happened.

The fix is upstream of the channels. A notification here is a **kind**, declared
once with a name and a delivery policy, and sent as an instance of that kind:

```python
notify = Notifications(channels=[Email(sender), WebPush(keys, subscriptions)])

@notify.kind("photo_shared", digest=hours(1))
@dataclass
class PhotoShared:
    photo_id: str
    actor: str

    def title(self) -> str:
        return f"{self.actor} shared a photo with you"

await notify.send(PhotoShared(photo.id, actor.name), to=recipient)
```

That declaration is what makes the rest possible at all. A kind has a stable
identity, so two of them can be recognised as the same thing and collapsed; a
stream of bare `send` calls cannot be, which is why applications that start
there never get deduplication and never get digests.

**Delivery is a `wreath.jobs` job**, one per channel per recipient, so retries,
backoff and dead-lettering are the ones wreath already has rather than a second
set. Nothing here retries in-process.

## What this is not

Not an ESP. Warm-up scheduling, reputation management, per-domain throttling and
IP rotation are a product, and a framework that starts down that road ends up
maintaining a deliverability team's worth of heuristics. Wreath's job is to emit
correct, signed, unsubscribable mail through whatever transport is configured --
see `wreath.doctor.check_email_deliverability` for the part that tells you when
the DNS does not back it up.

Reference: `wreath.users` for the sending side of account mail, and
`wreath.doctor` for the deliverability check.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ._userkit import MailClass, Message, SuppressedError, Unsubscribe
from ._webpush import (
    MAX_PAYLOAD_BYTES,
    PushError,
    PushResult,
    PushSubscription,
    VapidKeys,
    declarative_payload,
    encrypt,
    vapid_headers,
)

__all__ = [
    "Channel",
    "Email",
    "InApp",
    "Notifications",
    "PushDelivery",
    "PushError",
    "PushResult",
    "PushSubscription",
    "PushSubscriptions",
    "Recipient",
    "SendResult",
    "VapidKeys",
    "WebPush",
]


@dataclass(frozen=True, slots=True)
class Recipient:
    """Who a notification is for, and how they can be reached.

    `key` is the stable identifier deduplication and digesting are keyed on, so
    it must not change when an address does.
    """

    key: str
    email: str | None = None
    #: Opaque to this module. `Notifications` passes it to the preference
    #: callable, which is where an application's own idea of a person lives.
    principal: Any = None


@runtime_checkable
class Preferences(Protocol):
    """Whether a recipient wants a kind of notification on a channel.

    Deliberately a protocol with one method rather than a table this module
    owns. Preferences are authorization-adjacent -- "this person has opted out
    of this category" is a fact a policy engine can hold and a permission
    manifest can expose -- and the composed-principal work is where that
    belongs. Implement this over whatever holds it today; the integration point
    when principal facts land is this one method.
    """

    async def allows(self, recipient: Recipient, kind: str, channel: str) -> bool:
        """Whether `recipient` accepts `kind` on `channel`."""
        ...


class AllowAll:
    """The default: every kind on every channel.

    A permissive default is right *here* and wrong for marketing mail, and the
    difference is enforced one layer down: `MailClass.MARKETING` consults the
    suppression list in `wreath._userkit` regardless of what this says.
    """

    async def allows(self, recipient: Recipient, kind: str, channel: str) -> bool:
        """Always `True`."""
        return True


@runtime_checkable
class Channel(Protocol):
    """One way of reaching someone."""

    @property
    def name(self) -> str:
        """The channel's name, as preferences and `only=` refer to it."""
        ...

    async def deliver(self, recipient: Recipient, note: Any, kind: KindSpec) -> None:
        """Deliver one notification, or raise. Retries belong to the caller."""
        ...


@dataclass(frozen=True, slots=True)
class KindSpec:
    """The policy attached to one declared kind."""

    name: str
    #: How long to collapse repeats of this kind for the same recipient into
    #: one delivery. `0` sends every one.
    digest: float = 0.0
    #: Whether this is operational or promotional mail. No default at the
    #: `Message` layer, and none inherited silently here either: a kind that
    #: does not say is transactional only because most notifications are, and
    #: `MailClass.MARKETING` is what a digest of product news must declare.
    mail_class: MailClass = MailClass.TRANSACTIONAL
    #: Required for a marketing kind, and built per recipient so the link can
    #: carry a per-person token.
    unsubscribe: Callable[[Recipient], Unsubscribe] | None = None
    #: Deliver only on these channels, by name. Empty means all of them.
    only: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SendResult:
    """What one `send` did, per channel."""

    delivered: tuple[str, ...] = ()
    #: Channels skipped because the recipient's preferences declined them.
    declined: tuple[str, ...] = ()
    #: Skipped because an identical notification was sent inside the digest
    #: window.
    deduplicated: bool = False
    #: Channel name to the error it raised. A channel failing does not stop the
    #: others: an unreachable push service must not cost someone their email.
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def reached(self) -> bool:
        return bool(self.delivered)


class Notifications:
    """Declares kinds, applies preferences, and fans out to channels.

    Args:
        channels: The channels to deliver on, in order.
        preferences: Per-recipient opt-outs. Permissive by default.
        enqueue: How to run a delivery. Defaults to awaiting it inline, which is
            right for a test and wrong for production -- pass
            `jobs.enqueue`-shaped callable so retries and dead-lettering are the
            durable ones.
        rate_limit: The most notifications one recipient may be sent per hour,
            across all kinds. `0` disables the cap.
    """

    def __init__(
        self,
        channels: Sequence[Channel],
        *,
        preferences: Preferences | None = None,
        enqueue: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]] | None = None,
        rate_limit: int = 200,
    ) -> None:
        self._channels = tuple(channels)
        self._preferences = preferences or AllowAll()
        self._enqueue = enqueue
        self._rate_limit = rate_limit
        self._kinds: dict[type, KindSpec] = {}
        self._recent: dict[tuple[str, str], float] = {}
        self._counts: dict[str, list[float]] = {}
        #: Counted rather than logged and forgotten. A rising `rate_limited`
        #: with a flat `delivered` is a notification loop -- something writing a
        #: row whose notification writes a row -- and it is invisible without a
        #: number.
        self.delivered = 0
        self.deduplicated = 0
        self.rate_limited = 0
        self.declined = 0

    def kind(
        self,
        name: str,
        *,
        digest: float = 0.0,
        mail_class: MailClass = MailClass.TRANSACTIONAL,
        unsubscribe: Callable[[Recipient], Unsubscribe] | None = None,
        only: Iterable[str] = (),
    ) -> Callable[[type], type]:
        """Declare a notification kind. Use as a class decorator.

        Raises:
            ValueError: `name` is already declared, or a marketing kind was
                declared without a way to unsubscribe from it -- which is the
                RFC 8058 requirement, refused here rather than at send time so
                it is a startup error instead of a delivery failure.
        """
        if any(spec.name == name for spec in self._kinds.values()):
            raise ValueError(f"notification kind {name!r} is already declared")
        if mail_class is MailClass.MARKETING and unsubscribe is None:
            raise ValueError(
                f"marketing kind {name!r} needs unsubscribe=, a callable returning an "
                "Unsubscribe for a recipient; RFC 8058 one-click unsubscribe is not "
                "optional for promotional mail"
            )
        spec = KindSpec(name, digest, mail_class, unsubscribe, tuple(only))

        def declare(cls: type) -> type:
            self._kinds[cls] = spec
            return cls

        return declare

    def spec_for(self, note: Any) -> KindSpec:
        """The declared policy for `note`.

        Raises:
            LookupError: `note`'s type was never declared with `kind`. Refused
                rather than defaulted, because the default would have to guess
                the mail class, and both wrong guesses are expensive.
        """
        spec = self._kinds.get(type(note))
        if spec is None:
            raise LookupError(
                f"{type(note).__name__} is not a declared notification kind; decorate "
                "it with @notify.kind(...) so it has a name, a mail class and a digest "
                "window"
            )
        return spec

    async def send(
        self, note: Any, *, to: Recipient, now: float | None = None
    ) -> SendResult:
        """Deliver `note` to one recipient on every channel they allow."""
        spec = self._spec_and_clock(note)
        moment = time.time() if now is None else now
        if self._is_duplicate(spec, to, moment):
            self.deduplicated += 1
            return SendResult(deduplicated=True)
        if not self._within_rate_limit(to, moment):
            self.rate_limited += 1
            return SendResult(failed={"*": "rate limit exceeded for this recipient"})

        delivered: list[str] = []
        declined: list[str] = []
        failed: dict[str, str] = {}
        for channel in self._channels:
            if spec.only and channel.name not in spec.only:
                continue
            if not await self._preferences.allows(to, spec.name, channel.name):
                declined.append(channel.name)
                continue
            try:
                await self._run(channel, to, note, spec)
            except (OSError, PushError, SuppressedError, ValueError) as exc:
                # Per channel, and named. One channel failing must not cost the
                # others: a push service outage is not a reason to withhold the
                # email, and the two failures have different causes and
                # different fixes.
                failed[channel.name] = f"{type(exc).__name__}: {exc}"
            else:
                delivered.append(channel.name)
        if delivered:
            self.delivered += 1
            self._recent[(spec.name, to.key)] = moment
            self._counts.setdefault(to.key, []).append(moment)
        self.declined += len(declined)
        return SendResult(tuple(delivered), tuple(declined), False, failed)

    def _spec_and_clock(self, note: Any) -> KindSpec:
        return self.spec_for(note)

    async def _run(self, channel: Channel, to: Recipient, note: Any, spec: KindSpec) -> None:
        if self._enqueue is None:
            await channel.deliver(to, note, spec)
            return

        async def job() -> None:
            await channel.deliver(to, note, spec)

        await self._enqueue(job)

    def _is_duplicate(self, spec: KindSpec, to: Recipient, moment: float) -> bool:
        if spec.digest <= 0:
            return False
        last = self._recent.get((spec.name, to.key))
        return last is not None and moment - last < spec.digest

    def _within_rate_limit(self, to: Recipient, moment: float) -> bool:
        """Whether this recipient is under the hourly cap.

        The window is trimmed on read rather than swept on a timer, so a
        recipient nobody notifies costs nothing and the table cannot grow a
        background task of its own.
        """
        if self._rate_limit <= 0:
            return True
        window = self._counts.setdefault(to.key, [])
        cutoff = moment - 3600
        while window and window[0] < cutoff:
            window.pop(0)
        return len(window) < self._rate_limit


# --- channels ---------------------------------------------------------------


@runtime_checkable
class MessageSender(Protocol):
    """The part of `wreath._userkit.EmailSender` this channel needs."""

    async def send(self, message: Message) -> None:
        """Deliver one message."""
        ...


@dataclass(slots=True)
class Email:
    """Deliver by email through a `wreath.users` sender.

    The message body comes from the notification itself: a kind may define
    `subject()` and `body()`, and falls back to its `title()` or its `repr`.
    """

    sender: MessageSender
    name: str = "email"

    async def deliver(self, recipient: Recipient, note: Any, kind: KindSpec) -> None:
        """Build a `Message` for `note` and hand it to the sender.

        Raises:
            ValueError: the recipient has no email address.
        """
        if not recipient.email:
            raise ValueError(f"recipient {recipient.key!r} has no email address")
        unsubscribe = kind.unsubscribe(recipient) if kind.unsubscribe else None
        await self.sender.send(
            Message(
                to=recipient.email,
                subject=_text(note, "subject") or _text(note, "title") or kind.name,
                body=_text(note, "body") or _text(note, "title") or repr(note),
                mail_class=kind.mail_class,
                unsubscribe=unsubscribe,
            )
        )


@runtime_checkable
class PushSubscriptions(Protocol):
    """Where a recipient's push subscriptions live."""

    async def for_recipient(self, key: str) -> Sequence[PushSubscription]:
        """Every subscription registered for this recipient."""
        ...

    async def remove(self, endpoint: str) -> None:
        """Forget the subscription with this endpoint.

        Called when a push service answers 404 or 410. **Required**: a store
        that never prunes becomes a slow leak plus an error rate nobody
        attributes, and the push services expect the deletion.
        """
        ...


@dataclass(slots=True)
class InMemoryPushSubscriptions:
    """A dict-backed subscription store for development and tests."""

    _by_recipient: dict[str, list[PushSubscription]] = field(default_factory=dict)

    async def for_recipient(self, key: str) -> Sequence[PushSubscription]:
        """Every subscription registered for `key`."""
        return tuple(self._by_recipient.get(key, ()))

    async def add(self, key: str, subscription: PushSubscription) -> None:
        """Register a subscription, replacing one with the same endpoint."""
        entries = self._by_recipient.setdefault(key, [])
        entries[:] = [e for e in entries if e.endpoint != subscription.endpoint]
        entries.append(subscription)

    async def remove(self, endpoint: str) -> None:
        """Forget every subscription with this endpoint."""
        for entries in self._by_recipient.values():
            entries[:] = [e for e in entries if e.endpoint != endpoint]


@dataclass(slots=True)
class WebPush:
    """Deliver by Web Push, encrypted per RFC 8291.

    Args:
        keys: The application server's VAPID identity.
        subscriptions: Where recipients' subscriptions live.
        post: How to POST a body. Defaults to `wreath.http_client`; injectable
            so a test never opens a socket.
        ttl: How long a push service should hold an undelivered message.
    """

    keys: VapidKeys
    subscriptions: PushSubscriptions
    post: Callable[..., Awaitable[PushResult]] | None = None
    ttl: int = 3600
    name: str = "webpush"

    async def deliver(self, recipient: Recipient, note: Any, kind: KindSpec) -> None:
        """Encrypt and send to every subscription this recipient has.

        A `404` or `410` prunes the subscription and is **not** an error: the
        endpoint is permanently gone, which is information rather than a
        failure. Anything else raises, so the caller's job retries it.
        """
        payload = _push_payload(note)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise PushError(
                f"the notification is {len(payload)} bytes before encryption, over the "
                f"{MAX_PAYLOAD_BYTES}-byte push limit; send an identifier and let the "
                "client fetch the detail"
            )
        errors: list[str] = []
        for subscription in await self.subscriptions.for_recipient(recipient.key):
            body = encrypt(subscription, payload)
            headers = {
                **vapid_headers(self.keys, subscription.endpoint),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(self.ttl),
            }
            result = await self._send(subscription.endpoint, body, headers)
            if result.expired:
                await self.subscriptions.remove(subscription.endpoint)
            elif not result.delivered:
                errors.append(f"{subscription.endpoint}: {result.status} {result.detail}")
        if errors:
            raise PushError("; ".join(errors))

    async def _send(self, endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        if self.post is None:
            self.post = PushDelivery().send
        return await self.post(endpoint, body, headers)


@dataclass(slots=True)
class InApp:
    """Deliver into a live `wreath.rooms` room, for an in-product inbox."""

    rooms: Any
    name: str = "inapp"

    async def deliver(self, recipient: Recipient, note: Any, kind: KindSpec) -> None:
        """Publish to the room named for this recipient."""
        await self.rooms.publish(
            f"notifications:{recipient.key}",
            _text(note, "body") or _text(note, "title") or repr(note),
        )


def _text(note: Any, attribute: str) -> str:
    """Call `note.<attribute>()` if it exists, else read it, else empty."""
    value = getattr(note, attribute, None)
    if value is None:
        return ""
    return str(value() if callable(value) else value)


def _push_payload(note: Any) -> bytes:
    """A Declarative Web Push document for `note`, or its own bytes.

    A kind may define `push()` returning bytes to take full control. Otherwise a
    declarative payload is built from `title`, `body` and `navigate` -- the
    format Safari displays with no service worker involved.
    """
    own = getattr(note, "push", None)
    if callable(own):
        payload = own()
        if isinstance(payload, bytes):
            return payload
    return declarative_payload(
        _text(note, "title") or type(note).__name__,
        body=_text(note, "body"),
        navigate=_text(note, "navigate") or "/",
    )


class PushDelivery:
    """Posts encrypted pushes, one pooled `HTTPClient` per push service.

    `wreath.http_client.HTTPClient` is bound to a single origin for its life,
    which is what lets it pool connections and hold a rate policy. Push
    endpoints do not come from one origin -- a recipient list reaches Google,
    Mozilla and Apple in the same fan-out -- so this keeps a client per origin
    and reuses it, rather than building one per message and throwing away the
    connection that made pooling worth having.

    Args:
        limits: Passed through to each client.
    """

    def __init__(self, **limits: Any) -> None:
        self._clients: dict[str, Any] = {}
        self._limits = limits

    async def send(self, endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        """POST one encrypted payload and classify the answer."""
        from .http_client import ClientError, HTTPClient

        scheme, _, rest = endpoint.partition("://")
        authority, _, path = rest.partition("/")
        origin = f"{scheme}://{authority}"
        client = self._clients.get(origin)
        if client is None:
            client = HTTPClient(name=f"webpush:{authority}", base_url=origin, **self._limits)
            self._clients[origin] = client
        wire = tuple(
            (name.encode("ascii"), value.encode("ascii")) for name, value in headers.items()
        )
        try:
            response = await client.post(f"/{path}", headers=wire, body=body)
        except ClientError as exc:
            # Reachability failures are transient by definition, so they are not
            # `expired`: deleting a subscription because a push service was
            # briefly unreachable loses a real recipient permanently.
            return PushResult(0, expired=False, detail=f"{type(exc).__name__}: {exc}")
        return PushResult(
            response.status,
            expired=response.status in (404, 410),
            detail=response.body[:200].decode("utf-8", "replace"),
        )

    async def aclose(self) -> None:
        """Close every pooled client."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
