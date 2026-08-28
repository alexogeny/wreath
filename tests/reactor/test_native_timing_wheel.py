"""Native timing wheel (wreath._native._reactor) — the first C reactor primitive.

O(1) insert/cancel with a fixed slot array; the backing store the loop's
call_later/call_at will use in place of asyncio's heap. Unlike the rest of this
directory these are GREEN now — the C primitive exists ahead of its wiring.
"""
from __future__ import annotations

import pytest

try:
    from wreath._native._reactor import TimingWheel
except ImportError:  # pragma: no cover -- the native reactor build is optional
    TimingWheel = None

pytestmark = pytest.mark.skipif(TimingWheel is None, reason="native reactor not built")


def test_fires_due_timers_on_advance():
    fired = []
    w = TimingWheel(resolution=0.001, slots=512, base=0.0)
    w.schedule(0.010, lambda: fired.append("a"))
    assert w.count == 1
    # Not yet due.
    assert w.advance(0.005) == []
    # Due now.
    callbacks = w.advance(0.020)
    for cb in callbacks:
        cb()
    assert fired == ["a"]
    assert w.count == 0


def test_timers_fire_in_deadline_order_across_rounds():
    w = TimingWheel(resolution=0.001, slots=8, base=0.0)  # tiny wheel forces wraparound
    order = []
    # Deadlines span several full rotations of the 8-slot wheel.
    for i in (30, 5, 17, 2, 25):
        w.schedule(i * 0.001, (lambda n: (lambda: order.append(n)))(i))
    now = 0.0
    for _ in range(40):
        now += 0.001
        for cb in w.advance(now):
            cb()
    assert order == [2, 5, 17, 25, 30]


def test_cancel_prevents_fire_and_frees_slot():
    w = TimingWheel(resolution=0.001, slots=64, base=0.0)
    fired = []
    w.schedule(0.005, lambda: fired.append("keep"))
    drop = w.schedule(0.005, lambda: fired.append("drop"))
    assert w.count == 2
    assert drop.cancel() is True
    assert drop.cancelled() is True
    assert w.count == 1
    for cb in w.advance(0.010):
        cb()
    assert fired == ["keep"]


def test_exact_rotation_timers_fire_on_time():
    """A timer at exactly N*slots ticks must fire after N rotations, not N+1.
    Regression for rounds = ticks/slots (off by a full rotation on exact multiples).
    """
    for ticks in (8, 16, 24, 9, 1):
        w = TimingWheel(resolution=0.001, slots=8, base=0.0)
        fired = []
        w.schedule(ticks * 0.001, (lambda tk, f: (lambda: f.append(tk)))(ticks, fired))
        fire_at = None
        for i in range(1, 60):
            for cb in w.advance(i * 0.001):
                cb()
                fire_at = i
        assert fire_at == ticks, f"{ticks}-tick timer fired at {fire_at}"


def test_large_clock_jump_fires_overdue_in_order_and_preserves_future_timer():
    w = TimingWheel(resolution=0.001, slots=8, base=0.0)
    fired: list[float] = []
    for deadline in (30.0, 5.0, 90_000.0, 17.0):
        w.schedule(deadline, lambda value=deadline: fired.append(value))

    # Simulate resuming after a one-day machine suspend. This used to walk all
    # 86.4 million elapsed ticks before returning to the event loop.
    for callback in w.advance(86_400.0):
        callback()

    assert fired == [5.0, 17.0, 30.0]
    assert w.count == 1
    for callback in w.advance(90_001.0):
        callback()
    assert fired == [5.0, 17.0, 30.0, 90_000.0]


def test_large_clock_jump_dispatches_advance_run_callbacks():
    w = TimingWheel(resolution=0.001, slots=8, base=0.0)
    fired: list[str] = []
    w.schedule(5.0, lambda: fired.append("due"))
    w.schedule(90_000.0, lambda: fired.append("future"))

    assert w.advance_run(86_400.0) == 1
    assert fired == ["due"]
    assert w.advance_run(90_001.0) == 1
    assert fired == ["due", "future"]


def test_double_cancel_is_safe():
    w = TimingWheel(resolution=0.001, slots=16, base=0.0)
    h = w.schedule(0.005, lambda: None)
    assert h.cancel() is True
    assert h.cancel() is False  # idempotent


