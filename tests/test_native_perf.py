"""Performance-ordering tests.

These assert the *robust, repeatable* orderings the design guarantees, using
best-of-N timing and thresholds set far below observed margins so they do not
flake under CI load. The full six-way matrix is printed (run pytest -s) for
visibility.

Established orderings (observed multiples in parentheses are typical, not
asserted verbatim):
  * native routers beat their pure twins (~4-6x)
  * on a large table with many routes sharing a segment position, the three C
    backends are comparable. This previously asserted the decision tree winning
    by ~3-4x over the trie, which held only while the trie linear-scanned a
    node's children; sorting those children and binary searching them closed the
    gap. The decision tree remains the default for capability pruning, not for
    literal dispatch -- see test_c_backends_are_comparable_at_scale below.
  * native json/codecs/ws/http beat their pure twins (6-24x); native json
    encode beats stdlib (~3.3x) and native json decode beats stdlib (~2.2x)

Note deliberately NOT asserted: py-dt is *not* faster than py-trie. In
interpreted Python the decision tree's per-node dict lookups cost more than
the trie's plain walk; the decision tree's win is realised in C. The default
backend is C-first, so this is the intended trade-off.
"""

from __future__ import annotations

import json as stdlib_json
import random
import time

import pytest
from _routing_impls import IMPLS, build

from wreath._native import _core
from wreath._pure import codecs as pure_codecs
from wreath._pure import http as pure_http
from wreath._pure import json as pure_json
from wreath._pure import ws as pure_ws

pytestmark = [
    pytest.mark.performance,
    pytest.mark.skipif(_core is None, reason="native extension not built"),
]


def _best(fn, reps: int = 5) -> float:
    fn()  # warm
    best = float("inf")
    for _ in range(reps):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def _speedup(slow, fast, reps: int = 5) -> float:
    return _best(slow, reps) / _best(fast, reps)


# --- routing --------------------------------------------------------------

def _large_shared_table() -> list[tuple[str, str]]:
    # 800 routes sharing the first two (parameter) positions and differing at a
    # deep literal segment: the widest literal fanout the trie's sorted-child
    # binary search has to handle, and the shape the decision tree's hashed
    # branch selection is built for.
    return [("GET", f"/api/{{a}}/{{b}}/thing{i}/{{c}}") for i in range(800)]


def _run_matches(table, queries):
    def run():
        for method, path in queries:
            table.match(method, path)

    return run


def test_native_routers_beat_pure() -> None:
    routes = [
        ("GET", "/"), ("GET", "/users"), ("GET", "/users/{id}"),
        ("GET", "/users/{id}/posts"), ("POST", "/users/{id}/posts"),
        ("GET", "/health"), ("GET", "/static/{path}"),
    ]
    rng = random.Random(7)
    choices = [
        ("GET", "/users/42"), ("GET", "/health"), ("POST", "/users/9/posts"),
        ("GET", "/static/x"), ("GET", "/nope"), ("GET", "/users/1/posts"),
    ]
    queries = [rng.choice(choices) for _ in range(20000)]

    timings = {}
    for name, factory in IMPLS.items():
        table = build(factory, routes)
        for method, path in queries[:1000]:  # compile / warm
            table.match(method, path)
        timings[name] = _best(_run_matches(table, queries))

    print("\nsmall-api routing (lower is better):")
    for name in ("c-dt", "c-trie", "c-bitset", "py-dt", "py-trie", "py-bitset"):
        if name in timings:
            print(f"  {name:9} {timings[name] * 1e3:7.2f} ms")

    assert timings["c-dt"] < timings["py-dt"] / 1.8
    assert timings["c-dt"] < timings["py-trie"] / 1.8
    assert timings["c-trie"] < timings["py-trie"] / 1.8
    assert timings["c-bitset"] < timings["py-bitset"] / 1.8


