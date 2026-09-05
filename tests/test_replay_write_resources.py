import tracemalloc
from array import array
from collections.abc import Callable
from typing import Any

import pytest

from wreath.replay import _ReplayTransport


def transport() -> _ReplayTransport:
    return _ReplayTransport(("127.0.0.1", 1), ("127.0.0.1", 2))


class CustomBytes(bytes):
    def __bytes__(self) -> bytes:
        return b"custom bytes"


class CustomBytearray(bytearray):
    def __bytes__(self) -> bytes:
        return b"custom bytearray"


class Converted:
    def __bytes__(self) -> bytes:
        return b"converted"


@pytest.mark.parametrize("lines", [False, True])
@pytest.mark.parametrize(
    "factory",
    [
        lambda: b"bytes",
        lambda: bytearray(b"bytearray"),
        lambda: memoryview(b"readonly"),
        lambda: memoryview(bytearray(b"writable")),
        lambda: memoryview(b"abcdef")[::2],
        lambda: memoryview(b"abcdef").cast("B", shape=[2, 3]),
        lambda: memoryview(b"x").cast("B", shape=[]),
        lambda: memoryview(array("I", [1, 2, 3])),
        lambda: CustomBytes(b"original"),
        lambda: CustomBytearray(b"original"),
        Converted,
        lambda: iter([1, 2, 255]),
        lambda: 3,
    ],
)
def test_write_matches_bytes_conversion(factory: Callable[[], Any], lines: bool) -> None:
    expected = bytes(factory())
    target = transport()
    if lines:
        target.writelines([factory()])
    else:
        target.write(factory())
    assert target.buffer == expected
    assert target.write_count == 1


@pytest.mark.parametrize("lines", [False, True])
def test_contiguous_view_write_avoids_temporary_copy(lines: bool) -> None:
    payload = bytearray(b"x" * (4 << 20))
    view = memoryview(payload)
    target = transport()
    tracemalloc.start()
    try:
        if lines:
            target.writelines([view])
        else:
            target.write(view)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert target.buffer == payload
    assert target.write_count == 1
    assert peak < len(payload) * 3 // 2


@pytest.mark.parametrize("lines", [False, True])
def test_self_buffer_and_exported_alias_semantics(lines: bool) -> None:
    target = transport()
    target.write(b"ab")
    if lines:
        target.writelines([target.buffer])
    else:
        target.write(target.buffer)
    assert target.buffer == b"abab"
    assert target.write_count == 2
    view = memoryview(target.buffer)
    with pytest.raises(BufferError):
        if lines:
            target.writelines([view])
        else:
            target.write(view)
    view.release()
    assert target.buffer == b"abab"
    assert target.write_count == 2


@pytest.mark.parametrize("lines", [False, True])
def test_released_view_refusal_and_closed_transport(lines: bool) -> None:
    view = memoryview(b"value")
    view.release()
    target = transport()
    with pytest.raises(ValueError, match="released memoryview"):
        if lines:
            target.writelines([view])
        else:
            target.write(view)
    assert target.buffer == b""
    assert target.write_count == 0
    target.close()
    target.write(view)
    target.writelines(object())
    assert target.write_count == 0


def test_writelines_keeps_partial_output_without_counting_failed_batch() -> None:
    target = transport()
    with pytest.raises(TypeError):
        target.writelines([b"first", object(), b"last"])
    assert target.buffer == b"first"
    assert target.write_count == 0
    target.writelines([])
    assert target.write_count == 1


def test_writelines_iterator_failure_preserves_partial_output_and_count() -> None:
    def chunks() -> Any:
        yield b"first"
        raise ValueError("stopped iteration")

    target = transport()
    with pytest.raises(ValueError, match="stopped iteration"):
        target.writelines(chunks())
    assert target.buffer == b"first"
    assert target.write_count == 0