def test_cancelling_earliest_timer_rescans_only_its_bucket():
    wheel = TimingWheel(resolution=0.001, slots=512, base=0.0)
    handles = [wheel.schedule(index * 0.001, lambda: None)
               for index in range(1, 257)]
    rescans = wheel.slot_rescans
    updates = wheel.tree_node_updates

    assert handles[0].cancel() is True
    assert wheel.slot_rescans - rescans == 1
    assert wheel.tree_node_updates - updates <= 10


def test_cancelling_a_same_deadline_cohort_rescans_once_total():
    # Every timer here shares one deadline, so all tie their slot minimum.
    # Tie-counting must let all but the last leave in O(1): one rescan for
    # the whole cohort, not one per cancel (the O(k^2) hazard this fixes).
    wheel = TimingWheel(resolution=0.001, slots=512, base=0.0)
    handles = [wheel.schedule(0.050, lambda: None) for _ in range(256)]
    rescans = wheel.slot_rescans

    for handle in handles:
        assert handle.cancel() is True

    assert wheel.slot_rescans - rescans == 1


def test_firing_a_same_deadline_cohort_rescans_once_total():
    # The same tie-count contract on the expiry path: draining k timers due
    # in one tick rescans the slot once, after the whole chain has left.
    fired = []
    wheel = TimingWheel(resolution=0.001, slots=512, base=0.0)
    for index in range(256):
        wheel.schedule(0.050, lambda i=index: fired.append(i))
    rescans = wheel.slot_rescans

    due = wheel.advance(0.100)
    for callback in due:
        callback()

    assert len(fired) == 256
    assert wheel.slot_rescans - rescans == 1


def test_advance_over_idle_gap_skips_parked_timers():
    # Absolute-deadline drain jumps cursor to the next due tick via the
    # interval tree; a long-parked timer must not be touched (let alone
    # decremented once per rotation) while the wheel sweeps idle ticks.
    wheel = TimingWheel(resolution=0.001, slots=64, base=0.0)
    wheel.schedule(10.0, lambda: None)          # ~156 rotations away
    rescans = wheel.slot_rescans
    updates = wheel.tree_node_updates

    assert wheel.advance(5.0) == []             # 5000 ticks, nothing due
    # No node visited, so neither counter moves for the parked timer.
    assert wheel.slot_rescans == rescans
    assert wheel.tree_node_updates == updates


def test_wheel_releases_pending_timers_on_dealloc():
    # A wheel dropped with live timers must not leak or dangle.
    w = TimingWheel(resolution=0.001, slots=32, base=0.0)
    handles = [w.schedule(1.0, lambda: None) for _ in range(100)]
    assert w.count == 100
    del w
    # handles still valid (own their own ref); cancelling after wheel death is safe
    import gc

    gc.collect()
    for h in handles:
        h.cancel()


# --- colliding slots -------------------------------------------------------
#
# A slot is a hash of the deadline, so deadlines congruent modulo `slots` share
# one. Every test above either spreads deadlines one per slot or gives a whole
# cohort the *same* deadline; neither builds the arrangement that actually
# hurts, which is many *distinct* deadlines in one slot. That gap is why the
# chain this heap replaced stayed quadratic in both cancel and fire without any
# test noticing.
#
# `SLOTS` is small so a modest timer count makes a deep chain.

SLOTS = 8
COLLIDING = 64


def _colliding_delays(count: int = COLLIDING) -> list[float]:
    """Delays whose deadlines all land in one slot, at distinct deadlines."""
    return [SLOTS * index * 0.001 for index in range(1, count + 1)]


def test_colliding_deadlines_fire_in_deadline_order():
    fired: list[int] = []
    wheel = TimingWheel(resolution=0.001, slots=SLOTS, base=0.0)
    delays = _colliding_delays()
    # Schedule out of order so passing cannot come from insertion order.
    for index in sorted(range(len(delays)), key=lambda i: (i * 37) % len(delays)):
        wheel.schedule(delays[index], lambda i=index: fired.append(i))

    for callback in wheel.advance(delays[-1] + 0.010):
        callback()

    assert fired == list(range(len(delays)))


