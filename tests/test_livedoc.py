"""The live-document primitive: one bounded registry, one hop, one honest tag.

These pin the primitive directly rather than only through the permission
manifest, because the properties that matter are properties of the primitive: a
registry that cannot grow without limit, a slot that is released when the client
disappears, and a coalesced notification that never claims a tag it cannot
state. A caller test can only ever show that *this* caller gets them right.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from wreath import _livedoc as livedoc
from wreath._livedoc import (
    Change,
    LiveDocument,
    change_events,
    change_stream,
)
from wreath._orm_events import publish_write

pytestmark = pytest.mark.asyncio

CHANNEL = "wreath_livedoc_test"


class FakeBus:
    """Records subscriptions and payloads; ``peers`` wires a second worker."""

    def __init__(self) -> None:
        self.handlers: list[tuple[str, Any]] = []
        self.published: list[tuple[str, Any]] = []
        self.peers: list[FakeBus] = []

    def subscribe(self, channel: str, **kwargs: Any):
        def register(handler):
            self.handlers.append((channel, handler))
            return handler

        return register

    async def publish(self, channel: str, payload: Any, **kwargs: Any) -> None:
        self.published.append((channel, payload))
        for bus in (self, *self.peers):
            for subscribed, handler in bus.handlers:
                if subscribed == channel:
                    await handler(_Message(channel, payload))


class _Message:
    def __init__(self, channel: str, payload: Any) -> None:
        self.channel = channel
        self.payload = payload


def _document(**kwargs: Any) -> LiveDocument:
    kwargs.setdefault("channel", CHANNEL)
    return LiveDocument(**kwargs)


async def _soon(condition, *, limit: float = 1.0) -> None:
    """Yield to the loop until ``condition()`` holds, rather than sleeping."""
    deadline = asyncio.get_running_loop().time() + limit
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0)


# --- being told your copy is stale ---------------------------------------------


async def test_a_subscriber_is_woken_with_the_reason_and_the_tag() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    document.notify("User::ada", "roles", etag='W/"abc"')

    assert await asyncio.wait_for(subscription.wait(), 1) == Change("roles", 'W/"abc"')


async def test_only_the_named_principal_is_woken() -> None:
    document = _document()
    ada = document.subscribe("User::ada")
    bo = document.subscribe("User::bo")
    assert ada is not None and bo is not None

    document.notify("User::ada", "roles")

    assert await asyncio.wait_for(ada.wait(), 1) == Change("roles", None)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bo.wait(), 0.02)


async def test_notify_all_wakes_everyone_because_a_deploy_stales_everyone() -> None:
    document = _document()
    subscriptions = [
        document.subscribe("User::ada"),
        document.subscribe("User::bo"),
    ]

    document.notify_all("policies", etag='W/"new"')

    for subscription in subscriptions:
        assert subscription is not None
        assert await asyncio.wait_for(subscription.wait(), 1) == Change(
            "policies", 'W/"new"'
        )


async def test_two_notifications_coalesce_to_one_wake() -> None:
    """The signal is idempotent, so a paused client must not build a backlog."""
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    document.notify("User::ada", "roles")
    document.notify("User::ada", "policies")

    assert await asyncio.wait_for(subscription.wait(), 1) == Change("policies", None)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.wait(), 0.02)


@pytest.mark.parametrize("order", [("known", "unknown"), ("unknown", "known")])
async def test_an_unknown_tag_wins_the_merge(order: tuple[str, str]) -> None:
    """Otherwise a client compares tags and skips the change we could not name."""
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    for which in order:
        document.notify(
            "User::ada", which, etag='W/"abc"' if which == "known" else None
        )

    change = await asyncio.wait_for(subscription.wait(), 1)
    assert change is not None and change.etag is None


# --- the registry is bounded ----------------------------------------------------


async def test_one_principal_cannot_fill_the_registry() -> None:
    document = _document(max_per_principal=2)

    assert document.subscribe("User::ada") is not None
    assert document.subscribe("User::ada") is not None
    assert document.subscribe("User::ada") is None      # a third tab, refused
    assert document.subscribe("User::bo") is not None   # somebody else, unaffected


async def test_the_registry_has_an_overall_cap() -> None:
    document = _document(max_subscribers=2)

    assert document.subscribe("User::ada") is not None
    assert document.subscribe("User::bo") is not None
    assert document.subscribe("User::cy") is None
    assert document.subscribers == 2


async def test_closing_frees_the_slot() -> None:
    document = _document(max_subscribers=1)
    first = document.subscribe("User::ada")
    assert first is not None and document.subscribe("User::bo") is None

    first.close()

    assert document.subscribers == 0
    assert document.subscribe("User::bo") is not None


async def test_closing_twice_does_not_free_the_slot_twice() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    subscription.close()
    subscription.close()

    assert document.subscribers == 0


async def test_a_closed_subscription_releases_its_waiter() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    waiter = asyncio.ensure_future(subscription.wait())
    await asyncio.sleep(0)
    document.close_all()

    assert await asyncio.wait_for(waiter, 1) is None


async def test_a_change_accepted_before_a_close_is_still_delivered() -> None:
    """A shutdown must not be the one way a known change reaches nobody."""
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    document.notify("User::ada", "roles")
    document.close_all()

    assert await asyncio.wait_for(subscription.wait(), 1) == Change("roles", None)
    assert await asyncio.wait_for(subscription.wait(), 1) is None


async def test_a_closed_subscription_ignores_later_notifications() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    subscription.close()
    await asyncio.wait_for(subscription.wait(), 1)

    document.notify("User::ada", "roles")

    assert subscription.closed
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.wait(), 0.02)


# --- a write is the signal ------------------------------------------------------


async def test_a_write_to_a_watched_model_stales_the_documents() -> None:
    """The role change no bolt-on can see: the ORM already announced it."""
    document = _document(watch=("Role",), watch_reason="roles")
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    publish_write(frozenset({"Role"}))

    assert await asyncio.wait_for(subscription.wait(), 1) == Change("roles", None)
    document.close_all()


async def test_a_write_to_another_model_is_not_a_change() -> None:
    document = _document(watch=("Role",), watch_reason="roles")
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    publish_write(frozenset({"Llama"}))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.wait(), 0.02)
    document.close_all()


async def test_a_model_may_be_named_by_its_class() -> None:
    class Role:
        pass

    document = _document(watch=(Role,), watch_reason="roles")
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    publish_write(frozenset({"Role"}))

    assert await asyncio.wait_for(subscription.wait(), 1) is not None
    document.close_all()


async def test_nothing_listens_for_writes_until_a_stream_exists() -> None:
    """`_orm_events` keeps a process-global list; an idle document is not in it."""
    document = _document(watch=("Role",))
    assert not document.watching

    subscription = document.subscribe("User::ada")
    assert subscription is not None and document.watching

    subscription.close()
    assert not document.watching


# --- across workers -------------------------------------------------------------


async def test_a_notification_reaches_a_stream_on_another_worker() -> None:
    """The write commits on whichever worker took it; the browser is elsewhere."""
    writer_bus, reader_bus = FakeBus(), FakeBus()
    writer_bus.peers.append(reader_bus)
    writer = _document(bus=writer_bus, watch=("Role",), watch_reason="roles")
    reader = _document(bus=reader_bus)
    subscription = reader.subscribe("User::ada")
    assert subscription is not None

    writer.notify_all("roles")

    assert await asyncio.wait_for(subscription.wait(), 1) == Change("roles", None)


async def test_a_worker_does_not_apply_its_own_echo_twice() -> None:
    bus = FakeBus()
    document = _document(bus=bus)
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    document.notify("User::ada", "roles")
    await asyncio.wait_for(subscription.wait(), 1)
    await _soon(lambda: bool(bus.published))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.wait(), 0.02)


async def test_a_detached_document_publishes_nothing() -> None:
    document = _document()
    assert not document.attached
    document.notify_all("policies")            # no bus, no error


# --- the stream -----------------------------------------------------------------


async def test_the_stream_frames_a_change_as_an_sse_event() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    events = change_events(subscription, keepalive=5)

    document.notify("User::ada", "roles", etag='W/"abc"')
    event = await asyncio.wait_for(anext(events), 1)

    assert event.event == "change"
    assert json.loads(event.data or "") == {"reason": "roles", "etag": 'W/"abc"'}
    await events.aclose()


async def test_the_stream_resolves_a_tag_the_notification_could_not_state() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    events = change_events(subscription, tag_for=lambda reason: f"tag-{reason}")

    document.notify("User::ada", "policies")
    event = await asyncio.wait_for(anext(events), 1)

    assert json.loads(event.data or "")["etag"] == "tag-policies"
    await events.aclose()


async def test_an_explicit_change_tag_does_not_call_the_fallback_resolver() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    def unexpected(_reason: str) -> str:
        raise AssertionError("resolved a tag the notification already carried")

    events = change_events(subscription, tag_for=unexpected)
    document.notify("User::ada", "roles", etag='W/"current"')
    event = await asyncio.wait_for(anext(events), 1)

    assert json.loads(event.data or "")["etag"] == 'W/"current"'
    await events.aclose()


async def test_a_resolver_that_declines_leaves_the_tag_out() -> None:
    """A stale identity cannot describe the new tag, so it says nothing."""
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    events = change_events(
        subscription, tag_for=lambda reason: None if reason == "roles" else "tag"
    )

    document.notify("User::ada", "roles")
    event = await asyncio.wait_for(anext(events), 1)

    assert json.loads(event.data or "")["etag"] is None
    await events.aclose()


async def test_an_idle_stream_sends_a_keepalive_comment() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    events = change_events(subscription, keepalive=0.01)

    event = await asyncio.wait_for(anext(events), 1)

    assert event.comment == "keepalive" and event.data is None
    await events.aclose()


async def test_a_moved_fingerprint_wakes_the_stream_without_a_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy set replaced in-process; nobody called `notify_all`."""
    monkeypatch.setattr(livedoc, "_FINGERPRINT_TTL", 0.0)
    fingerprints = iter(["one"])
    document = _document(fingerprint=lambda: next(fingerprints, "two"))
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    events = change_events(subscription, keepalive=0.01)

    event = await asyncio.wait_for(anext(events), 1)

    assert event.event == "change"
    assert json.loads(event.data or "")["reason"] == "policies"
    await events.aclose()


async def test_the_fingerprint_is_read_once_per_worker_not_once_per_stream() -> None:
    calls = 0

    def fingerprint() -> str:
        nonlocal calls
        calls += 1
        return "same"

    document = _document(fingerprint=fingerprint)
    for _ in range(50):
        document.fingerprint()

    assert calls == 1


async def test_a_document_with_no_fingerprint_has_no_drift_check() -> None:
    document = _document()
    assert document.fingerprint() == ""


async def test_a_vanished_client_frees_its_slot() -> None:
    """The disconnect closes the generator; the `finally` is the cleanup."""
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    events = change_events(subscription, keepalive=0.01)
    await asyncio.wait_for(anext(events), 1)

    await events.aclose()

    assert document.subscribers == 0 and subscription.closed


async def test_the_stream_ends_when_the_document_closes_it() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    events = change_events(subscription, keepalive=5)
    pending = asyncio.ensure_future(anext(events))
    await asyncio.sleep(0)

    document.close_all()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, 1)


async def test_change_stream_is_an_sse_response() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    response = change_stream(subscription)

    assert response.media_type == b"text/event-stream"
    document.close_all()
