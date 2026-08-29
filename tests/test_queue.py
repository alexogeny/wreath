from __future__ import annotations

import asyncio
import threading

import pytest

from wreath.queue import (
    PriorityQueue,
    Queue,
    QueueEmpty,
    QueueFull,
    RoundRobin,
)

ARMS = [Queue]
ARM_IDS = ["queue"]

HEAPS = [PriorityQueue]
HEAP_IDS = ["priority-queue"]


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestBounds:
    def test_offer_keeps_items_until_the_capacity_is_reached(self, arm) -> None:
        queue = arm(capacity=3)
        assert [queue.offer(i) for i in range(5)] == [True, True, True, False, False]
        assert len(queue) == 3
        assert queue.offered == 5
        assert queue.dropped == 2

    def test_a_full_queue_refuses_the_newest_by_default(self, arm) -> None:
        queue = arm(capacity=3)
        for i in range(5):
            queue.offer(i)
        assert queue.drain() == [0, 1, 2]

    def test_drop_oldest_evicts_the_front_instead(self, arm) -> None:
        queue = arm(capacity=3, drop_oldest=True)
        for i in range(5):
            assert queue.offer(i) is True
        assert queue.drain() == [2, 3, 4]
        # Still counted: something was lost either way, and an operator reading
        # `dropped` wants to know that.
        assert queue.dropped == 2

    def test_put_nowait_raises_rather_than_losing_the_item(self, arm) -> None:
        queue = arm(capacity=2)
        queue.put_nowait("a")
        queue.put_nowait("b")
        with pytest.raises(QueueFull):
            queue.put_nowait("c")
        assert queue.drain() == ["a", "b"]

    def test_capacity_must_be_positive(self, arm) -> None:
        with pytest.raises(ValueError, match="capacity must be positive"):
            arm(capacity=0)

    def test_capacity_of_one_is_a_latch(self, arm) -> None:
        queue = arm(capacity=1, drop_oldest=True)
        for i in range(5):
            queue.offer(i)
        assert queue.drain() == [4]


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestDraining:
    def test_drain_returns_everything_oldest_first(self, arm) -> None:
        queue = arm(capacity=8)
        for i in range(5):
            queue.offer(i)
        assert queue.drain() == [0, 1, 2, 3, 4]
        assert len(queue) == 0

    def test_drain_respects_a_limit(self, arm) -> None:
        queue = arm(capacity=8)
        for i in range(5):
            queue.offer(i)
        assert queue.drain(2) == [0, 1]
        assert queue.drain(99) == [2, 3, 4]

    def test_draining_an_empty_queue_is_an_empty_list(self, arm) -> None:
        assert arm(capacity=4).drain() == []

    def test_a_negative_limit_takes_nothing(self, arm) -> None:
        queue = arm(capacity=4)
        queue.offer("a")
        assert queue.drain(-1) == []
        assert len(queue) == 1

    def test_get_nowait_takes_the_oldest(self, arm) -> None:
        queue = arm(capacity=4)
        queue.offer("first")
        queue.offer("second")
        assert queue.get_nowait() == "first"
        assert queue.get_nowait() == "second"
        with pytest.raises(QueueEmpty):
            queue.get_nowait()

    def test_snapshot_shows_the_backlog_without_consuming_it(self, arm) -> None:
        queue = arm(capacity=8)
        for i in range(3):
            queue.offer(i)
        assert queue.snapshot() == [0, 1, 2]
        # The distinction that makes it worth having: asking what is queued must
        # not be the same act as taking it.
        assert queue.snapshot() == [0, 1, 2]
        assert len(queue) == 3
        assert queue.drain() == [0, 1, 2]
        assert queue.snapshot() == []

    def test_snapshot_follows_the_ring_around(self, arm) -> None:
        queue = arm(capacity=4)
        for i in range(3):
            queue.offer(i)
        queue.drain(2)
        queue.offer(3)
        queue.offer(4)
        assert queue.snapshot() == [2, 3, 4]

    def test_clear_reports_how_many_went(self, arm) -> None:
        queue = arm(capacity=8)
        for i in range(5):
            queue.offer(i)
        assert queue.clear() == 5
        assert len(queue) == 0

    def test_the_ring_wraps_without_reordering(self, arm) -> None:
        queue = arm(capacity=4)
        for cycle in range(50):
            for i in range(3):
                queue.offer((cycle, i))
            assert queue.drain(3) == [(cycle, 0), (cycle, 1), (cycle, 2)]


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestClosing:
    def test_a_closed_queue_refuses_new_items(self, arm) -> None:
        queue = arm(capacity=4)
        queue.close()
        assert queue.closed is True
        with pytest.raises(RuntimeError, match="closed"):
            queue.offer("x")
        with pytest.raises(RuntimeError, match="closed"):
            queue.put_nowait("x")

    def test_the_backlog_is_still_drainable_after_closing(self, arm) -> None:
        queue = arm(capacity=4)
        queue.offer("a")
        queue.offer("b")
        queue.close()
        assert queue.drain() == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestAwaiting:
    async def test_an_available_item_resolves_without_suspending(self, arm) -> None:
        queue = arm(capacity=4)
        queue.offer("ready")
        assert await queue.get() == "ready"

    async def test_a_tuple_payload_survives_the_fast_path(self, arm) -> None:
        # StopIteration carries the value, and a tuple is the case where
        # PyErr_SetObject would have unpacked it into constructor arguments.
        queue = arm(capacity=4)
        queue.offer(("a", "tuple"))
        assert await queue.get() == ("a", "tuple")

    async def test_an_exception_instance_is_an_ordinary_payload(self, arm) -> None:
        queue = arm(capacity=4)
        error = ValueError("carried, not raised")
        queue.offer(error)
        delivered = await queue.get()
        assert delivered is error

    async def test_an_empty_queue_waits_for_a_producer_on_the_same_loop(self, arm) -> None:
        queue = arm(capacity=4)

        async def produce() -> None:
            await asyncio.sleep(0.01)
            queue.offer("late")

        asyncio.create_task(produce())
        assert await queue.get() == "late"

    async def test_a_producer_on_another_thread_wakes_the_waiter(self, arm) -> None:
        queue = arm(capacity=4)

        def produce() -> None:
            queue.offer("from-a-thread")

        threading.Timer(0.01, produce).start()
        assert await asyncio.wait_for(queue.get(), timeout=5.0) == "from-a-thread"

    async def test_cancelling_a_waiter_leaves_the_queue_usable(self, arm) -> None:
        queue = arm(capacity=4)
        task = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # A cancelled Future left in the waiter deque would swallow the next
        # producer's wake-up and strand the item.
        assert queue.waiting is False
        queue.offer("after-the-cancellation")
        assert await asyncio.wait_for(queue.get(), timeout=5.0) == "after-the-cancellation"

    async def test_two_waiters_each_get_one_item(self, arm) -> None:
        queue = arm(capacity=8)
        both = asyncio.gather(queue.get(), queue.get())
        await asyncio.sleep(0)
        queue.offer(1)
        queue.offer(2)
        assert sorted(await asyncio.wait_for(both, timeout=5.0)) == [1, 2]

    async def test_more_waiters_than_items_leaves_the_rest_waiting(self, arm) -> None:
        queue = arm(capacity=8)
        first = asyncio.create_task(queue.get())
        second = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        queue.offer("only-one")
        done, pending = await asyncio.wait_for(
            asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED),
            timeout=5.0,
        )
        assert len(done) == 1
        assert len(pending) == 1
        assert done.pop().result() == "only-one"
        for task in pending:
            task.cancel()

    async def test_closing_wakes_a_waiter_rather_than_stranding_it(self, arm) -> None:
        queue = arm(capacity=4)
        task = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        queue.close()
        with pytest.raises(QueueEmpty):
            await asyncio.wait_for(task, timeout=5.0)

    async def test_an_item_offered_during_the_parking_race_is_not_lost(self, arm) -> None:
        # The window this covers: `get()` finds the ring empty, and a producer
        # offers before the waiter becomes visible. Without the re-check after
        # parking, nothing would wake and the item would sit there.
        queue = arm(capacity=4)
        for _ in range(200):
            waiter = asyncio.create_task(queue.get())
            queue.offer("raced")
            assert await asyncio.wait_for(waiter, timeout=5.0) == "raced"


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_offering_from_many_threads_loses_nothing_it_did_not_count(arm) -> None:
    queue = arm(capacity=10_000)
    threads = [
        threading.Thread(target=lambda base=base: [queue.offer(base + i) for i in range(500)])
        for base in range(0, 8000, 1000)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    drained = queue.drain()
    assert queue.offered == 4000
    assert queue.dropped == 0
    assert len(drained) == 4000
    assert len(set(drained)) == 4000, "no item was written into another's slot"


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_producers_and_a_drainer_agree_on_the_total(arm) -> None:
    queue = arm(capacity=64, drop_oldest=False)
    collected: list[object] = []
    stop = threading.Event()

    def drain_until_stopped() -> None:
        while not stop.is_set() or len(queue) > 0:
            collected.extend(queue.drain(16))

    drainer = threading.Thread(target=drain_until_stopped)
    drainer.start()
    producers = [
        threading.Thread(target=lambda: [queue.offer(object()) for _ in range(2000)])
        for _ in range(4)
    ]
    for thread in producers:
        thread.start()
    for thread in producers:
        thread.join()
    stop.set()
    drainer.join()
    assert queue.offered == 8000
    assert len(collected) == queue.offered - queue.dropped


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_the_queue_releases_what_it_held(arm) -> None:
    import weakref

    class Held:
        pass

    queue = arm(capacity=4)
    item = Held()
    reference = weakref.ref(item)
    queue.offer(item)
    del item
    assert reference() is not None
    queue.clear()
    assert reference() is None


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_a_cycle_through_the_queue_is_collectable(arm) -> None:
    import gc
    import weakref

    class Node:
        def __init__(self) -> None:
            self.queue = None

    queue = arm(capacity=4)
    node = Node()
    node.queue = queue
    queue.offer(node)
    reference = weakref.ref(node)
    del node, queue
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestLifo:
    def test_a_lifo_serves_the_newest_first(self, arm) -> None:
        queue = arm(capacity=8, lifo=True)
        for i in range(4):
            queue.offer(i)
        assert queue.get_nowait() == 3
        assert queue.drain() == [2, 1, 0]

    def test_a_lifo_snapshot_follows_the_discipline(self, arm) -> None:
        # A snapshot that disagreed with the queue's own order would be a
        # debugging aid that lies about what happens next.
        queue = arm(capacity=8, lifo=True)
        for i in range(3):
            queue.offer(i)
        assert queue.snapshot() == [2, 1, 0]
        assert queue.snapshot() == [queue.get_nowait(), queue.get_nowait(), queue.get_nowait()]

    def test_a_lifo_still_bounds_and_counts(self, arm) -> None:
        queue = arm(capacity=2, lifo=True)
        assert [queue.offer(i) for i in range(4)] == [True, True, False, False]
        assert queue.dropped == 2
        assert queue.drain() == [1, 0]

    def test_a_lifo_ring_wraps_without_reordering(self, arm) -> None:
        queue = arm(capacity=4, lifo=True)
        for cycle in range(50):
            for i in range(3):
                queue.offer((cycle, i))
            assert queue.drain(3) == [(cycle, 2), (cycle, 1), (cycle, 0)]

    def test_fifo_is_the_default(self, arm) -> None:
        queue = arm(capacity=4)
        assert queue.lifo is False
        for i in range(3):
            queue.offer(i)
        assert queue.get_nowait() == 0

    @pytest.mark.asyncio
    async def test_a_lifo_await_takes_the_newest(self, arm) -> None:
        queue = arm(capacity=4, lifo=True)
        queue.offer("old")
        queue.offer("new")
        assert await queue.get() == "new"


@pytest.mark.parametrize("heap", HEAPS, ids=HEAP_IDS)
class TestPriority:
    def test_lowest_number_comes_out_first(self, heap) -> None:
        queue = heap(capacity=8)
        for item, priority in [("c", 3), ("a", 1), ("b", 2)]:
            queue.offer(item, priority)
        assert queue.drain() == ["a", "b", "c"]

    def test_ties_come_out_in_the_order_they_went_in(self, heap) -> None:
        # Most items in a real workload share a priority, so without stability
        # the *common* case is unordered.
        queue = heap(capacity=16)
        for i in range(10):
            queue.offer(f"item-{i}", 5)
        assert queue.drain() == [f"item-{i}" for i in range(10)]

    def test_stability_holds_across_interleaved_priorities(self, heap) -> None:
        queue = heap(capacity=32)
        expected = []
        for index, priority in enumerate([2, 1, 2, 1, 3, 1, 2, 3]):
            queue.offer((priority, index), priority)
        for priority in (1, 2, 3):
            expected += [
                (priority, index)
                for index, p in enumerate([2, 1, 2, 1, 3, 1, 2, 3])
                if p == priority
            ]
        assert queue.drain() == expected

    def test_the_item_is_never_compared(self, heap) -> None:
        class Unorderable:
            def __lt__(self, other: object) -> bool:
                raise AssertionError("the payload must never be compared")

            def __gt__(self, other: object) -> bool:
                raise AssertionError("the payload must never be compared")

        queue = heap(capacity=8)
        for _ in range(5):
            queue.offer(Unorderable(), 1.0)
        assert len(queue.drain()) == 5

    def test_a_full_queue_refuses_and_counts(self, heap) -> None:
        queue = heap(capacity=3)
        assert [queue.offer(f"p{p}", p) for p in (5, 1, 9, 0, 7)] == [
            True,
            True,
            True,
            False,
            False,
        ]
        assert queue.dropped == 2
        assert queue.drain() == ["p1", "p5", "p9"]

    def test_drop_lowest_lets_an_urgent_item_displace_a_queued_one(self, heap) -> None:
        queue = heap(capacity=3, drop_lowest=True)
        assert [queue.offer(f"p{p}", p) for p in (5, 1, 9, 0, 7)] == [True, True, True, True, False]
        # 9 was displaced by 0; 7 is worse than everything left, so it is refused.
        assert queue.drain() == ["p0", "p1", "p5"]
        assert queue.dropped == 2

    def test_peek_does_not_consume(self, heap) -> None:
        queue = heap(capacity=4)
        queue.offer("low", 5.0)
        queue.offer("high", 1.0)
        assert queue.peek() == "high"
        assert len(queue) == 2
        assert queue.peek() == "high"
        assert heap(capacity=4).peek("nothing") == "nothing"

    def test_snapshot_is_in_the_order_get_would_return(self, heap) -> None:
        queue = heap(capacity=8)
        for item, priority in [("c", 3), ("a", 1), ("b", 2)]:
            queue.offer(item, priority)
        assert queue.snapshot() == [(1.0, "a"), (2.0, "b"), (3.0, "c")]
        assert len(queue) == 3
        assert queue.drain() == ["a", "b", "c"]

    def test_nan_is_refused(self, heap) -> None:
        with pytest.raises(ValueError, match="NaN"):
            heap(capacity=4).offer("item", float("nan"))

    def test_negative_and_fractional_priorities_order_correctly(self, heap) -> None:
        queue = heap(capacity=8)
        for priority in (0.5, -3.0, 2.25, -0.5, 0.0):
            queue.offer(priority, priority)
        assert queue.drain() == [-3.0, -0.5, 0.0, 0.5, 2.25]

    def test_capacity_must_be_positive(self, heap) -> None:
        with pytest.raises(ValueError, match="capacity must be positive"):
            heap(capacity=0)

    def test_get_nowait_and_close(self, heap) -> None:
        queue = heap(capacity=4)
        with pytest.raises(QueueEmpty):
            queue.get_nowait()
        queue.offer("x", 1)
        queue.close()
        with pytest.raises(RuntimeError, match="closed"):
            queue.offer("y", 1)
        assert queue.drain() == ["x"]

    def test_clear_reports_how_many_went(self, heap) -> None:
        queue = heap(capacity=8)
        for i in range(5):
            queue.offer(i, i)
        assert queue.clear() == 5
        assert len(queue) == 0

    @pytest.mark.asyncio
    async def test_awaiting_resolves_and_waits(self, heap) -> None:
        queue = heap(capacity=4)
        queue.offer("ready", 1.0)
        assert await queue.get() == "ready"

        async def produce() -> None:
            await asyncio.sleep(0.01)
            queue.offer("late", 1.0)

        asyncio.create_task(produce())
        assert await asyncio.wait_for(queue.get(), timeout=5.0) == "late"

    def test_a_heavy_randomised_workload_stays_ordered(self, heap) -> None:
        import random

        rng = random.Random(0xC0FFEE)
        queue = heap(capacity=4096)
        model: list[tuple[float, int, int]] = []
        sequence = 0
        for step in range(4000):
            if rng.random() < 0.6 and len(model) < 4096:
                priority = float(rng.randrange(8))
                queue.offer(step, priority)
                model.append((priority, sequence, step))
                sequence += 1
            elif model:
                model.sort(key=lambda entry: entry[:2])
                assert queue.get_nowait() == model.pop(0)[2]
        model.sort(key=lambda entry: entry[:2])
        assert queue.drain() == [entry[2] for entry in model]


class TestRoundRobin:
    def test_lanes_take_turns(self) -> None:
        work = RoundRobin(capacity=8)
        for lane in ("a", "b", "c"):
            for i in range(3):
                work.offer(lane, f"{lane}{i}")
        assert work.drain() == ["a0", "b0", "c0", "a1", "b1", "c1", "a2", "b2", "c2"]

    def test_one_busy_lane_does_not_starve_the_others(self) -> None:
        # The whole reason this exists: on a single shared queue, "a" would be
        # served a hundred times before "b" was served once.
        work = RoundRobin(capacity=256)
        for i in range(100):
            work.offer("noisy", i)
        work.offer("quiet", "important")
        served = [work.get_nowait(), work.get_nowait()]
        assert "important" in served

    def test_an_empty_lane_is_skipped_without_losing_the_rotation(self) -> None:
        work = RoundRobin(capacity=8, lanes=("a", "b", "c"))
        work.offer("a", 1)
        work.offer("c", 2)
        assert work.get_nowait() == 1
        assert work.get_nowait() == 2
        with pytest.raises(QueueEmpty):
            work.get_nowait()

    def test_the_cursor_advances_past_whichever_lane_answered(self) -> None:
        work = RoundRobin(capacity=8)
        work.offer("a", "a1")
        work.offer("a", "a2")
        work.offer("b", "b1")
        # Not a1, a2, b1: after "a" answers, the cursor moves on.
        assert work.drain() == ["a1", "b1", "a2"]

    def test_capacity_is_per_lane(self) -> None:
        work = RoundRobin(capacity=2)
        assert [work.offer("a", i) for i in range(4)] == [True, True, False, False]
        # "b" is untouched by "a" filling up, which is the point of a lane.
        assert work.offer("b", "fine") is True
        assert work.dropped == 2
        assert work.offered == 5

    def test_lanes_are_bounded(self) -> None:
        work = RoundRobin(capacity=4, max_lanes=3)
        for name in ("a", "b", "c"):
            work.offer(name, 1)
        with pytest.raises(RuntimeError, match="ceiling of 3"):
            work.offer("d", 1)
        # An existing lane still works; only new ones are refused.
        assert work.offer("a", 2) is True

    def test_declared_lanes_fix_the_rotation_order(self) -> None:
        work = RoundRobin(capacity=4, lanes=("z", "y", "x"))
        assert work.lanes == ("z", "y", "x")
        for lane in ("x", "y", "z"):
            work.offer(lane, lane)
        assert work.drain() == ["z", "y", "x"]

    def test_snapshot_shows_each_lane_without_consuming(self) -> None:
        work = RoundRobin(capacity=8)
        work.offer("a", 1)
        work.offer("b", 2)
        assert work.snapshot() == {"a": [1], "b": [2]}
        assert len(work) == 2

    def test_drain_respects_a_limit_and_stays_interleaved(self) -> None:
        work = RoundRobin(capacity=8)
        for lane in ("a", "b"):
            for i in range(3):
                work.offer(lane, f"{lane}{i}")
        assert work.drain(limit=4) == ["a0", "b0", "a1", "b1"]
        assert len(work) == 2

    def test_an_empty_scheduler_reports_empty(self) -> None:
        with pytest.raises(QueueEmpty, match="no lanes"):
            RoundRobin(capacity=4).get_nowait()

    def test_bounds_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="capacity must be positive"):
            RoundRobin(capacity=0)
        with pytest.raises(ValueError, match="max_lanes must be positive"):
            RoundRobin(capacity=4, max_lanes=0)


