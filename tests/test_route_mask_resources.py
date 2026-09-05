import gc
import tracemalloc

import pytest

from wreath._native import _core


def test_route_masks_store_unique_literals_not_route_count_per_position():
    table = _core.PolicyRouteTable()
    for index in range(4096):
        parts = [str((index >> (2 * position)) & 3) for position in range(6)]
        table.add("/api/{id}/" + "/".join(parts), "GET", index, (0,))
    gc.collect()
    tracemalloc.start()
    try:
        table.compile()
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert table.stats()["routes"] == 4096
    assert table.stats()["literal_keys"] == 25
    assert retained < 1_500_000
    for index in range(4096):
        parts = [str((index >> (2 * position)) & 3) for position in range(6)]
        assert table.match("GET", "/api/value/" + "/".join(parts), 0) == (index, {"id": "value"})
    assert table.match("GET", "/other/value/0/0/0/0/0/0", 0) is None


@pytest.mark.parametrize("prefix", ["", "x" * 25])
def test_growing_literal_maps_preserve_matches_access_and_misses(prefix):
    table = _core.PolicyRouteTable()
    for index in range(257):
        table.add(f"/api/{{id}}/{prefix}{index:04}", "GET", index, (2,))
    table.add("/api/{id}/{tail}", "GET", "fallback", (0,))
    table.compile()
    assert table.stats()["literal_keys"] == 258
    for index in range(257):
        path = f"/api/value/{prefix}{index:04}"
        assert table.match("GET", path, 2) == (index, {"id": "value"})
        assert table.match("GET", path, 0) == (
            "fallback",
            {"id": "value", "tail": f"{prefix}{index:04}"},
        )
    assert table.match("GET", f"/api/value/{prefix}0257", 2) == (
        "fallback",
        {"id": "value", "tail": f"{prefix}0257"},
    )
    assert table.match("GET", "/other/value/0000", 2) is None


def test_all_parameter_positions_and_empty_table():
    table = _core.PolicyRouteTable()
    table.compile()
    assert table.match("GET", "/value", 0) is None
    table = _core.PolicyRouteTable()
    table.add("/{first}/{second}", "GET", "first", (2,))
    table.compile()
    assert table.stats()["literal_keys"] == 0
    assert table.match("GET", "/a/b", 2) == ("first", {"first": "a", "second": "b"})
    assert table.match("GET", "/a/b", 0) is None


def test_parameter_positions_do_not_allocate_literal_tables():
    table = _core.PolicyRouteTable()
    for length in range(1, 33):
        path = "/" + "/".join(f"{{p{position}}}" for position in range(length))
        table.add(path, "GET", length, (0,))
    gc.collect()
    tracemalloc.start()
    try:
        table.compile()
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert table.stats()["routes"] == 32
    assert table.stats()["literal_keys"] == 0
    assert retained < 60_000
