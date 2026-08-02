"""The notification layer, and the two refusals that are easy to get backwards.

The transactional/marketing boundary is a legal question with a technical
encoding, and both wrong answers are expensive: treat everything as
transactional and you are a non-compliant bulk sender; treat everything as
marketing and a suppressed address stops receiving its own password resets. Both
directions are asserted here, by their distinct paths rather than by "a send
happened".
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from wreath._userkit import (
    CapturingEmailSender,
    InMemorySuppressionList,
    MailClass,
    Message,
    SmtpEmailSender,
    SuppressedError,
    SuppressionReason,
    Unsubscribe,
)
from wreath._webpush import PushError, PushResult, PushSubscription, VapidKeys
from wreath.notifications import (
    Email,
    InMemoryPushSubscriptions,
    Notifications,
    Recipient,
    WebPush,
)

SUBSCRIPTION = PushSubscription(
    "https://push.example.net/gone",
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "BTBZMqHH6r4Tts7J_aSIgg",
)


@dataclass
class PhotoShared:
    actor: str

    def title(self) -> str:
        return f"{self.actor} shared a photo"

    def navigate(self) -> str:
        return "/photos/1"


# --- the suppression boundary ------------------------------------------------


async def test_a_suppressed_address_still_receives_a_password_reset() -> None:
    """The whole point of the transactional class.

    Someone who marked a newsletter as spam is on the suppression list. They
    must still be able to reset their own password, or the complaint has locked
    them out of their account -- which reads to them as the product being
    broken, and to support as an outage.

    Asserted through the captured message rather than through "no exception":
    a sender that silently dropped it would also not raise.
    """
    suppression = InMemorySuppressionList()
    await suppression.suppress("locked.out@example.com", SuppressionReason.COMPLAINT)
    sender = CapturingEmailSender(suppression=suppression)

    await sender.send(
        Message(
            to="locked.out@example.com",
            subject="Reset your password",
            body="link",
            mail_class=MailClass.TRANSACTIONAL,
        )
    )
    assert [message.to for message in sender.messages] == ["locked.out@example.com"]


async def test_marketing_mail_to_a_suppressed_address_is_refused_by_reason() -> None:
    """And the other direction, with the reason in the message.

    Asserting only that it raised, or only that the address appears, would pass
    for any refusal -- including one that refused the transactional case too.
    The reason word is what distinguishes this branch from every other.
    """
    suppression = InMemorySuppressionList()
    await suppression.suppress("gone@example.com", SuppressionReason.COMPLAINT)
    sender = CapturingEmailSender(suppression=suppression)

    with pytest.raises(SuppressedError, match="complaint"):
        await sender.send(
            Message(
                to="gone@example.com",
                subject="Our newsletter",
                body="news",
                mail_class=MailClass.MARKETING,
                unsubscribe=Unsubscribe(url="https://example.com/u/1"),
            )
        )
    assert sender.messages == []


async def test_releasing_an_address_lets_marketing_through_again() -> None:
    suppression = InMemorySuppressionList()
    await suppression.suppress("back@example.com", SuppressionReason.UNSUBSCRIBED)
    await suppression.release("back@example.com")
    sender = CapturingEmailSender(suppression=suppression)
    await sender.send(
        Message(
            to="back@example.com",
            subject="Our newsletter",
            body="news",
            mail_class=MailClass.MARKETING,
            unsubscribe=Unsubscribe(url="https://example.com/u/1"),
        )
    )
    assert len(sender.messages) == 1


async def test_suppression_matching_ignores_case_and_padding() -> None:
    """An address entered with a capital is the same person who complained."""
    suppression = InMemorySuppressionList()
    await suppression.suppress("Person@Example.COM ", SuppressionReason.MANUAL)
    assert await suppression.reason("person@example.com") is SuppressionReason.MANUAL


# --- RFC 8058 ----------------------------------------------------------------


def test_marketing_without_an_unsubscribe_target_is_refused_at_construction() -> None:
    """Refused when the message is built, not when it is sent.

    The requirement is on the headers, so a message that cannot carry them is
    a programming error rather than a delivery failure discovered in a job.
    """
    with pytest.raises(ValueError, match="requires a one-click unsubscribe"):
        Message(to="a@b.c", subject="s", body="b", mail_class=MailClass.MARKETING)


def test_a_plain_http_unsubscribe_url_is_refused() -> None:
    """The provider POSTs to it unattended, so it has to be https."""
    with pytest.raises(ValueError, match="must be https"):
        Unsubscribe(url="http://example.com/u/1")


def test_marketing_mail_carries_both_rfc8058_headers() -> None:
    """A visible link in the body does not satisfy the requirement.

    Both headers or nothing: `List-Unsubscribe` alone gets no one-click
    affordance from Gmail, which is the entire point of adding it.
    """
    sender = SmtpEmailSender(host="localhost", from_addr="news@example.com")
    raw = sender.build(
        Message(
            to="reader@example.net",
            subject="Our newsletter",
            body="news",
            mail_class=MailClass.MARKETING,
            unsubscribe=Unsubscribe(url="https://example.com/u/1", mailto="mailto:u@example.com"),
        )
    ).decode()
    assert "List-Unsubscribe: <mailto:u@example.com>, <https://example.com/u/1>" in raw
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in raw


def test_transactional_mail_carries_no_unsubscribe_headers() -> None:
    """Operational mail is excluded, and adding them invites the wrong action."""
    sender = SmtpEmailSender(host="localhost", from_addr="accounts@example.com")
    raw = sender.build(
        Message(
            to="user@example.net",
            subject="Reset your password",
            body="link",
            mail_class=MailClass.TRANSACTIONAL,
        )
    ).decode()
    assert "List-Unsubscribe" not in raw


def test_every_message_carries_a_date_and_a_message_id() -> None:
    """Both are close to mandatory in practice and neither is automatic."""
    sender = SmtpEmailSender(host="localhost", from_addr="accounts@example.com")
    raw = sender.build(
        Message(to="u@example.net", subject="s", body="b", mail_class=MailClass.TRANSACTIONAL)
    ).decode()
    assert "\nDate: " in raw
    assert "@example.com>" in raw.split("Message-ID: ")[1]


# --- the layer ----------------------------------------------------------------


async def test_an_undeclared_notification_is_refused_rather_than_defaulted() -> None:
    """Because the default would have to guess the mail class."""
    notify = Notifications(channels=[Email(CapturingEmailSender())])
    with pytest.raises(LookupError, match="not a declared notification kind"):
        await notify.send(object(), to=Recipient("u1", email="u@example.com"))


def test_a_marketing_kind_without_an_unsubscribe_callable_is_refused() -> None:
    notify = Notifications(channels=[Email(CapturingEmailSender())])
    with pytest.raises(ValueError, match="needs unsubscribe="):
        notify.kind("news", mail_class=MailClass.MARKETING)(type("News", (), {}))


def test_declaring_one_name_twice_is_refused() -> None:
    """Two kinds with one name collapse each other's digests silently."""
    notify = Notifications(channels=[Email(CapturingEmailSender())])
    notify.kind("shared")(type("A", (), {}))
    with pytest.raises(ValueError, match="already declared"):
        notify.kind("shared")(type("B", (), {}))