def test_cancelling_from_the_middle_of_a_colliding_slot_leaves_the_rest_intact():
    # Arbitrary removal is the operation the parent pointer exists for, and the
    # one a cancelled request deadline takes. Removing interior nodes must not
    # disturb the order or the survival of their neighbours.
    fired: list[int] = []
    wheel = TimingWheel(resolution=0.001, slots=SLOTS, base=0.0)
    delays = _colliding_delays()
    handles = [
        wheel.schedule(delay, lambda i=index: fired.append(i))
        for index, delay in enumerate(delays)
    ]
    doomed = [index for index in range(len(delays)) if index % 3 == 1]
    for index in doomed:
        assert handles[index].cancel() is True

    assert wheel.count == len(delays) - len(doomed)
    for callback in wheel.advance(delays[-1] + 0.010):
        callback()

    assert fired == [i for i in range(len(delays)) if i not in set(doomed)]


def test_cancelling_the_minimum_of_a_colliding_slot_republishes_the_next_one():
    # Every cancel here removes the slot's current minimum, so every one has to
    # move the published minimum -- the arrangement whose old cost was a full
    # chain walk per cancel. The property that matters is that the wheel does
    # not under-report: advancing to just short of the earliest survivor must
    # fire nothing, and advancing past it must fire exactly that one.
    delays = _colliding_delays(8)
    for cancelled in range(len(delays) - 1):
        fired: list[int] = []
        wheel = TimingWheel(resolution=0.001, slots=SLOTS, base=0.0)
        handles = [
            wheel.schedule(delay, lambda i=index, out=fired: out.append(i))
            for index, delay in enumerate(delays)
        ]
        for index in range(cancelled + 1):
            assert handles[index].cancel() is True

        survivor = cancelled + 1
        assert wheel.advance(delays[survivor] - 0.002) == []
        assert fired == []
        for callback in wheel.advance(delays[survivor] + 0.0005):
            callback()
        assert fired == [survivor], (
            f"after cancelling {cancelled + 1}, expected only timer {survivor}")


def test_colliding_slot_survives_interleaved_schedule_cancel_and_fire():
    # A model check over the three operations mixed, which is where a heap gets
    # its pointers wrong if it is going to. Deterministic ordering, so a failure
    # reproduces from the seed in the source rather than from a lucky run.
    import random

    rng = random.Random(20260727)
    wheel = TimingWheel(resolution=0.001, slots=SLOTS, base=0.0)
    live: dict[int, float] = {}
    handles: dict[int, object] = {}
    fired: list[tuple[float, int]] = []
    now = 0.0
    key = 0

    for _ in range(400):
        action = rng.random()
        if action < 0.5:
            key += 1
            # Always strictly in the future. `schedule` clamps a past delay to
            # one tick, which would make the model's intended deadline a
            # fiction -- and the resulting disagreement would look like a
            # heap-ordering failure rather than the test's own mistake.
            deadline = now + SLOTS * rng.randint(1, 40) * 0.001
            handles[key] = wheel.schedule(
                deadline - now, lambda k=key, d=deadline: fired.append((d, k)))
            live[key] = deadline
        elif action < 0.75 and live:
            doomed = rng.choice(sorted(live))
            assert handles[doomed].cancel() is True
            del live[doomed]
        else:
            now += SLOTS * 4 * 0.001
            for callback in wheel.advance(now):
                callback()
            for expired in [k for k, when in live.items() if when <= now]:
                del live[expired]
        assert wheel.count == len(live), (
            f"wheel holds {wheel.count}, model holds {len(live)}")

    deadlines = [deadline for deadline, _ in fired]
    assert deadlines == sorted(deadlines), (
        "callbacks left the wheel out of deadline order")
    assert fired, "the run never fired anything, so it proved nothing"


def test_a_deep_colliding_slot_is_released_on_dealloc():
    # `heap_release` threads its own stack through the sibling links rather than
    # recursing, because a slot's heap is as deep as its chain. A recursive
    # release would trade the quadratic for a stack overflow here.
    import gc

    wheel = TimingWheel(resolution=0.001, slots=SLOTS, base=0.0)
    handles = [wheel.schedule(delay, lambda: None)
               for delay in _colliding_delays(4000)]
    assert wheel.count == 4000
    del wheel
    gc.collect()
    for handle in handles:
        handle.cancel()
