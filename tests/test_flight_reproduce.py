"""Joining a crash file to a recording: does replaying it retrace the crash?

A ring file names the request that was in flight when a process died and the
log call sites it had reached. It does not hold the request's bytes -- a
completion cell carries a route and a status, never a payload -- so reproducing
the failure needs a transport recording of that request from somewhere else.

What joins the two is the *sequence of call sites*. If replaying the recording
emits the same sites in the same order, the replay went where the dead process
went; where it stops matching is where the behaviour changed. These tests drive
that both ways -- a recording that retraces the path, and one that does not.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest

import wreath
import wreath.errors  # register framework sites in both parent and crash child
from wreath import logging as log
from wreath.recording import read_ring_file
from wreath.replay import ReplayError, record_transport_segments, reproduce_from_ring

_flight = pytest.importorskip("wreath._native._flight", exc_type=ImportError)

CHARGE = log.event(
    "repro.charge",
    "charging {amount}",
    level=log.INFO,
    fields=(log.field("amount", int),),
)
SETTLE = log.event(
    "repro.settle",
    "settling {amount}",
    level=log.INFO,
    fields=(log.field("amount", int),),
)
REFUND = log.event(
    "repro.refund",
    "refunding {amount}",
    level=log.INFO,
    fields=(log.field("amount", int),),
)


def _app(sites) -> wreath.Wreath:
    """An app whose handler walks a fixed list of call sites."""
    app = wreath.Wreath()

    @app.get("/checkout")
    async def checkout(request: wreath.Request) -> wreath.Response:
        for site in sites:
            site(42)
        return wreath.response.TextResponse("ok")

    return app


def _recording():
    return record_transport_segments(
        [b"GET /checkout HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"]
    )


class _Ring:
    """A ring file's answers, without needing a crashed process to make one.

    `reproduce_from_ring` reads exactly two things -- which request was in
    flight, and the sites it reached -- so a stand-in that answers those is a
    truthful double rather than a mock of an implementation. The real decoder is
    covered by `tests/test_flight_ring_file.py`, including against an actual
    SIGSEGV; duplicating that here would test the fork, not the join.
    """

    def __init__(self, request_id: int, sites: tuple[int, ...]) -> None:
        self._request_id = request_id
        self._sites = sites

    def in_flight(self) -> tuple[int, ...]:
        return (self._request_id,) if self._request_id else ()

    def logs_for(self, request_id: int):
        if request_id != self._request_id:
            return ()
        return tuple(_Record(site) for site in self._sites)


class _Record:
    def __init__(self, site_id: int) -> None:
        self._site_id = site_id

    def decode(self):
        return self

    @property
    def site_id(self) -> int:
        return self._site_id


def _reproduce(app, ring, **kwargs):
    return asyncio.run(reproduce_from_ring(app, ring, _recording(), **kwargs))


def test_a_replay_that_retraces_the_path_reproduces_it() -> None:
    sites = (CHARGE, SETTLE)
    ring = _Ring(7, tuple(site.site_id for site in sites))
    outcome = _reproduce(_app(sites), ring)
    assert outcome.reproduced
    assert outcome.diverged_at is None
    assert outcome.request_id == 7
    assert outcome.observed == outcome.expected
    assert b"200" in outcome.result.response


def test_a_replay_that_takes_a_different_turn_is_reported_not_hidden() -> None:
    """The useful negative: the fix changed the path, and it says where."""
    ring = _Ring(7, (CHARGE.site_id, SETTLE.site_id))
    outcome = _reproduce(_app((CHARGE, REFUND)), ring)
    assert not outcome.reproduced
    assert outcome.diverged_at == 1, "it should agree on the first site and part at the second"
    assert outcome.observed[0] == CHARGE.site_id
    assert outcome.observed[1] == REFUND.site_id


def test_a_replay_that_stops_early_diverges_where_it_stopped() -> None:
    ring = _Ring(7, (CHARGE.site_id, SETTLE.site_id))
    outcome = _reproduce(_app((CHARGE,)), ring)
    assert not outcome.reproduced
    assert outcome.diverged_at == 1
    assert len(outcome.observed) == 1


def test_a_replay_that_goes_further_still_reproduces() -> None:
    """The crash file stops where the *process* stopped, not where the request
    would have. A replay that survives past that point has still retraced it."""
    ring = _Ring(7, (CHARGE.site_id,))
    outcome = _reproduce(_app((CHARGE, SETTLE, REFUND)), ring)
    assert outcome.reproduced
    assert len(outcome.observed) == 3


def test_an_explicit_request_id_overrides_the_in_flight_choice() -> None:
    sites = (CHARGE,)
    ring = _Ring(11, tuple(site.site_id for site in sites))
    outcome = _reproduce(_app(sites), ring, request_id=11)
    assert outcome.request_id == 11
    assert outcome.reproduced


def test_an_explicit_request_id_does_not_inspect_in_flight_requests() -> None:
    class _ExplicitRing(_Ring):
        def in_flight(self) -> tuple[int, ...]:
            raise AssertionError("an explicit request id must bypass discovery")

    sites = (CHARGE,)
    ring = _ExplicitRing(11, tuple(site.site_id for site in sites))

    outcome = _reproduce(_app(sites), ring, request_id=11)

    assert outcome.request_id == 11
    assert outcome.reproduced


def test_a_ring_with_nothing_in_flight_refuses_rather_than_guessing() -> None:
    """Every request that logged also completed: there is no crash here."""
    with pytest.raises(ReplayError, match="no request in flight"):
        _reproduce(_app((CHARGE,)), _Ring(0, ()))


def test_a_request_with_no_records_refuses_rather_than_comparing_nothing() -> None:
    """An empty expected sequence would 'reproduce' against literally anything."""
    with pytest.raises(ReplayError, match="no log records"):
        _reproduce(_app((CHARGE,)), _Ring(7, ()))


def test_several_in_flight_requests_ask_rather_than_pick() -> None:
    class _Several(_Ring):
        def in_flight(self) -> tuple[int, ...]:
            return (7, 9)

    with pytest.raises(ReplayError, match="2 requests in flight"):
        _reproduce(_app((CHARGE,)), _Several(7, (CHARGE.site_id,)))


def _real_crash_child(ring_path: str) -> None:
    """Create the real crash file from a fresh, single-threaded interpreter."""
    import faulthandler

    faulthandler.disable()
    recorder = _flight.Recorder(
        _flight.MODE_PULSE,
        ring_records=64,
        active_requests=8,
        ring_path=ring_path,
    )
    healthy = recorder.begin(1, 1, 0)
    healthy.route(7, 3)
    healthy.finish(1_000, 200, 0, 0, 0, 12)

    runtime = log.LogRuntime(
        log.recorder_sink(recorder),
        level=log.INFO,
        native=log.recorder_emitter(recorder),
    )
    log.install(runtime)
    doomed = recorder.begin(1, 1, 0)
    doomed.route(7, 3)
    log.begin_request(doomed.request_id)
    CHARGE(42)
    SETTLE(42)
    os.kill(os.getpid(), signal.SIGSEGV)
    raise RuntimeError("SIGSEGV returned")


_CRASH_SCRIPT = (
    "from tests.test_flight_reproduce import _real_crash_child; "
    "import sys; _real_crash_child(sys.argv[1])"
)


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX signals")
@pytest.mark.skipif("ASAN_OPTIONS" in os.environ, reason="ASan intercepts SIGSEGV")
def test_a_real_crash_file_drives_a_real_reproduction(tmp_path) -> None:
    """The whole story, with nothing stood in for.

    A child maps a ring, opens a request, logs its way through two call sites,
    and segfaults. The parent decodes the file it left behind, finds the request
    that never completed, and replays a recording of that request against the
    app -- reaching the same two sites in the same order.

    This is the claim the feature is for: the crash file says *which* request,
    the recording says *what* it was, and together they say whether it still
    does that.
    """
    ring_path = str(tmp_path / "flight.wfrr")
    child = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, ring_path],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == -signal.SIGSEGV, child.stderr

    ring = read_ring_file(ring_path)
    assert ring.in_flight() == (2,), "the doomed request is the one with no completion"

    outcome = _reproduce(_app((CHARGE, SETTLE)), ring)
    assert outcome.request_id == 2
    assert outcome.expected == (CHARGE.site_id, SETTLE.site_id)
    assert outcome.reproduced, (
        f"the replay did not retrace the crash: {outcome.observed} against "
        f"{outcome.expected}"
    )

    # And the negative, against the same real file: a build that takes a
    # different turn is reported rather than passing.
    diverged = _reproduce(_app((CHARGE, REFUND)), ring)
    assert not diverged.reproduced
    assert diverged.diverged_at == 1


def test_the_replay_publishes_nothing_into_the_installed_runtime() -> None:
    """A reproduction is an investigation, not traffic.

    It runs under a captured runtime, so the records it produces must not reach
    whatever this process has installed -- least of all a recorder whose ring is
    the file being investigated.
    """
    published: list[object] = []
    previous = log.install(log.LogRuntime(published.append, level=log.TRACE))
    try:
        ring = _Ring(7, (CHARGE.site_id,))
        outcome = _reproduce(_app((CHARGE,)), ring)
    finally:
        log.install(previous)
    assert outcome.reproduced
    assert published == [], "the replay's records escaped into the live runtime"
