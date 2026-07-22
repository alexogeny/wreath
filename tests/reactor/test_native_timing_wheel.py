"""Native timing wheel (wreath._native._reactor) — the first C reactor primitive.

O(1) insert/cancel with a fixed slot array; the backing store the loop's
call_later/call_at will use in place of asyncio's heap. Unlike the rest of this
directory these are GREEN now — the C primitive exists ahead of its wiring.
"""
from __future__ import annotations

import pytest

_reactor = pytest.importorskip("wreath._native._reactor")
TimingWheel = _reactor.TimingWheel


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
