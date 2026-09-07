import gc
import tracemalloc

import pytest

from wreath._native import _core
from wreath.signatures import SignatureError


def _compile(names, limit=64):
    covered = " ".join(f'"{name}"' for name in names)
    return _core.signature_compile_pair(
        f'sig1=({covered});created=1;keyid="fixture"',
        "sig1=:YWJj:",
        SignatureError,
        8192,
        limit,
    )


def _retained(limit):
    gc.collect()
    tracemalloc.start()
    try:
        plans = [_compile(("@method",), limit) for _ in range(8)]
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return retained, [_core.signature_plan_facts(plan) for plan in plans]


def test_signature_storage_tracks_components_not_limit():
    small, small_facts = _retained(4)
    large, large_facts = _retained(64)
    assert small_facts == large_facts
    assert len(large_facts) == 8
    assert large <= small + 1024


@pytest.mark.parametrize("count", [1, 4, 5, 8, 9, 16, 17, 32, 33, 64])
def test_component_growth_preserves_facts(count):
    names = tuple(f"x-field-{index}" for index in range(count))
    facts = _core.signature_plan_facts(_compile(names))
    assert facts == ({"created": 1, "keyid": "fixture"}, b"abc", names)


@pytest.mark.parametrize("limit", [0, 1, 4, 63])
def test_component_limit_is_still_enforced(limit):
    names = tuple(f"x-field-{index}" for index in range(limit + 1))
    with pytest.raises(SignatureError, match="signature covers too many components"):
        _compile(names, limit)


def test_empty_components_are_still_refused():
    with pytest.raises(SignatureError, match="signature covers no components"):
        _compile(())
