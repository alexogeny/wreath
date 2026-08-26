"""Causality across wreath's own seams: outbound HTTP, and the durable queue.

A trace that stops at the request boundary is a trace of one hop. These cover
the two seams wreath owns both ends of -- the client it calls out with, and the
queue it hands work to -- so "what caused this?" has an answer at 03:00.
"""

from __future__ import annotations

import pytest

from wreath import Wreath, telemetry
from wreath.http_client import HTTPClient, TracePolicy
from wreath.middleware.base import PipelineHooks
from wreath.testing import TestClient

PARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture
def unpropagated():
    """Reset the latch and the binding, so one test cannot arm the next."""
    previous = telemetry.PROPAGATING
    telemetry.PROPAGATING = False
    token = telemetry.outbound_context.set(None)
    try:
        yield
    finally:
        telemetry.outbound_context.reset(token)
        telemetry.PROPAGATING = previous


def _client(**kw) -> HTTPClient:
    client = HTTPClient("billing", base_url="https://billing.example", **kw)
    client._started = True
    return client


def _sent_headers(client: HTTPClient, headers=()) -> dict[bytes, bytes]:
    """The headers this client would put on the wire.

    `_propagated` is the whole of the decision and is applied once, before the
    redirect loop, so this is the exact tuple every hop carries. Driving a real
    `request()` would need a socket and would not check anything more --
    `HTTPClient` is slotted, so there is no instance method to intercept.
    """
    return {name.lower(): value for name, value in client._propagated(tuple(headers))}


# --- stage 1: outbound HTTP --------------------------------------------------


def test_a_traced_request_propagates_traceparent(unpropagated):
    telemetry.outbound_context.set((PARENT, ""))
    headers = _sent_headers(_client())
    assert headers[b"traceparent"] == PARENT.encode()


def test_an_untraced_request_sends_no_traceparent(unpropagated):
    headers = _sent_headers(_client())
    assert b"traceparent" not in headers


def test_an_explicit_traceparent_wins(unpropagated):
    """A header the caller wrote is a decision; the framework does not overrule it."""
    telemetry.outbound_context.set((PARENT, ""))
    mine = b"00-11111111111111111111111111111111-2222222222222222-01"
    headers = _sent_headers(_client(), ((b"traceparent", mine),))
    assert headers[b"traceparent"] == mine


def test_a_client_can_refuse_to_propagate(unpropagated):
    """An origin outside the trust boundary does not get our trace ids."""
    telemetry.outbound_context.set((PARENT, ""))
    headers = _sent_headers(_client(trace=TracePolicy(propagate=False)))
    assert b"traceparent" not in headers


def test_an_unarmed_process_never_sends_a_stale_bound_context(unpropagated):
    """The process latch is authoritative even if a context variable is bound.

    A copied context can outlive the application that armed propagation.  The
    cheap global guard prevents that stale value crossing an outbound trust
    boundary before a new application declares an outbound client.
    """
    telemetry.outbound_context.set((PARENT, ""))
    client = _client()
    telemetry.PROPAGATING = False

    assert b"traceparent" not in _sent_headers(client)


def test_tracestate_rides_only_when_asked(unpropagated):
    telemetry.outbound_context.set((PARENT, "vendor=abc"))
    assert b"tracestate" not in _sent_headers(_client())
    headers = _sent_headers(_client(trace=TracePolicy(tracestate=True)))
    assert headers[b"tracestate"] == b"vendor=abc"


def test_an_empty_tracestate_is_not_sent(unpropagated):
    telemetry.outbound_context.set((PARENT, ""))
    headers = _sent_headers(_client(trace=TracePolicy(tracestate=True)))
    assert b"tracestate" not in headers


def test_constructing_a_client_arms_propagation(unpropagated):
    """Nothing binds a context until something exists that could send one."""
    assert telemetry.PROPAGATING is False
    _client()
    assert telemetry.PROPAGATING is True


class _Traced:
    """The smallest thing `server_span` reads: an incoming `traceparent`."""

    def __init__(self, parent=PARENT, state=None):
        self._parent = parent
        self._state = state

    def header(self, name):
        if name == "traceparent":
            return self._parent
        if name == "tracestate":
            return self._state
        return None


def test_binding_carries_the_incoming_context_when_armed(unpropagated):
    telemetry.propagates()
    token = telemetry.bind_propagation(_Traced())
    assert token is not None
    parent, state = telemetry.outbound_context.get(None)
    assert parent == PARENT
    assert state == ""


