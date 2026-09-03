from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

import pytest

from wreath import notifications
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
    Chat,
    Email,
    InApp,
    InMemoryPushSubscriptions,
    KindSpec,
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


async def test_in_app_delivery_uses_the_room_broadcast_contract() -> None:
    class Rooms:
        def __init__(self) -> None:
            self.calls = []

        async def broadcast(self, room: str, message: str) -> None:
            self.calls.append((room, message))

    rooms = Rooms()
    await InApp(rooms).deliver(Recipient("u1"), PhotoShared("Ada"), object())
    assert rooms.calls == [("notifications:u1", "Ada shared a photo")]


async def test_chat_delivery_reuses_chatops_tenant_and_idempotency_contract() -> None:
    class ChatOps:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def send(self, **values: object) -> None:
            self.calls.append(values)

    @dataclass(frozen=True)
    class Destination:
        tenant: str

    chat = ChatOps()
    channel = Chat(
        chat,
        destination=lambda recipient: Destination(f"slack:{recipient.key}"),
    )
    note = PhotoShared("Ada")

    await channel.deliver(Recipient("T123"), note, type("Kind", (), {"name": "shared"})())

    assert chat.calls[0]["tenant"] == "slack:T123"
    assert chat.calls[0]["content"] == "Ada shared a photo"
    assert isinstance(chat.calls[0]["idempotency_key"], str)
    assert len(chat.calls[0]["idempotency_key"]) == 64


@dataclass(frozen=True)
class ChatDestination:
    tenant: object


class RecordingChat:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send(self, **values: object) -> None:
        self.calls.append(values)


async def test_chat_delivery_awaits_async_destination() -> None:
    async def destination(recipient: Recipient) -> ChatDestination:
        return ChatDestination(f"tenant:{recipient.key}")

    chat = RecordingChat()
    channel = Chat(chat, destination=destination)

    await channel.deliver(
        Recipient("u1"),
        PhotoShared("Ada"),
        type("Kind", (), {"name": "shared"})(),
    )

    assert chat.calls[0]["tenant"] == "tenant:u1"


@pytest.mark.parametrize("tenant", [None, "", 1, b"tenant"])
async def test_chat_delivery_requires_nonempty_text_tenant(tenant: object) -> None:
    channel = Chat(RecordingChat(), destination=lambda _recipient: ChatDestination(tenant))

    with pytest.raises(ValueError, match="non-empty tenant"):
        await channel.deliver(
            Recipient("u1"),
            PhotoShared("Ada"),
            type("Kind", (), {"name": "shared"})(),
        )


async def test_chat_delivery_uses_custom_renderer() -> None:
    chat = RecordingChat()
    channel = Chat(
        chat,
        destination=lambda _recipient: ChatDestination("tenant"),
        render=lambda _note: "custom rendering",
    )

    await channel.deliver(
        Recipient("u1"),
        PhotoShared("Ada"),
        type("Kind", (), {"name": "shared"})(),
    )

    assert chat.calls[0]["content"] == "custom rendering"


@pytest.mark.parametrize("rendered", [None, "", 1, b"content"])
async def test_chat_delivery_requires_nonempty_text_rendering(rendered: object) -> None:
    channel = Chat(
        RecordingChat(),
        destination=lambda _recipient: ChatDestination("tenant"),
        render=lambda _note: rendered,
    )

    with pytest.raises(ValueError, match="content must be non-empty text"):
        await channel.deliver(
            Recipient("u1"),
            PhotoShared("Ada"),
            type("Kind", (), {"name": "shared"})(),
        )


async def test_chat_delivery_prefers_body_to_title() -> None:
    note = type("Note", (), {"body": "body", "title": "title"})()
    chat = RecordingChat()
    channel = Chat(chat, destination=lambda _recipient: ChatDestination("tenant"))

    await channel.deliver(
        Recipient("u1"),
        note,
        type("Kind", (), {"name": "shared"})(),
    )

    assert chat.calls[0]["content"] == "body"


async def test_chat_delivery_falls_back_to_note_representation() -> None:
    class Note:
        def __repr__(self) -> str:
            return "represented note"

    chat = RecordingChat()
    channel = Chat(chat, destination=lambda _recipient: ChatDestination("tenant"))

    await channel.deliver(
        Recipient("u1"),
        Note(),
        type("Kind", (), {"name": "shared"})(),
    )

    assert chat.calls[0]["content"] == "represented note"