# These are about *consistency* rather than about any one container. Each was
# an asymmetry found by listing the four public types side by side: the same
# idea spelled two ways is something a reader has to intern twice, and the
# second spelling is the one they will get wrong.

FAMILY = [Queue, PriorityQueue, RoundRobin]
FAMILY_IDS = ["queue", "priority", "roundrobin"]


def _fill(container, count: int) -> None:
    """Put `count` items in, whatever the container's offer signature is."""
    for index in range(count):
        if container.__class__ is RoundRobin:
            container.offer(f"lane{index % 2}", index)
        elif container.__class__ is PriorityQueue:
            container.offer(index, float(index))
        else:
            container.offer(index)


@pytest.mark.parametrize("kind", FAMILY, ids=FAMILY_IDS)
class TestOneFamilyOneSurface:
    def test_clear_reports_how_many_went(self, kind) -> None:
        container = kind(capacity=16)
        _fill(container, 5)
        assert container.clear() == 5
        assert len(container) == 0

    def test_peek_does_not_consume(self, kind) -> None:
        container = kind(capacity=16)
        _fill(container, 3)
        first = container.peek()
        assert first is not None
        assert len(container) == 3
        assert container.peek() == first
        assert container.get_nowait() == first

    def test_peek_of_an_empty_container_is_the_default(self, kind) -> None:
        assert kind(capacity=4).peek("nothing") == "nothing"

    def test_capacity_offered_and_dropped_all_read_the_same_way(self, kind) -> None:
        container = kind(capacity=16)
        _fill(container, 4)
        assert container.capacity == 16
        assert container.offered == 4
        assert container.dropped == 0

    def test_put_nowait_raises_rather_than_dropping(self, kind) -> None:
        container = kind(capacity=2)
        # Filled into *one* lane for RoundRobin, because its `capacity` is per
        # lane and not in total -- the single place where the shared surface
        # means something different, and the reason it is spelled out here
        # rather than left to a helper that spreads items around.
        if kind is RoundRobin:
            container.offer("lane0", 1)
            container.offer("lane0", 2)
        else:
            _fill(container, 2)
        with pytest.raises(QueueFull):
            if kind is RoundRobin:
                container.put_nowait("lane0", "overflow")
            elif kind is PriorityQueue:
                container.put_nowait("overflow", 99.0)
            else:
                container.put_nowait("overflow")
        # Nothing was lost, so nothing is counted as lost.
        assert container.dropped == 0
        assert len(container) == 2

    def test_close_then_refuse_but_still_drain(self, kind) -> None:
        container = kind(capacity=16)
        _fill(container, 2)
        container.close()
        assert container.closed is True
        with pytest.raises(RuntimeError, match="closed"):
            _fill(container, 1)
        assert len(container.drain()) == 2

    def test_a_subclass_chains_init_like_any_python_class(self, kind) -> None:
        # Both rings used to configure themselves in `__new__`, which meant a
        # subclass had to override `__new__` and must never call
        # `super().__init__` -- a rule that lived only in a comment, and one
        # `wreath.kv.KV` next door did not share.
        seen: list[int] = []

        class Subclass(kind):  # type: ignore[misc,valid-type]
            __slots__ = ()

            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                seen.append(self.capacity)

        container = Subclass(capacity=7)
        assert seen == [7]
        assert container.capacity == 7


