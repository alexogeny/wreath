from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import wreath._livedoc as module
from wreath._livedoc import Change, LiveDocument, change_events


def _document(**options: Any) -> LiveDocument:
    return LiveDocument(channel="wreath_livedoc_mutation", **options)


@pytest.mark.asyncio
async def test_fingerprint_cache_expires_at_the_declared_one_second_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter((0.0, 2.0))
    values = iter(("first", "second"))
    monkeypatch.setattr(module, "monotonic", lambda: next(times))
    document = _document(fingerprint=lambda: next(values))

    assert document.fingerprint() == "first"
    assert document.fingerprint() == "second"


@pytest.mark.asyncio
async def test_closed_subscription_set_ignores_a_later_change() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    subscription.close()

    subscription._set(Change("roles", "tag"))

    assert subscription._pending is None


@pytest.mark.asyncio
async def test_coalescing_keeps_an_unknown_new_tag() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    subscription._set(Change("known", "tag"))
    subscription._set(Change("unknown", None))

    assert subscription._pending is not None
    assert subscription._pending.etag is None


@pytest.mark.asyncio
async def test_coalescing_keeps_an_unknown_pending_tag() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    subscription._set(Change("unknown", None))
    subscription._set(Change("known", "tag"))

    assert subscription._pending is not None
    assert subscription._pending.etag is None


@pytest.mark.asyncio
async def test_coalescing_replaces_one_known_tag_with_the_next() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    subscription._set(Change("first", "old"))
    subscription._set(Change("second", "new"))

    assert subscription._pending == Change("second", "new")


@pytest.mark.asyncio
async def test_document_without_a_watch_never_subscribes_to_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "subscribe_writes", pytest.fail)

    assert _document().subscribe("User::ada") is not None


@pytest.mark.asyncio
async def test_document_subscribes_to_writes_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(module, "subscribe_writes", calls.append)
    document = _document(watch=("Role",))

    assert document.subscribe("User::ada") is not None
    assert document.subscribe("User::bo") is not None

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_release_ignores_a_subscription_for_an_unknown_principal() -> None:
    document = _document()
    foreign: Any = SimpleNamespace(principal="User::unknown")

    document._release(foreign)

    assert document.subscribers == 0


@pytest.mark.asyncio
async def test_release_ignores_a_foreign_subscription_for_a_known_principal() -> None:
    document = _document()
    other = _document()
    local = document.subscribe("User::ada")
    foreign = other.subscribe("User::ada")
    assert local is not None and foreign is not None

    document._release(foreign)

    assert document.subscribers == 1
    local.close()
    foreign.close()


@pytest.mark.asyncio
async def test_release_removes_the_empty_principal_bucket() -> None:
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    subscription.close()

    assert "User::ada" not in document._by_principal


@pytest.mark.asyncio
async def test_release_keeps_a_nonempty_principal_bucket() -> None:
    document = _document()
    first = document.subscribe("User::ada")
    second = document.subscribe("User::ada")
    assert first is not None and second is not None

    first.close()

    assert document.subscribers == 1
    assert second in document._by_principal["User::ada"]
    second.close()


@pytest.mark.asyncio
async def test_last_watched_subscription_unsubscribes_from_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "subscribe_writes", lambda _callback: None)
    calls: list[Any] = []
    monkeypatch.setattr(module, "unsubscribe_writes", calls.append)
    document = _document(watch=("Role",))
    first = document.subscribe("User::ada")
    second = document.subscribe("User::bo")
    assert first is not None and second is not None

    first.close()
    assert calls == []
    second.close()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unwatched_document_never_unsubscribes_from_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "unsubscribe_writes", pytest.fail)
    document = _document()
    subscription = document.subscribe("User::ada")
    assert subscription is not None

    subscription.close()


def test_detached_document_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    document._bridge = SimpleNamespace(
        attached=False,
        publish_soon=pytest.fail,
        channel="wreath_livedoc_mutation",
    )

    document._publish(None, "roles", None)


@pytest.mark.asyncio
async def test_apply_refuses_a_non_string_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document()
    monkeypatch.setattr(LiveDocument, "_deliver", pytest.fail)

    await document._apply({"principal": None, "reason": 7, "etag": None})


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [7, b"User::ada"])
async def test_apply_refuses_a_non_string_principal(
    principal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _document()
    monkeypatch.setattr(LiveDocument, "_deliver", pytest.fail)

    await document._apply({"principal": principal, "reason": "roles", "etag": None})


@pytest.mark.asyncio
@pytest.mark.parametrize(("etag", "expected"), [("tag", "tag"), (7, None)])
async def test_apply_preserves_only_string_etags(
    etag: Any, expected: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered: list[tuple[Any, Change]] = []
    document = _document()

    def deliver(_document: LiveDocument, principal: Any, change: Change) -> None:
        delivered.append((principal, change))

    monkeypatch.setattr(LiveDocument, "_deliver", deliver)

    await document._apply({"principal": "User::ada", "reason": "roles", "etag": etag})

    assert delivered == [("User::ada", Change("roles", expected))]


@pytest.mark.asyncio
async def test_change_events_uses_the_documents_keepalive_when_unspecified() -> None:
    document = _document(keepalive=0.001)
    subscription = document.subscribe("User::ada")
    assert subscription is not None
    events = change_events(subscription)

    event = await asyncio.wait_for(anext(events), 1)

    assert event.comment == "keepalive"
    await events.aclose()