async def test_a_suppressed_address_still_receives_a_password_reset() -> None:
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
    suppression = InMemorySuppressionList()
    await suppression.suppress("Person@Example.COM ", SuppressionReason.MANUAL)
    assert await suppression.reason("person@example.com") is SuppressionReason.MANUAL


def test_marketing_without_an_unsubscribe_target_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="requires a one-click unsubscribe"):
        Message(to="a@b.c", subject="s", body="b", mail_class=MailClass.MARKETING)


def test_a_plain_http_unsubscribe_url_is_refused() -> None:
    with pytest.raises(ValueError, match="must be https"):
        Unsubscribe(url="http://example.com/u/1")


def test_marketing_mail_carries_both_rfc8058_headers() -> None:
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
    sender = SmtpEmailSender(host="localhost", from_addr="accounts@example.com")
    raw = sender.build(
        Message(to="u@example.net", subject="s", body="b", mail_class=MailClass.TRANSACTIONAL)
    ).decode()
    assert "\nDate: " in raw
    assert "@example.com>" in raw.split("Message-ID: ")[1]


async def test_an_undeclared_notification_is_refused_rather_than_defaulted() -> None:
    notify = Notifications(channels=[Email(CapturingEmailSender())])
    with pytest.raises(LookupError, match="not a declared notification kind"):
        await notify.send(object(), to=Recipient("u1", email="u@example.com"))


def test_a_marketing_kind_without_an_unsubscribe_callable_is_refused() -> None:
    notify = Notifications(channels=[Email(CapturingEmailSender())])
    with pytest.raises(ValueError, match="needs unsubscribe="):
        notify.kind("news", mail_class=MailClass.MARKETING)(type("News", (), {}))


def test_declaring_one_name_twice_is_refused() -> None:
    notify = Notifications(channels=[Email(CapturingEmailSender())])
    notify.kind("shared")(type("A", (), {}))
    with pytest.raises(ValueError, match="already declared"):
        notify.kind("shared")(type("B", (), {}))


def test_redeclaring_same_class_releases_its_previous_kind_name() -> None:
    notify = Notifications(channels=[])

    notify.kind("old-name")(PhotoShared)
    notify.kind("new-name")(PhotoShared)
    replacement = notify.kind("old-name")(type("Replacement", (), {}))

    assert notify.spec_for(PhotoShared("Ada")).name == "new-name"
    assert notify.spec_for(replacement()).name == "old-name"


async def test_fatal_channel_exception_is_not_converted_to_delivery_failure() -> None:
    class FatalDelivery(BaseException):
        pass

    class FatalChannel:
        name = "fatal"

        async def deliver(self, recipient: Recipient, note: object, kind: object) -> None:
            raise FatalDelivery

    notify = Notifications(channels=[FatalChannel()])
    notify.kind("photo_shared")(PhotoShared)

    with pytest.raises(FatalDelivery):
        await notify.send(PhotoShared("Ada"), to=Recipient("u1"))


def test_digest_sweep_preserves_newer_deadline_for_same_key() -> None:
    notify = Notifications(channels=[])
    key = ("shared", "u1")
    notify._recent[key] = 200
    notify._recent_expirations = [(100, 1, key)]
    spec = KindSpec("shared", 60, MailClass.TRANSACTIONAL, None, ())

    assert notify._is_duplicate(spec, Recipient("u1"), 150) is True
    assert notify._recent == {key: 200}


def test_rate_sweep_ignores_expiration_for_missing_recipient() -> None:
    notify = Notifications(channels=[], rate_limit=2)
    notify._count_expirations = [(100, 1, "missing")]

    notify._sweep_rate_windows(200)

    assert notify._counts == {}


def test_rate_sweep_ignores_stale_deadline_for_existing_recipient() -> None:
    notify = Notifications(channels=[], rate_limit=2)
    notify._counts["u1"] = deque([1000])
    notify._count_expirations = [(200, 1, "u1")]

    notify._sweep_rate_windows(300)

    assert notify._counts["u1"] == deque([1000])
    assert notify._count_expirations == []


def test_rate_sweep_removes_only_expired_events_and_reschedules_window() -> None:
    notify = Notifications(channels=[], rate_limit=2)
    notify._counts["u1"] = deque([100, 2000])
    notify._count_expirations = [(3700, 1, "u1")]

    notify._sweep_rate_windows(4000)

    assert notify._counts["u1"] == deque([2000])
    assert notify._count_expirations == [(5600, 1, "u1")]


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


