"""Two properties every fault schedule must satisfy, against every driver.

The corpus names failures well. What it did not do was *prove* anything about
them: the corpus test asserted that two replays of the same schedule agreed,
which is a determinism property, not a recovery one. A schedule can be perfectly
deterministic about doing nothing.

So this file drives the corpus at real subsystems (`tests/_replaydrive.py`) and
holds every (schedule, driver) pair to two properties, both of which shipped
broken in this repository:

**(a) No fault may produce a hang.** A query error was raised, logged, and never
reached the caller's future, so a default configuration waited forever. Every
drive here runs under a wall-clock bound and names the schedule *and* the driver
when it blows, because a hang that stalls a suite is a hang nobody attributes.

**(b) No fault may produce silence.** For every schedule, at least one driver's
observation must differ from that driver's own no-fault control -- an exception,
a counter that moved, a status or state that changed. "Nothing happened and
nothing was recorded" is the exact shape of the doorbell that died and never
reconnected, of `_start_passes`, of `_enqueue_next_shift`, and of
`services._cancel_all` swallowing the only observation of a task's exception.

Both properties are *self-checking*. The corpus name set is pinned, so an entry
that stops being generated turns this red rather than quietly shrinking the
matrix; the lossless exemptions are pinned in both directions, so an exemption
that stops being needed is also red; and a driver-less schedule is red unless it
is explicitly gated on a live database.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from _replaydrive import (  # noqa: E402 -- `tests/` is on sys.path; see test_double_fidelity
    Driver,
    Observation,
    ReplayDriveTimeout,
    all_drivers,
    drivers_for,
    observe,
)

from wreath.replay import (
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    fault_corpus,
    record_transport_segments,
    replay_transport,
)

CORPUS = fault_corpus()

#: Every schedule the corpus is expected to generate, written out rather than
#: derived. Deriving the expectation from the thing under test is how a suite
#: silently stops covering something: delete an enum member and a derived list
#: shrinks in agreement with the deletion. This one turns red.
EXPECTED_CORPUS = frozenset(
    {
        "adapter-begin_error",
        "adapter-claim-lost-then-commit-unknown",
        "adapter-claim_lost",
        "adapter-commit_error",
        "adapter-connect_error",
        "adapter-connection_drop",
        "adapter-connection_failed",
        "adapter-decode_error",
        "adapter-doorbell-drop-then-refused-reopen",
        "adapter-listen_refused",
        "adapter-lost_commit",
        "adapter-notify_stream_end",
        "adapter-notify_stream_error",
        "adapter-object-torn-write-then-read",
        "adapter-object_read_short",
        "adapter-object_unreachable",
        "adapter-object_write_torn",
        "adapter-pool_exhausted",
        "adapter-pool_timeout",
        "adapter-prepared_poison",
        "adapter-read_timeout",
        "adapter-release_error",
        "adapter-server_error",
        "adapter-statement_timeout",
        "schedule-jump-then-reset",
        "transport-clock_jump-seg0",
        "transport-clock_jump-seg1",
        "transport-duplicate-seg0",
        "transport-duplicate-seg1",
        "transport-half_close-seg0",
        "transport-half_close-seg1",
        "transport-reset-seg0",
        "transport-reset-seg1",
        "transport-short_read-seg0",
        "transport-short_read-seg1",
        "transport-split-seg0",
        "transport-split-seg1",
        "transport-timeout-seg0",
        "transport-timeout-seg1",
        "transport-truncate-seg0",
        "transport-truncate-seg1",
    }
)

#: Schedules whose *correct* outcome is indistinguishable from no fault at all,
#: with the reason each one is. An exemption list is exactly the shape of a
#: check with nothing to check, so it is guarded from both sides: every entry
#: must in fact produce no difference (`test_the_lossless_exemptions_are_still
#: _needed`), and each one carries a positive assertion of its own below.
LOSSLESS = {
    "transport-split-seg0": (
        "SPLIT loses no bytes -- it moves the read boundary into the middle of "
        "a frame. Equality with the unfaulted replay *is* the assertion, and "
        "test_a_split_read_reproduces_the_unfaulted_replay_exactly makes it."
    ),
    "transport-split-seg1": "as transport-split-seg0, at a mid-stream segment",
    "transport-clock_jump-seg0": (
        "Advancing the virtual clock with no deadline armed is a no-op by "
        "construction; a driver that fired a timer here would be wrong, which "
        "is what test_a_clock_jump_alone_fires_nothing asserts. The region "
        "earns its place by composing -- schedule-jump-then-reset is observed."
    ),
    "transport-clock_jump-seg1": "as transport-clock_jump-seg0, mid-stream",
}

#: Seams no in-process driver can reach, and why. `_passes/driver.py::_run_chunk`
#: is the only owned consumer of `connection.transaction()`, and getting a walk
#: there needs a real ledger row -- scripting one through a `DatabaseDouble`
#: would bake the ledger's column list into a test file, and that list changed
#: twice in one day. These are driven for real in `test_replay_live_faults.py`.
DSN_ONLY = frozenset(
    {"adapter-begin_error", "adapter-commit_error", "adapter-statement_timeout"}
)

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

PAIRS = [
    (name, driver)
    for name in sorted(CORPUS)
    for driver in drivers_for(CORPUS[name])
]
PAIR_IDS = [f"{name}::{driver.name}" for name, driver in PAIRS]


# --- the matrix is the size this file says it is ------------------------------


def test_the_corpus_is_exactly_the_set_this_suite_covers() -> None:
    """A corpus entry that stops being generated must turn this red.

    Both directions, and the count. A missing name means coverage shrank
    without anyone noticing; an extra one means a region was added and nobody
    decided which driver answers it.
    """
    assert set(CORPUS) == EXPECTED_CORPUS
    assert len(CORPUS) == 41


def test_every_schedule_is_driven_by_something() -> None:
    """A schedule no driver reaches is a corpus entry nothing exercises.

    The one legitimate exception is a seam that genuinely needs a server, and
    that list is named and short. Anything else here means a region was added
    to the corpus and no driver was taught to answer it -- which is how a
    region becomes decoration.
    """
    undriven = sorted(name for name, schedule in CORPUS.items() if not drivers_for(schedule))
    assert undriven == sorted(DSN_ONLY), (
        f"these schedules have no driver: {undriven}. Add one to "
        "tests/_replaydrive.py, or name the seam in DSN_ONLY with a reason."
    )


def test_the_pair_matrix_is_not_empty_for_any_driver() -> None:
    """Every driver must be reached by at least one schedule.

    A driver whose declared seams no corpus entry touches is a driver that
    never runs -- the mirror image of an undriven schedule, and just as quiet.
    """
    reached = {driver.name for _, driver in PAIRS}
    declared = {driver.name for driver in all_drivers()}
    assert reached == declared


# --- (a) no fault may produce a hang ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "driver"), PAIRS, ids=PAIR_IDS)
async def test_no_schedule_hangs_its_driver(name: str, driver: Driver) -> None:
    """Every drive reaches an owned outcome inside the bound.

    The failure this encodes shipped in a *default* configuration: a query
    error was raised and printed while the caller waited forever, because the
    code that resolves a caller's future was the code that failed. A fault may
    fail, degrade, or be handled. It may never leave a caller waiting.
    """
    await observe(driver, name, CORPUS[name])


@pytest.mark.asyncio
async def test_the_hang_detector_actually_fires() -> None:
    """The bound is a check, so it has to be shown to be able to fail.

    Seven times this repository has shipped an assertion that could not fail --
    a decorative EXPLAIN, refusals that never fired, waivers against a disabled
    rule. A timeout that is never exercised is the same shape, so a driver that
    genuinely hangs is driven here on purpose.
    """
    hung = asyncio.Event()  # never set

    async def never_returns(schedule: FaultSchedule) -> Observation:
        await hung.wait()
        raise AssertionError("unreachable")  # pragma: no cover

    driver = Driver(name="hangs-forever", seams=frozenset(), run=never_returns)
    with pytest.raises(ReplayDriveTimeout) as caught:
        # A short bound so proving the mechanism does not cost `BOUND` seconds.
        await observe(driver, "made-up-schedule", FaultSchedule(), bound=0.05)
    message = str(caught.value)
    # Both coordinates, because a hang report that names only one of them
    # leaves you re-running the whole matrix to find out which pair it was.
    assert "made-up-schedule" in message and "hangs-forever" in message


# --- (b) no fault may produce silence -----------------------------------------


async def _control(driver: Driver) -> Observation:
    return await observe(driver, "<no faults>", FaultSchedule())


#: The schedules the silence property actually runs over. Computed once, and
#: pinned by `test_the_three_populations_partition_the_corpus` -- a skip inside
#: the property body would have been a silent skip, which is the failure mode
#: this whole suite is written against.
OBSERVABLE = sorted(set(CORPUS) - DSN_ONLY - set(LOSSLESS))


def test_the_three_populations_partition_the_corpus() -> None:
    """Every schedule is observable, lossless, or gated -- exactly one of them.

    Written as a partition so a name cannot fall out of the property by
    appearing in two lists, or by appearing in none.
    """
    assert set(OBSERVABLE) | set(LOSSLESS) | DSN_ONLY == set(CORPUS)
    assert not set(OBSERVABLE) & set(LOSSLESS)
    assert not set(OBSERVABLE) & DSN_ONLY
    assert not set(LOSSLESS) & DSN_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("name", OBSERVABLE)
async def test_no_schedule_is_silent_everywhere(name: str) -> None:
    """Some driver must be able to tell that the fault happened.

    Not *every* driver: a silent fault is legitimately silent at a seam that
    has nothing to observe with -- `CLAIM_LOST` is invisible to a handler with
    no claim in it, and modelling that as a failure would assert the opposite
    of what the region says. What is never legitimate is a fault that no owned
    code anywhere reacts to.
    """
    schedule = CORPUS[name]
    observed: dict[str, tuple[str, ...]] = {}
    for driver in drivers_for(schedule):
        control = await _control(driver)
        result = await observe(driver, name, schedule)
        observed[driver.name] = result.diff(control)
    assert any(observed.values()), (
        f"{name} produced no observable difference in any driver "
        f"({observed}). A fault that nothing notices is the shape of every "
        "silent-degradation defect this corpus exists to catch: either the "
        "owned code needs a counter, or the region needs a driver that can "
        "see it, or it is genuinely lossless and belongs in LOSSLESS with a "
        "written reason."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(LOSSLESS))
async def test_the_lossless_exemptions_are_still_needed(name: str) -> None:
    """An excuse list guarded from the other side.

    If one of these starts producing an observable difference, the exemption is
    stale and the schedule belongs back in the property -- so this turns red
    rather than letting a now-checkable region stay excused forever.
    """
    schedule = CORPUS[name]
    for driver in drivers_for(schedule):
        control = await _control(driver)
        result = await observe(driver, name, schedule)
        assert not result.diff(control), (
            f"{name} now differs from its control under {driver.name}: "
            f"{result.diff(control)}. Remove it from LOSSLESS -- the property "
            "can check it now."
        )


# --- the positive assertions the two lossless regions carry instead -----------

GET = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"


def _split_app():
    import wreath

    app = wreath.Wreath()

    @app.get("/")
    async def root(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("ok")

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", [1, 5, 8, 13, 15])
async def test_a_split_read_reproduces_the_unfaulted_replay_exactly(cut: int) -> None:
    """No bytes are lost, so nothing may change. Byte for byte.

    This is the property an incremental parser breaks first -- a rescan, a
    length read before the length arrived, a frame boundary assumed to align
    with a read boundary. Every other transport region asserts that a
    degradation is handled, and no "handled it gracefully" outcome can hide a
    violation of *this* one, which is why SPLIT is a region rather than a
    variation on SHORT_READ.
    """
    recording = record_transport_segments([GET[:16], GET[16:32], GET[32:]])
    plain = await replay_transport(_split_app(), recording)
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.SPLIT), 0, cut),))
    split = await replay_transport(_split_app(), recording, faults=schedule)
    assert split.response == plain.response
    assert split.terminal == plain.terminal
    assert b"HTTP/1.1 200" in split.response  # and it really did serve the request


@pytest.mark.asyncio
async def test_a_clock_jump_alone_fires_nothing() -> None:
    """Time passing is not, by itself, an event.

    The driver's deadline enforcement is armed by a request, not by the clock,
    so a five-second jump with a complete request already answered must change
    nothing. A driver that fired a timer here would close connections that had
    done nothing wrong -- and `schedule-jump-then-reset` is the composition
    where the jump *does* matter, which is the region's actual reason to exist.
    """
    recording = record_transport_segments([GET[:16], GET[16:32], GET[32:]])
    plain = await replay_transport(_split_app(), recording)
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.CLOCK_JUMP), 0, 5_000_000),))
    jumped = await replay_transport(_split_app(), recording, faults=schedule)
    assert jumped.normalized == plain.normalized
    assert jumped.terminal == plain.terminal


# --- the DSN-gated seams announce themselves ----------------------------------


@pytest.mark.skipif(
    not _DSN,
    reason="set WREATH_TEST_POSTGRES_DSN: the transaction seam needs a real ledger row",
)
@pytest.mark.parametrize("name", sorted(DSN_ONLY))
def test_the_transaction_seam_is_covered_by_the_live_suite(name: str) -> None:
    """The gate that keeps `DSN_ONLY` from being a place to hide a region.

    Naming a schedule here excuses it from the in-process property, so the
    excuse has to cost something: with a database available, the live suite
    must actually drive it. `tests/test_replay_live_faults.py` is where, and
    this asserts the name it uses matches the name excused here.
    """
    import test_replay_live_faults as live

    assert name in live.TRANSACTION_SCHEDULES, (
        f"{name} is excused from the in-process property but the live suite "
        "does not drive it, so nothing drives it at all"
    )
