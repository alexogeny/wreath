"""Endpoint-plan replay (Stage 7), scoped to the owned request pipeline.

Endpoint-plan replay starts from a *canonical semantic request* and re-runs the
Wreath-owned routing, binding, validation, auth-requirement evaluation, and
serialization. It never opens a socket: it synthesizes an ASGI scope and drives
the application in-process.

Handler modes decide what happens at the one boundary that is *not* owned — the
Python handler:

- `INVOKE` runs the real handler. Because that is arbitrary Python, the run is
  labelled **best effort**: the owned pipeline around it is real, but the result
  is only as reproducible as the handler.
- `REPLACE` supplies a recorded return value or exception instead of running
  the handler, then drives the owned response coercion, exception mapping, and
  serialization. No arbitrary Python handler runs, so the owned portion is
  deterministic.
- `SKIP` resolves the route and reports whether the owned router matched,
  without producing a response body.

This is a first cut: `INVOKE` exercises the whole owned pipeline end to end;
`REPLACE`/`SKIP` exercise the owned response/exception/serialization and
routing boundaries. Re-running binding/validation with a *substituted* handler
is a later increment (it requires recompiling the route with the stub endpoint).
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .request import DEFAULT_LIMITS, Request

__all__ = [
    "CanonicalRequest",
    "PlanMode",
    "PlanReplayResult",
    "replay_endpoint_plan",
]


class PlanMode(StrEnum):
    """What endpoint-plan replay does at the handler boundary."""

    INVOKE = "invoke"  # run the real handler (best effort)
    REPLACE = "replace"  # use a recorded return/exception (deterministic owned path)
    SKIP = "skip"  # resolve the route only, no handler, no body


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    """A canonical semantic request: the owned inputs to the request pipeline.

    Deliberately free of transport detail — no framing, no connection. Headers
    are policy-selected/redacted `(name, value)` byte pairs; the body is the
    already-assembled request body. `path` selects the route; `path_params`
    may be supplied when replaying without re-parsing the path.
    """

    method: str
    path: str
    headers: tuple[tuple[bytes, bytes], ...] = ()
    query_string: bytes = b""
    body: bytes = b""
    path_params: dict[str, str] = field(default_factory=dict)
    client: tuple[str, int] = ("127.0.0.1", 54321)
    server: tuple[str, int] = ("127.0.0.1", 8000)
    scheme: str = "http"


@dataclass(frozen=True, slots=True)
class PlanReplayResult:
    """The owned outcome of an endpoint-plan replay."""

    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    mode: str
    #: True when the run executed arbitrary Python (the real handler); the owned
    #: pipeline around it is still real, but the result is not deterministic.
    best_effort: bool
    #: True when the owned portion is reproducible (no arbitrary Python ran).
    deterministic: bool
    #: A short note when the route did not match or the mode could not complete.
    note: str | None = None

    def matches(self, other: PlanReplayResult) -> bool:
        """Whether two replays produced the same owned status/headers/body."""
        return (
            self.status == other.status
            and self.headers == other.headers
            and self.body == other.body
        )


def _scope(canonical: CanonicalRequest) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": canonical.method,
        "scheme": canonical.scheme,
        "path": canonical.path,
        "raw_path": canonical.path.encode("utf-8"),
        "query_string": canonical.query_string,
        "headers": [list(h) for h in canonical.headers],
        "client": list(canonical.client),
        "server": list(canonical.server),
    }


def _receive_factory(body: bytes) -> Any:
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class _Capture:
    """Collects the owned response the app sends over ASGI."""

    __slots__ = ("status", "headers", "body")

    def __init__(self) -> None:
        self.status = 0
        self.headers: tuple[tuple[bytes, bytes], ...] = ()
        self.body = bytearray()

    async def send(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            self.status = message["status"]
            self.headers = tuple((bytes(k), bytes(v)) for k, v in message.get("headers", ()))
        elif kind == "http.response.body":
            self.body += message.get("body", b"")


async def replay_endpoint_plan(
    app: Any,
    canonical: CanonicalRequest,
    *,
    mode: PlanMode = PlanMode.INVOKE,
    recorded_return: Any = None,
    recorded_exception: BaseException | None = None,
    adapters: Any = None,
) -> PlanReplayResult:
    """Replay a canonical request through the owned endpoint pipeline.

    See the module docstring for the mode semantics. `adapters` installs
    request-scoped boundary doubles (PostgreSQL / HTTP) so an `INVOKE` run can
    reach those seams deterministically or under an injected fault; it is a
    `wreath._replay_adapters.ReplayAdapters`. Raises nothing for a normal
    owned error path (it becomes a status like any request); only a misuse (e.g.
    `REPLACE` without a recorded result) raises `ValueError`.
    """
    from ._replay_adapters import installed_adapters

    mode = PlanMode(mode)
    scope = _scope(canonical)
    capture = _Capture()

    if mode is PlanMode.INVOKE:
        receive = _receive_factory(canonical.body)
        with installed_adapters(app, adapters):
            await app(scope, receive, capture.send)
        return PlanReplayResult(
            status=capture.status,
            headers=capture.headers,
            body=bytes(capture.body),
            mode=str(mode),
            best_effort=True,
            deterministic=False,
        )

    if mode is PlanMode.SKIP:
        matched = _resolve_route(app, canonical)
        return PlanReplayResult(
            status=0,
            headers=(),
            body=b"",
            mode=str(mode),
            best_effort=False,
            deterministic=True,
            note="route matched" if matched else "no route matched",
        )

    # REPLACE: run the owned routing/binding/validation, then substitute the
    # handler with the recorded result. If binding/validation rejects the request,
    # that owned status wins and the recorded result is never reached -- exactly
    # what deterministic replay wants. No arbitrary user handler runs.
    if recorded_exception is None and recorded_return is None:
        raise ValueError("REPLACE mode needs a recorded_return or recorded_exception")
    receive = _receive_factory(canonical.body)
    if hasattr(app, "_routes"):
        with _substituted_endpoints(app, recorded_return, recorded_exception):
            await app(scope, receive, capture.send)
    else:
        # A bare ASGI app has no owned routing to run; coerce the result directly.
        from .app import _coerce_response

        request = Request(scope, receive, canonical.path_params, DEFAULT_LIMITS)
        if recorded_exception is not None:
            response = await app._handle_exception(request, recorded_exception)
        else:
            response = _coerce_response(recorded_return)
        await response(capture.send)
    return PlanReplayResult(
        status=capture.status,
        headers=capture.headers,
        body=bytes(capture.body),
        mode=str(mode),
        best_effort=False,
        deterministic=True,
    )


@contextmanager
def _substituted_endpoints(
    app: Any, recorded_return: Any, recorded_exception: BaseException | None
) -> Iterator[None]:
    """Swap every route's handler for a signature-preserving stub that returns the
    recorded result, and force a recompile so the owned binder runs against it.

    `functools.wraps` copies the original endpoint's signature/annotations, so
    binding and validation still infer and check exactly what the real handler
    declared -- only the leaf body is replaced. Restored on exit."""
    original = list(app._routes)

    def make_stub(endpoint: Any) -> Any:
        @functools.wraps(endpoint)
        async def stub(*args: Any, **kwargs: Any) -> Any:
            if recorded_exception is not None:
                raise recorded_exception
            return recorded_return

        return stub

    app._routes[:] = [
        dataclasses.replace(route, endpoint=make_stub(route.endpoint))
        if hasattr(route, "endpoint")
        else route
        for route in original
    ]
    app._dirty = True
    try:
        yield
    finally:
        app._routes[:] = original
        app._dirty = True


def _resolve_route(app: Any, canonical: CanonicalRequest) -> bool:
    """Whether the owned router matches this canonical request, without running
    a handler. Uses the app's compiled matcher; tolerant of both routing modes."""
    if getattr(app, "_dirty", False):
        app._compile_routes()
    matcher = getattr(app, "_route_match", None)
    if matcher is None:
        return False
    try:
        return matcher(canonical.method, canonical.path, 0) is not None
    except TypeError:
        # `Wreath._route_match` adapts the arity across routing modes itself, so
        # this only catches a foreign or mocked app whose matcher takes a
        # different signature -- "tolerant of both routing modes" in the
        # docstring above. Narrowed from a blanket catch: a matcher that raises
        # for any other reason is a routing fault, and reporting "no route"
        # would make a replayed request silently miss a handler it should hit.
        return False