def test_binding_carries_tracestate_when_the_request_had_one(unpropagated):
    telemetry.propagates()
    telemetry.bind_propagation(_Traced(state="vendor=abc"))
    assert telemetry.outbound_context.get(None)[1] == "vendor=abc"


def test_an_unpropagated_request_binds_nothing(unpropagated):
    """No incoming traceparent is the common case: nothing to send.

    It binds *None* rather than skipping the bind, so a reused context cannot
    carry the previous request's parent -- see the inheritance test below.
    """
    telemetry.propagates()
    telemetry.bind_propagation(_Traced(parent=None))
    assert telemetry.outbound_context.get(None) is None


def test_a_malformed_traceparent_binds_nothing(unpropagated):
    telemetry.propagates()
    telemetry.bind_propagation(_Traced(parent="not-a-traceparent"))
    assert telemetry.outbound_context.get(None) is None


def test_binding_is_inert_until_a_client_exists(unpropagated):
    """The request path must not pay for a feature nobody enabled."""

    class _Request:
        def header(self, name):
            return PARENT if name == "traceparent" else None

    telemetry.bind_propagation(_Request())
    assert telemetry.outbound_context.get(None) is None


def test_an_unpropagated_request_does_not_inherit_the_previous_one(unpropagated):
    """A context reused across requests must not leak the last one's parent.

    Keep-alive can hand two requests the same context. If the second binds
    nothing because it carries no traceparent, it must still not send outbound
    calls stamped with the *first* request's trace -- that is a misattribution,
    which is worse than no trace at all.
    """
    telemetry.propagates()
    telemetry.bind_propagation(_Traced())
    assert telemetry.outbound_context.get(None) is not None
    telemetry.bind_propagation(_Traced(parent=None))
    assert telemetry.outbound_context.get(None) is None


# --- stage 1: the pipeline binding, which makes all of the above live --------


async def test_a_handler_inherits_the_requests_context_end_to_end(unpropagated):
    """The pipeline binding. Without it every test above is inert in a real app.

    Drives the whole chain in one assertion: an incoming `traceparent` reaches
    the request pipeline, the pipeline binds it, and the client the handler
    calls out with puts it on the wire.
    """
    app = Wreath()
    outbound = _client()  # constructing it arms PROPAGATING

    @app.get("/call")
    async def call(request):
        return {"sent": _sent_headers(outbound).get(b"traceparent", b"").decode()}

    async with TestClient(app) as client:
        response = await client.get("/call", headers={"traceparent": PARENT})

    assert response.json()["sent"] == PARENT


async def test_an_untraced_request_leaves_the_handler_nothing_to_send(unpropagated):
    """The common case: no upstream tracer, so nothing is invented."""
    app = Wreath()
    outbound = _client()

    @app.get("/call")
    async def call(request):
        return {"sent": _sent_headers(outbound).get(b"traceparent", b"").decode()}

    async with TestClient(app) as client:
        response = await client.get("/call")

    assert response.json()["sent"] == ""


async def test_an_app_with_no_outbound_client_never_binds(unpropagated, monkeypatch):
    """The guide claims such an app pays nothing. This is that claim, asserted.

    `PROPAGATING` is a *cost* guard -- `bind_propagation` would no-op anyway --
    so nothing about the response can distinguish it. What is observable is the
    call itself, and the claim is about the call.
    """
    calls: list[object] = []
    monkeypatch.setattr(telemetry, "bind_propagation", lambda request: calls.append(request))

    # Both binding sites: the request is built in one place when global hooks
    # exist and another when they do not, so an app without middleware exercises
    # only one of the two guards.
    async def passthrough(request):
        return None

    for hooks in (False, True):
        app = Wreath()
        if hooks:
            app.add_global_middleware(PipelineHooks(before=passthrough))

        @app.get("/quiet")
        async def quiet(request):
            return {}

        async with TestClient(app) as client:
            assert (await client.get("/quiet")).status == 200

    assert calls == []


async def test_a_global_middleware_call_carries_the_context(unpropagated):
    """Binding happens before the first `before` hook, not after it.

    A global middleware that calls out -- an auth introspection, a flag fetch --
    is doing so because of this request, so its call belongs in the same trace.
    """
    seen: list[str] = []
    app = Wreath()
    outbound = _client()

    async def look(request):
        seen.append(_sent_headers(outbound).get(b"traceparent", b"").decode())
        return None

    app.add_global_middleware(PipelineHooks(before=look))

    @app.get("/call")
    async def call(request):
        return {}

    async with TestClient(app) as client:
        await client.get("/call", headers={"traceparent": PARENT})

    assert seen == [PARENT]
