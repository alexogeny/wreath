"""Performance-ordering tests.

These assert the *robust, repeatable* orderings the design guarantees, using
best-of-N timing and thresholds set far below observed margins so they do not
flake under CI load. The full six-way matrix is printed (run pytest -s) for
visibility.

Every comparand is something a caller could plausibly reach for instead -- the
stdlib, or another routing backend -- so a ratio here means "faster than the
alternative", never "faster than the version this replaced".

Established orderings (observed multiples in parentheses are typical, not
asserted verbatim):
  * on a large table with many routes sharing a segment position, the three
    routing backends are comparable. This previously asserted the decision tree
    winning by ~3-4x over the trie, which held only while the trie linear-scanned
    a node's children; sorting those children and binary searching them closed
    the gap. The decision tree remains the default for capability pruning, not
    for literal dispatch -- see test_c_backends_are_comparable_at_scale below.
  * `json_dumps` beats `json.dumps` (~3.3x) and `json_loads` beats `json.loads`
    (~2.2x); `parse_qs` beats `urllib.parse.parse_qsl` (~17x, measured
    2026-08-10 against an A/A floor of 1.003x).
"""

from __future__ import annotations

import json as stdlib_json
import random
import time
import urllib.parse

import pytest
from _routing_impls import IMPLS, build

from wreath._native import _core

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

def test_json_dumps_beats_the_stdlib_encoder() -> None:
    payload = {
        "users": [
            {"id": i, "name": f"user{i}", "active": i % 2 == 0, "score": i * 1.5}
            for i in range(50)
        ]
    }

    def native():
        for _ in range(3000):
            _core.json_dumps(payload)

    def stdlib():
        for _ in range(3000):
            stdlib_json.dumps(payload, separators=(",", ":")).encode("utf-8")

    ratio = _speedup(stdlib, native)
    print(f"\njson dumps: {ratio:.2f}x stdlib")
    # Observed ~3.3x. The stdlib arm includes the `.encode()` because bytes are
    # what a response needs, and leaving it out would be timing a different job.
    assert ratio > 1.5


def test_json_loads_beats_the_stdlib_decoder() -> None:
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
    print(f"\njson loads: {ratio:.2f}x stdlib")
    # Observed ~2.2x: the native decoder parses UTF-8 bytes in place instead
    # of decoding the whole document to str first.
    assert ratio > 1.3


# --- query strings --------------------------------------------------------

def test_parse_qs_beats_the_stdlib_query_parser() -> None:
    """Against `urllib.parse.parse_qsl`, which produces the identical pairs.

    Observed 17.8x on 2026-08-10 with an A/A floor of 1.003x; asserted at 4x so
    a loaded box cannot flake it. `keep_blank_values=True` is what makes the two
    comparable -- without it the stdlib drops `flag` and is answering an easier
    question, which the equality assertion below is here to keep honest.
    """
    query = b"a=1&b=%C3%A9&flag&k=x+y&z=%20%21" * 3
    text = query.decode()
    assert _core.parse_qs(query) == urllib.parse.parse_qsl(text, keep_blank_values=True)

    def native():
        for _ in range(10000):
            _core.parse_qs(query)

    def stdlib():
        for _ in range(10000):
            urllib.parse.parse_qsl(text, keep_blank_values=True)

    ratio = _speedup(stdlib, native)
    print(f"\nparse_qs: {ratio:.2f}x urllib.parse.parse_qsl")
    assert ratio > 4.0