async def test_one_slow_channel_does_not_block_the_other_channels() -> None:
    release = asyncio.Event()
    fast_delivered = asyncio.Event()

    class Slow:
        name = "slow"

        async def deliver(self, recipient: Recipient, note: object, kind: object) -> None:
            await release.wait()

    class Fast:
        name = "fast"

        async def deliver(self, recipient: Recipient, note: object, kind: object) -> None:
            fast_delivered.set()

    notify = Notifications(channels=[Slow(), Fast()])
    notify.kind("photo_shared")(PhotoShared)
    sending = asyncio.create_task(notify.send(PhotoShared("Ada"), to=Recipient("u1")))
    await asyncio.wait_for(fast_delivered.wait(), timeout=0.5)
    release.set()

    result = await sending
    assert result.delivered == ("slow", "fast")


async def test_the_hourly_rate_limit_stops_a_notification_loop() -> None:
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


async def test_a_410_from_the_push_service_removes_the_subscription() -> None:
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


async def test_subscription_removal_does_not_walk_unrelated_recipients() -> None:
    class NoValueWalk(dict):
        def values(self):
            raise AssertionError("every recipient was walked")

    subscriptions = InMemoryPushSubscriptions()
    await subscriptions.add("u1", SUBSCRIPTION)
    subscriptions._by_recipient = NoValueWalk(subscriptions._by_recipient)

    await subscriptions.remove(SUBSCRIPTION.endpoint)

    assert await subscriptions.for_recipient("u1") == ()


async def test_a_500_keeps_the_subscription_and_raises_for_the_job_to_retry() -> None:
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


async def test_one_slow_push_endpoint_does_not_block_another_subscription() -> None:
    subscriptions = InMemoryPushSubscriptions()
    second = PushSubscription(
        "https://push.example.net/second",
        SUBSCRIPTION.p256dh,
        SUBSCRIPTION.auth,
    )
    await subscriptions.add("u1", SUBSCRIPTION)
    await subscriptions.add("u1", second)
    release = asyncio.Event()
    second_started = asyncio.Event()

    async def post(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        if endpoint == SUBSCRIPTION.endpoint:
            await release.wait()
        else:
            second_started.set()
        return PushResult(201, expired=False)

    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=post)
    delivery = asyncio.create_task(channel.deliver(Recipient("u1"), PhotoShared("Ada"), object()))
    await asyncio.wait_for(second_started.wait(), timeout=0.5)
    release.set()
    await delivery


async def test_web_push_reraises_fatal_delivery_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FatalPush(BaseException):
        pass

    subscriptions = InMemoryPushSubscriptions()
    await subscriptions.add("u1", SUBSCRIPTION)

    async def post(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        raise FatalPush

    monkeypatch.setattr(notifications, "encrypt", lambda _subscription, _payload: b"body")
    monkeypatch.setattr(notifications, "vapid_headers", lambda _keys, _endpoint: {})
    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=post)

    with pytest.raises(FatalPush):
        await channel.deliver(Recipient("u1"), PhotoShared("Ada"), object())


async def test_web_push_limits_each_delivery_batch_to_declared_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscriptions = InMemoryPushSubscriptions()
    for index in range(17):
        await subscriptions.add(
            "u1",
            PushSubscription(
                f"https://push{index}.example.net/subscription",
                SUBSCRIPTION.p256dh,
                SUBSCRIPTION.auth,
            ),
        )
    active = 0
    peak = 0

    async def post(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return PushResult(201, expired=False)

    monkeypatch.setattr(notifications, "encrypt", lambda _subscription, _payload: b"body")
    monkeypatch.setattr(notifications, "vapid_headers", lambda _keys, _endpoint: {})
    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=post)

    await channel.deliver(Recipient("u1"), PhotoShared("Ada"), object())

    assert peak == 8