@pytest.mark.asyncio
class TestRoundRobinAwaits:
    async def test_an_available_item_resolves_without_suspending(self) -> None:
        work = RoundRobin(capacity=8)
        work.offer("a", "ready")
        assert await work.get() == "ready"

    async def test_it_waits_for_whichever_lane_fills(self) -> None:
        work = RoundRobin(capacity=8, lanes=("a", "b"))

        async def produce() -> None:
            await asyncio.sleep(0.01)
            work.offer("b", "late")

        asyncio.create_task(produce())
        assert await asyncio.wait_for(work.get(), timeout=5.0) == "late"

    async def test_a_lane_created_after_the_getter_parked_still_wakes_it(self) -> None:
        # The case the wiring is easiest to get wrong: arming happens per lane,
        # so a lane that did not exist when the getter parked has to be armed
        # as it is created or its first item wakes nobody.
        work = RoundRobin(capacity=8)
        getter = asyncio.create_task(work.get())
        await asyncio.sleep(0)
        work.offer("brand-new", "hello")
        assert await asyncio.wait_for(getter, timeout=5.0) == "hello"

    async def test_a_producer_on_another_thread_wakes_it(self) -> None:
        work = RoundRobin(capacity=8, lanes=("a",))
        threading.Timer(0.01, lambda: work.offer("a", "from-a-thread")).start()
        assert await asyncio.wait_for(work.get(), timeout=5.0) == "from-a-thread"

    async def test_cancelling_leaves_it_usable(self) -> None:
        work = RoundRobin(capacity=8, lanes=("a",))
        task = asyncio.create_task(work.get())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        work.offer("a", "after")
        assert await asyncio.wait_for(work.get(), timeout=5.0) == "after"

    async def test_closing_wakes_a_parked_getter(self) -> None:
        work = RoundRobin(capacity=8, lanes=("a",))
        task = asyncio.create_task(work.get())
        await asyncio.sleep(0)
        work.close()
        with pytest.raises(QueueEmpty):
            await asyncio.wait_for(task, timeout=5.0)


def test_kv_and_queue_agree_on_what_clear_returns() -> None:
    from wreath.kv import KV

    table = KV(max_entries=8)
    table.set("a", 1)
    table.set("b", 2)
    queue = Queue(capacity=8)
    queue.offer(1)
    queue.offer(2)
    assert table.clear() == queue.clear() == 2
