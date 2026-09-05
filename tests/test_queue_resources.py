import gc
import sys
import tracemalloc
import weakref

import pytest

from wreath._native import _core
from wreath.queue import PriorityQueue, Queue, RoundRobin


@pytest.mark.parametrize("kind", [Queue, PriorityQueue])
def test_empty_queue_does_not_reserve_capacity(kind):
    tracemalloc.start()
    try:
        queues = [kind(capacity=65536) for _ in range(4)]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert all(queue.capacity == 65536 and len(queue) == 0 for queue in queues)
    assert peak < 65536


@pytest.mark.parametrize("kind", [_core.Queue, _core.PriorityQueue])
def test_bare_new_can_accept_an_item(kind):
    queue = kind.__new__(kind)
    assert queue.capacity == 1
    assert queue.offer("first")
    assert not queue.offer("refused")
    assert queue.get_nowait() == "first"
    assert (queue.offered, queue.dropped) == (2, 1)


@pytest.mark.parametrize("kind", [Queue, PriorityQueue])
def test_reinitialization_discards_items_and_resets_counters(kind):
    queue = kind(capacity=1)
    queue.offer("old")
    queue.offer("refused")
    queue.close()
    queue.__init__(capacity=2)
    assert (len(queue), queue.offered, queue.dropped, queue.closed) == (0, 0, 0, False)
    queue.put_nowait("new")
    assert queue.get_nowait() == "new"


@pytest.mark.parametrize("kind", [Queue, PriorityQueue])
def test_clear_allows_finalizer_to_refill(kind):
    queue = kind(capacity=2)

    class Refiller:
        def __del__(self):
            queue.offer("refilled")

    queue.offer(Refiller())
    assert queue.clear() == 1
    assert queue.get_nowait() == "refilled"


@pytest.mark.parametrize("kind", [Queue, PriorityQueue])
def test_allocated_queue_cycle_releases_items(kind):
    class Item:
        def __init__(self, queue):
            self.queue = queue

    queue = kind(capacity=65536)
    item = Item(queue)
    reference = weakref.ref(item)
    queue.offer(item)
    del item, queue
    gc.collect()
    assert reference() is None


def test_empty_round_robin_lanes_do_not_reserve_capacity():
    tracemalloc.start()
    try:
        queue = RoundRobin(capacity=65536, lanes=map(str, range(32)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(queue) == 0
    assert peak < 65536


@pytest.mark.parametrize("kind", [Queue, PriorityQueue])
def test_impossible_capacity_preserves_initialized_queue(kind):
    queue = kind(capacity=2)
    queue.offer("kept")
    with pytest.raises(MemoryError):
        queue.__init__(capacity=sys.maxsize)
    assert queue.capacity == 2
    assert queue.get_nowait() == "kept"


@pytest.mark.parametrize("kind", [Queue, PriorityQueue])
def test_clear_retains_allocated_storage_for_refill(kind):
    queue = kind(capacity=65536)
    queue.offer("first")
    assert queue.clear() == 1
    tracemalloc.start()
    try:
        queue.offer("second")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert queue.get_nowait() == "second"
    assert peak < 65536
