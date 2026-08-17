"""Connection-owned native operation FIFO behaviour."""

from __future__ import annotations

import pytest

from wreath._native import _postgres


def test_native_operation_queue_wraps_grows_and_preserves_fifo() -> None:
    queue = _postgres._OperationQueue()
    values = [object() for _ in range(24)]
    for value in values[:8]:
        queue.append(value)
    assert [queue.popleft() for _ in range(3)] == values[:3]

    for value in values[8:]:
        queue.append(value)

    assert len(queue) == 21
    assert queue[0] is values[3]
    assert queue[-1] is values[-1]
    assert list(queue) == values[3:]


def test_native_operation_queue_clear_and_empty_refusal() -> None:
    queue = _postgres._OperationQueue()
    queue.append(object())
    queue.clear()
    assert not queue
    assert list(queue) == []
    with pytest.raises(IndexError, match="empty operation queue"):
        queue.popleft()


def test_native_connection_selects_the_connection_owned_queue() -> None:
    assert _postgres.Connection._operation_queue_type is _postgres._OperationQueue