def test_c_backends_are_comparable_at_scale() -> None:
    """All three C routers handle wide shared-segment fanout in the same class.

    The decision tree hashes branch selection; the trie binary searches sorted
    children; the bitset intersects one mask per segment position. None should be
    an order of magnitude off the others.

    This is deliberately a loose bound, because the backends do not have the same
    contract and this is not an apples-to-apples race. The decision tree's and
    bitset's match() take a caller capability mask and evaluate access clauses;
    the trie's takes no mask and does no authorization work, because it has no
    such feature. On this table (no access clauses) both pay for that machinery
    and exercise none of it. Asserting an ordering here would be asserting the
    size of that overhead, which is not a design guarantee.
    """
    routes = _large_shared_table()
    queries = [
        ("GET", f"/api/1/2/thing{random.Random(i).randint(0, 799)}/3")
        for i in range(20000)
    ]
    timings = {}
    for name in ("c-dt", "c-trie", "c-bitset"):
        table = build(IMPLS[name], routes)
        table.match(*queries[0])  # compile
        timings[name] = _best(_run_matches(table, queries))

    dt_time, trie_time = timings["c-dt"], timings["c-trie"]
    print(
        "\nlarge-shared routing (800 routes, one literal position): "
        + ", ".join(f"{n} {t * 1e3:.2f} ms" for n, t in timings.items())
        + f"  (c-dt is {trie_time / dt_time:.2f}x c-trie, "
        f"{timings['c-bitset'] / dt_time:.2f}x c-bitset)"
    )
    # This asserted dt_time < trie_time / 1.3 while the trie linear-scanned all
    # 800 children at the shared position. It binary searches them now, so the
    # decision tree's hashed branch selection no longer buys a margin here and
    # the two land within ~10% of each other (the trie is typically a few
    # percent ahead). All three must still be in the same class -- an order of
    # magnitude apart would mean one of them regressed badly.
    assert dt_time < trie_time * 3, (dt_time, trie_time)
    assert trie_time < dt_time * 3, (dt_time, trie_time)
    for name in ("c-trie", "c-bitset"):
        assert timings[name] < dt_time * 3, (name, timings[name], dt_time)
        assert dt_time < timings[name] * 3, (name, timings[name], dt_time)


# --- json -----------------------------------------------------------------

def test_native_json_beats_pure_and_stdlib() -> None:
    payload = {
        "users": [
            {"id": i, "name": f"user{i}", "active": i % 2 == 0, "score": i * 1.5}
            for i in range(50)
        ]
    }

    def native():
        for _ in range(3000):
            _core.json_dumps(payload)

    def pure():
        for _ in range(3000):
            pure_json.json_dumps(payload)

    def stdlib():
        for _ in range(3000):
            stdlib_json.dumps(payload, separators=(",", ":")).encode("utf-8")

    vs_pure = _speedup(pure, native)
    vs_stdlib = _speedup(stdlib, native)
    print(f"\njson: native is {vs_pure:.2f}x pure, {vs_stdlib:.2f}x stdlib")
    assert vs_pure > 2.5
    assert vs_stdlib > 1.5


def test_native_json_loads_beats_stdlib() -> None:
    payload = {
        "users": [
            {"id": i, "name": f"user{i}", "active": i % 2 == 0, "score": i * 1.5}
            for i in range(50)
        ]
    }
    document = stdlib_json.dumps(payload).encode()

    def native():
        for _ in range(3000):
            _core.json_loads(document)

    def stdlib():
        for _ in range(3000):
            stdlib_json.loads(document)

    ratio = _speedup(stdlib, native)
    print(f"\njson loads: native is {ratio:.2f}x stdlib")
    # Observed ~2.2x: the native decoder parses UTF-8 bytes in place instead
    # of decoding the whole document to str first.
    assert ratio > 1.3


# --- codecs / ws / http ---------------------------------------------------

def test_native_parse_qs_beats_pure() -> None:
    query = b"a=1&b=%C3%A9&flag&k=x+y&z=%20%21" * 3

    def native():
        for _ in range(10000):
            _core.parse_qs(query)

    def pure():
        for _ in range(10000):
            pure_codecs.parse_qs(query)

    ratio = _speedup(pure, native)
    print(f"\nparse_qs: native is {ratio:.2f}x pure")
    assert ratio > 3.0


def test_native_ws_mask_beats_pure() -> None:
    data = bytes(random.Random(1).randint(0, 255) for _ in range(2000))
    key = b"\x01\x02\x03\x04"

    def native():
        for _ in range(10000):
            _core.ws_mask(data, key)

    def pure():
        for _ in range(10000):
            pure_ws.ws_mask(data, key)

    ratio = _speedup(pure, native)
    print(f"\nws_mask: native is {ratio:.2f}x pure")
    assert ratio > 5.0


def test_native_http_parser_beats_pure() -> None:
    request = (
        b"GET /api/v1/users?page=2 HTTP/1.1\r\nHost: example.com\r\n"
        b"Accept: application/json\r\nUser-Agent: x\r\n\r\n"
    )

    def native():
        for _ in range(10000):
            _core.http_parse_request(request)

    def pure():
        for _ in range(10000):
            pure_http.http_parse_request(request)

    ratio = _speedup(pure, native)
    print(f"\nhttp parser: native is {ratio:.2f}x pure")
    assert ratio > 3.0
