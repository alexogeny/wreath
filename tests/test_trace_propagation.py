"""Causality across wreath's own seams: outbound HTTP, and the durable queue.

A trace that stops at the request boundary is a trace of one hop. These cover
the two seams wreath owns both ends of -- the client it calls out with, and the
queue it hands work to -- so "what caused this?" has an answer at 03:00.
"""

from __future__ import annotations

import pytest

from wreath import telemetry
from wreath.http_client import HTTPClient, TracePolicy

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
    """No incoming traceparent is the common case, and must cost nothing."""
    telemetry.propagates()
    assert telemetry.bind_propagation(_Traced(parent=None)) is None
    assert telemetry.outbound_context.get(None) is None


def test_a_malformed_traceparent_binds_nothing(unpropagated):
    telemetry.propagates()
    assert telemetry.bind_propagation(_Traced(parent="not-a-traceparent")) is None


def test_binding_is_inert_until_a_client_exists(unpropagated):
    """The request path must not pay for a feature nobody enabled."""

    class _Request:
        def header(self, name):
            return PARENT if name == "traceparent" else None

    telemetry.bind_propagation(_Request())
    assert telemetry.outbound_context.get(None) is None