async def test_an_encryption_failure_sends_only_the_valid_prefix(monkeypatch) -> None:
    subscriptions = InMemoryPushSubscriptions()
    invalid = PushSubscription(
        "https://push.example.net/invalid", SUBSCRIPTION.p256dh, SUBSCRIPTION.auth
    )
    later = PushSubscription(
        "https://push.example.net/later", SUBSCRIPTION.p256dh, SUBSCRIPTION.auth
    )
    await subscriptions.add("u1", SUBSCRIPTION)
    await subscriptions.add("u1", invalid)
    await subscriptions.add("u1", later)
    sent: list[str] = []
    real_encrypt = notifications.encrypt

    def encrypt(subscription: PushSubscription, payload: bytes) -> bytes:
        if subscription.endpoint == invalid.endpoint:
            raise PushError("invalid subscription key")
        return real_encrypt(subscription, payload)

    async def post(endpoint: str, body: bytes, headers: dict[str, str]) -> PushResult:
        sent.append(endpoint)
        return PushResult(201, expired=False)

    monkeypatch.setattr(notifications, "encrypt", encrypt)
    channel = WebPush(VapidKeys.generate("mailto:ops@example.com"), subscriptions, post=post)

    with pytest.raises(PushError, match="invalid subscription key"):
        await channel.deliver(Recipient("u1"), PhotoShared("Ada"), object())

    assert sent == [SUBSCRIPTION.endpoint]


async def test_an_oversized_notification_is_refused_before_it_is_encrypted() -> None:
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
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)])
    notify.kind("photo_shared")(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    for _ in range(3):
        await notify.send(PhotoShared("Ada"), to=recipient, now=1000.0)
    assert len(sender.messages) == 3
    assert notify.deduplicated == 0
    assert notify._recent == {}
    assert notify._recent_expirations == []


async def test_rate_limit_zero_disables_the_cap() -> None:
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)], rate_limit=0)
    notify.kind("photo_shared")(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    for index in range(12):
        await notify.send(PhotoShared(str(index)), to=recipient)
    assert len(sender.messages) == 12
    assert notify.rate_limited == 0
    assert notify._counts == {}


async def test_expired_notification_state_is_swept_without_revisiting_recipients() -> None:
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)], rate_limit=0)
    notify.kind("photo_shared", digest=10)(PhotoShared)

    for index in range(100):
        await notify.send(
            PhotoShared(str(index)),
            to=Recipient(f"u{index}", email=f"u{index}@example.com"),
            now=1000.0,
        )
    assert len(notify._recent) == 100

    await notify.send(
        PhotoShared("later"),
        to=Recipient("later", email="later@example.com"),
        now=1011.0,
    )
    assert set(notify._recent) == {("photo_shared", "later")}


async def test_digest_state_is_swept_during_non_digest_traffic() -> None:
    @dataclass
    class Immediate:
        subject: str

    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)], rate_limit=0)
    notify.kind("photo_shared", digest=10)(PhotoShared)
    notify.kind("immediate")(Immediate)

    for index in range(100):
        await notify.send(
            PhotoShared(str(index)),
            to=Recipient(f"u{index}", email=f"u{index}@example.com"),
            now=1000.0,
        )

    await notify.send(
        Immediate("later"),
        to=Recipient("later", email="later@example.com"),
        now=1011.0,
    )

    assert notify._recent == {}
    assert notify._recent_expirations == []


async def test_rate_state_expires_while_a_long_digest_suppresses_delivery() -> None:
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)], rate_limit=2)
    notify.kind("photo_shared", digest=7200)(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    await notify.send(PhotoShared("first"), to=recipient, now=1000.0)
    result = await notify.send(PhotoShared("duplicate"), to=recipient, now=4601.0)

    assert result.deduplicated
    assert notify._counts == {}
    assert notify._count_expirations == []


async def test_an_expired_rate_window_releases_its_recipient_state() -> None:
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)], rate_limit=2)
    notify.kind("photo_shared")(PhotoShared)
    recipient = Recipient("u1", email="u@example.com")

    await notify.send(PhotoShared("one"), to=recipient, now=1000.0)
    await notify.send(PhotoShared("two"), to=recipient, now=1001.0)
    assert not notify._within_rate_limit(recipient, 1002.0)
    assert notify._within_rate_limit(recipient, 4602.0)
    assert notify._counts == {}


async def test_expired_rate_windows_are_swept_without_revisiting_recipients() -> None:
    sender = CapturingEmailSender()
    notify = Notifications(channels=[Email(sender)], rate_limit=2)
    notify.kind("photo_shared")(PhotoShared)

    for index in range(100):
        await notify.send(
            PhotoShared(str(index)),
            to=Recipient(f"u{index}", email=f"u{index}@example.com"),
            now=1000.0,
        )
    assert len(notify._counts) == 100

    await notify.send(
        PhotoShared("later"),
        to=Recipient("later", email="later@example.com"),
        now=4601.0,
    )

    assert set(notify._counts) == {"later"}
    assert len(notify._count_expirations) == 1


async def test_the_digest_window_is_only_armed_by_a_delivery() -> None:

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