async def test_a_repeat_inside_the_digest_window_is_collapsed() -> None:
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)])
    notify.kind("photo_shared", digest=3600)(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    first = await notify.send(PhotoShared("Ada"), to=recipient, now=1000.0)
    second = await notify.send(PhotoShared("Ada"), to=recipient, now=2000.0)
    third = await notify.send(PhotoShared("Ada"), to=recipient, now=5000.0)

    assert first.delivered == ("email",)
    assert second.deduplicated is True and second.delivered == ()
    assert third.delivered == ("email",)
    assert len(sender.messages) == 2


async def test_a_declined_preference_skips_only_that_channel() -> None:
    class NoEmail:
        async def allows(self, recipient: Recipient, kind: str, channel: str) -> bool:
            return channel != "email"

    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)], preferences=NoEmail())
    notify.kind("photo_shared")(PhotoShared)
    result = await notify.send(PhotoShared("Ada"), to=Recipient("u1", email="u@example.com"))
    assert result.declined == ("email",)
    assert sender.messages == []


async def test_one_channel_failing_does_not_stop_the_others() -> None:
    """A push outage must not withhold the email."""

    class Broken:
        name = "webpush"

        async def deliver(self, recipient: Recipient, note: object, kind: object) -> None:
            raise OSError("push service unreachable")

    sender = CapturingEmailSender()
    notify = Notifications(channels=[Broken(), Email(sender)])
    notify.kind("photo_shared")(PhotoShared)
    result = await notify.send(PhotoShared("Ada"), to=Recipient("u1", email="u@example.com"))

    assert result.delivered == ("email",)
    assert "webpush" in result.failed
    assert len(sender.messages) == 1


