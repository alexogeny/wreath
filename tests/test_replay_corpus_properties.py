from __future__ import annotations

import asyncio
import os

import pytest
from _replaydrive import (  # `tests/` is on sys.path; see test_double_fidelity
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
DSN_ONLY = frozenset({"adapter-begin_error", "adapter-commit_error", "adapter-statement_timeout"})

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

PAIRS = [(name, driver) for name in sorted(CORPUS) for driver in drivers_for(CORPUS[name])]
PAIR_IDS = [f"{name}::{driver.name}" for name, driver in PAIRS]


def test_the_corpus_is_exactly_the_set_this_suite_covers() -> None:
    assert set(CORPUS) == EXPECTED_CORPUS
    assert len(CORPUS) == 41


def test_every_schedule_is_driven_by_something() -> None:
    undriven = sorted(name for name, schedule in CORPUS.items() if not drivers_for(schedule))
    assert undriven == sorted(DSN_ONLY), (
        f"these schedules have no driver: {undriven}. Add one to "
        "tests/_replaydrive.py, or name the seam in DSN_ONLY with a reason."
    )


def test_the_pair_matrix_is_not_empty_for_any_driver() -> None:
    reached = {driver.name for _, driver in PAIRS}
    declared = {driver.name for driver in all_drivers()}
    assert reached == declared


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "driver"), PAIRS, ids=PAIR_IDS)
async def test_no_schedule_hangs_its_driver(name: str, driver: Driver) -> None:
    await observe(driver, name, CORPUS[name])


@pytest.mark.asyncio
async def test_the_hang_detector_actually_fires() -> None:
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


async def _control(driver: Driver) -> Observation:
    return await observe(driver, "<no faults>", FaultSchedule())


#: The schedules the silence property actually runs over. Computed once, and
#: pinned by `test_the_three_populations_partition_the_corpus` -- a skip inside
#: the property body would have been a silent skip, which is the failure mode
#: this whole suite is written against.
OBSERVABLE = sorted(set(CORPUS) - DSN_ONLY - set(LOSSLESS))


def test_the_three_populations_partition_the_corpus() -> None:
    assert set(OBSERVABLE) | set(LOSSLESS) | DSN_ONLY == set(CORPUS)
    assert not set(OBSERVABLE) & set(LOSSLESS)
    assert not set(OBSERVABLE) & DSN_ONLY
    assert not set(LOSSLESS) & DSN_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("name", OBSERVABLE)
async def test_no_schedule_is_silent_everywhere(name: str) -> None:
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
    schedule = CORPUS[name]
    for driver in drivers_for(schedule):
        control = await _control(driver)
        result = await observe(driver, name, schedule)
        assert not result.diff(control), (
            f"{name} now differs from its control under {driver.name}: "
            f"{result.diff(control)}. Remove it from LOSSLESS -- the property "
            "can check it now."
        )


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
    recording = record_transport_segments([GET[:16], GET[16:32], GET[32:]])
    plain = await replay_transport(_split_app(), recording)
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.SPLIT), 0, cut),))
    split = await replay_transport(_split_app(), recording, faults=schedule)
    assert split.response == plain.response
    assert split.terminal == plain.terminal
    assert b"HTTP/1.1 200" in split.response  # and it really did serve the request


@pytest.mark.asyncio
async def test_a_clock_jump_alone_fires_nothing() -> None:
    recording = record_transport_segments([GET[:16], GET[16:32], GET[32:]])
    plain = await replay_transport(_split_app(), recording)
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.CLOCK_JUMP), 0, 5_000_000),))
    jumped = await replay_transport(_split_app(), recording, faults=schedule)
    assert jumped.normalized == plain.normalized
    assert jumped.terminal == plain.terminal


@pytest.mark.skipif(
    not _DSN,
    reason="set WREATH_TEST_POSTGRES_DSN: the transaction seam needs a real ledger row",
)
@pytest.mark.parametrize("name", sorted(DSN_ONLY))
def test_the_transaction_seam_is_covered_by_the_live_suite(name: str) -> None:
    import test_replay_live_faults as live

    assert name in live.TRANSACTION_SCHEDULES, (
        f"{name} is excused from the in-process property but the live suite "
        "does not drive it, so nothing drives it at all"
    )
