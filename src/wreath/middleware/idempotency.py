"""Make unsafe requests replay-safe with an ``Idempotency-Key`` header.

A client that retries a ``POST`` (a dropped connection, an impatient user) must
not create two records. When a request carries an ``Idempotency-Key``, this
middleware remembers the first response and replays it for any repeat of the same
key — from a small, bounded, in-process store (no Redis, no external state).

Opt-in per request (the header) and safe by construction:

* only unsafe methods are considered (``POST``/``PUT``/``PATCH``/``DELETE``),
* the key is scoped by method, path, and the authenticated principal, so two
  callers cannot collide on the same key value,
* a concurrent duplicate (the first is still running) gets ``409``,
* a ``5xx`` is not cached — a transient failure stays retryable.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..cache import BoundedCache
from ..request import Request
from ..response import ProblemResponse, Response

_UNCACHEABLE_BODY = ("StreamingResponse", "FileResponse", "SSEResponse", "PreparedResponse")
_IN_FLIGHT = object()


class IdempotencyMiddleware:
    """Replay the stored response for a repeated ``Idempotency-Key``."""

    global_scope = True
    __slots__ = ("_header", "_methods", "_store")

    def __init__(
        self,
        *,
        ttl: float = 24 * 60 * 60,
        max_entries: int = 4096,
        methods: Iterable[str] = ("POST", "PUT", "PATCH", "DELETE"),
        header: str = "idempotency-key",
    ) -> None:
        self._store: BoundedCache = BoundedCache(max_entries=max_entries, ttl=ttl)
        self._methods = frozenset(m.upper() for m in methods)
        self._header = header.lower()

    def _key(self, request: Request) -> tuple | None:
        if request.method not in self._methods:
            return None
        value = request.header(self._header)
        if not value:
            return None
        identity = request.identity
        principal = identity.id if identity is not None else None
        return (request.method, request.path, principal, value)

    async def before(self, request: Request):
        key = self._key(request)
        if key is None:
            return None
        entry = self._store.get(key)
        if entry is _IN_FLIGHT:
            return ProblemResponse(
                status=409,
                detail="a request with this Idempotency-Key is still in progress",
            )
        if entry is not None:
            status, headers, body = entry
            replay = Response(body, status=status, headers=list(headers))
            replay.headers.append((b"idempotency-replayed", b"true"))
            return replay
        # Reserve the key so a concurrent duplicate 409s, and remember it for `after`.
        self._store.set(key, _IN_FLIGHT)
        request.state.idempotency_key = key
        return None

    async def after(self, request: Request, response):
        key = getattr(request.state, "idempotency_key", None) if request._state else None
        if key is None:
            return response
        if response.status >= 500 or type(response).__name__ in _UNCACHEABLE_BODY:
            # A transient failure (or an un-snapshottable body) stays retryable.
            self._store.delete(key)
            return response
        self._store.set(key, (response.status, tuple(response.headers), response.body))
        return response


__all__ = ["IdempotencyMiddleware"]