async def test_the_hourly_rate_limit_stops_a_notification_loop() -> None:
    """Counted, because a loop is invisible without a number.

    The inward abuse case: something writes a row, the write notifies, the
    notification writes a row. Nothing here raises, so only the counter shows it.
    """
    notify = Notifications(channels=[Email(CapturingEmailSender())], rate_limit=2)
    notify.kind("photo_shared")(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    outcomes = [await notify.send(PhotoShared(str(i)), to=recipient) for i in range(5)]
    assert [bool(outcome.delivered) for outcome in outcomes] == [True, True, False, False, False]
    assert notify.rate_limited == 3


async def test_a_recipient_with_no_address_fails_only_the_email_channel() -> None:
    notify = Notifications(channels=[Email(CapturingEmailSender())])
    notify.kind("photo_shared")(PhotoShared)
    result = await notify.send(PhotoShared("Ada"), to=Recipient("u1"))
    assert "email" in result.failed
    assert "no email address" in result.failed["email"]


# --- push subscription pruning ------------------------------------------------


async def test_a_410_from_the_push_service_removes_the_subscription() -> None:
    """Required by RFC 8030, and the reason a push store does not leak.

    `410 Gone` means the subscription is permanently dead. A store that never
    prunes accumulates them, and every send afterwards spends an ECDH and an
    HTTP round trip to be told the same thing -- a rising error rate nobody
    attributes to anything.
    """
    subscriptions = InMemoryPushSubscriptions()
    await subscriptions.add("u1", SUBSCRIPTION)

    async def gone(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        return PushResult(410, expired=True, detail="Gone")

    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=gone)
    notify = Notifications(channels=[channel])
    notify.kind("photo_shared")(PhotoShared)

    result = await notify.send(PhotoShared("Ada"), to=Recipient("u1"))
    assert result.failed == {}
    assert await subscriptions.for_recipient("u1") == ()


async def test_a_404_also_removes_the_subscription() -> None:
    subscriptions = InMemoryPushSubscriptions()
    await subscriptions.add("u1", SUBSCRIPTION)

    async def missing(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        return PushResult(404, expired=True)

    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=missing)
    await channel.deliver(Recipient("u1"), PhotoShared("Ada"), object())
    assert await subscriptions.for_recipient("u1") == ()


async def test_a_500_keeps_the_subscription_and_raises_for_the_job_to_retry() -> None:
    """A server error is transient; deleting on one would lose real recipients."""
    subscriptions = InMemoryPushSubscriptions()
    await subscriptions.add("u1", SUBSCRIPTION)

    async def broken(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        return PushResult(500, expired=False, detail="upstream error")

    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=broken)
    notify = Notifications(channels=[channel])
    notify.kind("photo_shared")(PhotoShared)

    result = await notify.send(PhotoShared("Ada"), to=Recipient("u1"))
    assert "webpush" in result.failed
    assert len(await subscriptions.for_recipient("u1")) == 1


async def test_a_successful_push_carries_the_vapid_and_encoding_headers() -> None:
    subscriptions = InMemoryPushSubscriptions()
    await subscriptions.add("u1", SUBSCRIPTION)
    seen: dict[str, str] = {}

    async def capture(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        seen.update(headers)
        seen["_len"] = str(len(body))
        return PushResult(201, expired=False)

    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=capture)
    await channel.deliver(Recipient("u1"), PhotoShared("Ada"), object())

    assert seen["Content-Encoding"] == "aes128gcm"
    assert seen["Authorization"].startswith("vapid t=")
    assert ", k=" in seen["Authorization"]
    assert int(seen["_len"]) > 86  # header plus a non-empty ciphertext


async def test_an_oversized_notification_is_refused_before_it_is_encrypted() -> None:
    """Refused at the channel, naming the size, not deep in the crypto.

    The 4096-byte cap is on the *encrypted* body, so a payload near the limit
    fails inside `encrypt` with a message about ciphertext. Checking it here
    means the error names the notification, which is the thing the author can
    actually make smaller.
    """
    subscriptions = InMemoryPushSubscriptions()
    await subscriptions.add("u1", SUBSCRIPTION)

    @dataclass
    class Huge:
        def title(self) -> str:
            return "x" * 5000

        def navigate(self) -> str:
            return "/"

    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions)
    with pytest.raises(PushError, match="over the 4096-byte push limit"):
        await channel.deliver(Recipient("u1"), Huge(), object())


