"""IdempotencyMiddleware: first response is stored and replayed for the same key."""
from __future__ import annotations

import pytest

from wreath.middleware import IdempotencyMiddleware
from wreath.request import Request
from wreath.response import Response

pytestmark = pytest.mark.asyncio


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request(method="POST", path="/orders", key: str | None = "k1") -> Request:
    headers = [(b"host", b"x")]
    if key is not None:
        headers.append((b"idempotency-key", key.encode()))
    scope = {"type": "http", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": headers}
    return Request(scope, _receive)


async def test_first_call_passes_through_then_replays() -> None:
    mw = IdempotencyMiddleware()

    first = _request()
    assert await mw.before(first) is None            # not seen -> proceed
    await mw.after(first, Response(b"created", status=201))

    second = _request()                              # same key
    replay = await mw.before(second)
    assert replay is not None
    assert replay.status == 201 and replay.body == b"created"
    assert (b"idempotency-replayed", b"true") in replay.headers


async def test_concurrent_duplicate_gets_409() -> None:
    mw = IdempotencyMiddleware()
    first = _request()
    assert await mw.before(first) is None            # reserves the key (in-flight)
    # A second identical request arrives before the first's `after` runs.
    conflict = await mw.before(_request())
    assert conflict is not None and conflict.status == 409


async def test_5xx_is_not_cached_and_stays_retryable() -> None:
    mw = IdempotencyMiddleware()
    first = _request()
    await mw.before(first)
    await mw.after(first, Response(b"boom", status=500))
    # The key was released, so a retry proceeds instead of replaying the 500.
    assert await mw.before(_request()) is None


async def test_safe_method_and_missing_key_are_ignored() -> None:
    mw = IdempotencyMiddleware()
    assert await mw.before(_request(method="GET")) is None
    assert await mw.before(_request(key=None)) is None
    # Neither reserved a key, so `after` is a passthrough.
    resp = Response(b"x")
    assert await mw.after(_request(key=None), resp) is resp


async def test_key_is_scoped_by_principal() -> None:
    from wreath._auth.models import Identity

    mw = IdempotencyMiddleware()
    alice = _request()
    alice._set_identity(Identity(id="alice", roles=frozenset()))
    await mw.before(alice)
    await mw.after(alice, Response(b"alice-order", status=201))

    bob = _request()                                 # same key value, different user
    bob._set_identity(Identity(id="bob", roles=frozenset()))
    # Bob must NOT get alice's stored response.
    assert await mw.before(bob) is None
