"""Performance-ordering tests.

These assert the *robust, repeatable* orderings the design guarantees, using
best-of-N timing and thresholds set far below observed margins so they do not
flake under CI load. Each ratio is printed (run pytest -s) for visibility.

Every comparand is a stdlib function a caller could plausibly reach for instead,
so a ratio here means "faster than the alternative", never "faster than the
version this replaced".

Established orderings (observed multiples in parentheses are typical, not
asserted verbatim): `json_dumps` beats `json.dumps` (~3.3x), `json_loads`
beats `json.loads` (~2.2x), and `parse_qs` beats `urllib.parse.parse_qsl`
(~17x, measured 2026-08-10 against an A/A floor of 1.003x).
"""

from __future__ import annotations

import json as stdlib_json
import time
import urllib.parse

import pytest

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