async def test_one_pooled_client_is_reused_per_push_service() -> None:
    """Push endpoints span origins; `HTTPClient` is bound to one for its life.

    Building a client per message throws away the connection pooling that is the
    reason to use it, so the delivery helper keeps one per origin. Asserted
    without opening a socket: constructing a client connects to nothing.
    """
    from wreath.notifications import PushDelivery

    delivery = PushDelivery()
    made: list[str] = []

    class FakeClient:
        def __init__(self, *, name: str, base_url: str, **_: object) -> None:
            made.append(base_url)

        async def post(self, target: str, *, headers: object, body: bytes) -> object:
            return type("R", (), {"status": 201, "body": b""})()

        async def close(self) -> None: ...

    import wreath.http_client as http_client

    original = http_client.HTTPClient
    http_client.HTTPClient = FakeClient  # type: ignore[misc]
    try:
        await delivery.send("https://fcm.example/a", b"x", {})
        await delivery.send("https://fcm.example/b", b"x", {})
        await delivery.send("https://mozilla.example/c", b"x", {})
    finally:
        http_client.HTTPClient = original  # type: ignore[misc]
        await delivery.aclose()

    assert made == ["https://fcm.example", "https://mozilla.example"]


async def test_a_kind_restricted_with_only_skips_the_other_channels() -> None:
    """`only=` is how a noisy kind stays out of somebody's inbox.

    Without it the choice is per-recipient preference or nothing, and "this
    particular event is push-only" is a property of the event, not the person.
    """
    sender = CapturingEmailSender()
    subscriptions = InMemoryPushSubscriptions()
    await subscriptions.add("u1", SUBSCRIPTION)
    seen: list[str] = []

    async def accept(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        seen.append(endpoint)
        return PushResult(201, expired=False)

    push = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=accept)
    notify = Notifications(channels=[Email(sender), push])
    notify.kind("photo_shared", only=("webpush",))(PhotoShared)

    result = await notify.send(PhotoShared("Ada"), to=Recipient("u1", email="u@example.com"))
    assert result.delivered == ("webpush",)
    assert sender.messages == []
    assert len(seen) == 1


async def test_a_kind_with_no_digest_window_sends_every_time() -> None:
    """The default. Collapsing by default would silently lose notifications."""
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)])
    notify.kind("photo_shared")(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    for _ in range(3):
        await notify.send(PhotoShared("Ada"), to=recipient, now=1000.0)
    assert len(sender.messages) == 3
    assert notify.deduplicated == 0


async def test_rate_limit_zero_disables_the_cap() -> None:
    """An explicit opt-out, for a fan-out that is legitimately large."""
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)], rate_limit=0)
    notify.kind("photo_shared")(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    for index in range(12):
        await notify.send(PhotoShared(str(index)), to=recipient)
    assert len(sender.messages) == 12
    assert notify.rate_limited == 0


async def test_the_digest_window_is_only_armed_by_a_delivery() -> None:
    """A send that reached nobody must not start the window.

    Otherwise the first failure silences the kind for an hour, and the recipient
    never learns about the thing that failed to reach them.
    """

    class Broken:
        name = "email"

        async def deliver(self, recipient: Recipient, note: object, kind: object) -> None:
            raise OSError("transport down")

    notify = Notifications(channels=[Broken()])
    notify.kind("photo_shared", digest=3600)(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    first = await notify.send(PhotoShared("Ada"), to=recipient, now=1000.0)
    second = await notify.send(PhotoShared("Ada"), to=recipient, now=1001.0)
    assert first.delivered == () and "email" in first.failed
    assert second.deduplicated is False


async def test_a_marketing_kind_with_an_unsubscribe_delivers_and_carries_the_headers() -> None:
    """The positive case, which the refusal tests alone do not establish.

    A guard that refused *every* marketing kind would pass all of those and this
    is what catches it -- and it also pins that the per-recipient unsubscribe
    callable reaches the message, so the link can carry a per-person token.
    """
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)])

    @notify.kind(
        "product_news",
        mail_class=MailClass.MARKETING,
        unsubscribe=lambda r: Unsubscribe(url=f"https://example.com/u/{r.key}"),
    )
    @dataclass
    class ProductNews:
        headline: str

        def title(self) -> str:
            return self.headline

    result = await notify.send(ProductNews("New in August"), to=Recipient("u7", email="r@e.com"))

    assert result.delivered == ("email",)
    message = sender.messages[0]
    assert message.mail_class is MailClass.MARKETING
    assert message.unsubscribe is not None
    assert message.unsubscribe.url == "https://example.com/u/u7"
