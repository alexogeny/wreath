"""Cedar set construction dedupes in linear time without changing what it means.

A Cedar set is unordered with structural equality, so `_to_cedar_value` must
drop duplicates. It compared every candidate against every kept one, which is
O(N**2), and it runs on every `is_authorized` call -- once for the context and
once per entity attribute -- so a policy carrying a few hundred group ids paid
it per authorization. Measured before the fix: 25 elements 64us, 400 elements
14.4ms (a clean 4x per doubling); after: 6us and 82us.

Scalars now dedupe through a set. The tag in `_dedupe_key` is what keeps that
honest: `_cedar_eq` treats `True` and `1` as different, but Python compares them
equal and hashes them alike, so an untagged set would silently merge them.
"""

from __future__ import annotations

import pytest

from wreath._auth.cedar_engine import _cedar_eq, _dedupe_key, _to_cedar_value


def _convert(value: object) -> list:
    return _to_cedar_value(value, where="test")


# -- semantics ---------------------------------------------------------------


def test_bool_and_int_are_not_merged() -> None:
    """The case a plain set() would get wrong: hash(1) == hash(True)."""
    assert _convert([True, 1, False, 0]) == [True, 1, False, 0]
    assert _convert([1, True]) == [1, True]
    assert not _cedar_eq(True, 1)


def test_duplicate_scalars_collapse() -> None:
    assert _convert(["a", "a", "b", "a"]) == ["a", "b"]
    assert _convert([1, 1, 2]) == [1, 2]
    assert _convert([True, True]) == [True]


def test_first_occurrence_order_is_preserved() -> None:
    assert _convert(["c", "a", "b", "a", "c"]) == ["c", "a", "b"]


def test_records_still_dedupe_structurally() -> None:
    assert _convert([{"k": 1}, {"k": 1}, {"k": 2}]) == [{"k": 1}, {"k": 2}]


def test_nested_sets_dedupe_as_sets_not_sequences() -> None:
    """[1,2] and [2,1] are the same Cedar set, so one of them goes."""
    assert _convert([[1, 2], [2, 1], [3]]) == [[1, 2], [3]]


def test_mixed_hashable_and_structural_members() -> None:
    result = _convert(["a", {"k": 1}, "a", {"k": 1}, ["x"], ["x"], 7])
    assert result == ["a", {"k": 1}, ["x"], 7]


def test_entity_uids_dedupe() -> None:
    from wreath.authorization import EntityUid

    uid = EntityUid("User", "bo")
    assert _convert([uid, EntityUid("User", "bo"), EntityUid("User", "cy")]) == [
        ("User", "bo"),
        ("User", "cy"),
    ]


def test_dedupe_key_agrees_with_cedar_eq_for_every_hashable_kind() -> None:
    """Two values sharing a key must be `_cedar_eq`, and vice versa."""
    values = [True, False, 0, 1, 2, "", "a", "b", ("User", "bo"), ("User", "cy")]
    for left in values:
        for right in values:
            left_key, right_key = _dedupe_key(left), _dedupe_key(right)
            assert left_key is not None and right_key is not None
            assert (left_key == right_key) is _cedar_eq(left, right)


def test_unhashable_kinds_fall_back_to_structural_comparison() -> None:
    assert _dedupe_key({"k": 1}) is None
    assert _dedupe_key([1, 2]) is None


# -- complexity --------------------------------------------------------------


def _comparisons(count: int) -> int:
    """How many `_cedar_eq` calls converting `count` distinct strings costs."""
    import wreath._auth.cedar_engine as engine

    calls = 0
    original = engine._cedar_eq

    def counting(a: object, b: object) -> bool:
        nonlocal calls
        calls += 1
        return original(a, b)

    engine._cedar_eq = counting
    try:
        _convert([f"group-{index}" for index in range(count)])
    finally:
        engine._cedar_eq = original
    return calls


@pytest.mark.parametrize("count", [50, 100, 200, 400])
def test_scalar_sets_cost_no_structural_comparisons(count: int) -> None:
    """The quadratic term is gone entirely, not merely reduced."""
    assert _comparisons(count) == 0


def _record_comparisons(scalars: int) -> int:
    """`_cedar_eq` calls for two records buried among `scalars` strings."""
    import wreath._auth.cedar_engine as engine

    calls = 0
    original = engine._cedar_eq

    def counting(a: object, b: object) -> bool:
        nonlocal calls
        calls += 1
        return original(a, b)

    engine._cedar_eq = counting
    try:
        _convert([*[f"s{index}" for index in range(scalars)], {"k": 1}, {"k": 2}])
    finally:
        engine._cedar_eq = original
    return calls


def test_structural_members_compare_only_against_each_other() -> None:
    """A few records among many scalars must not rescan the scalars.

    Stated as "independent of the scalar count" rather than an exact number:
    `_cedar_eq` recurses into a record's fields, so comparing two one-field
    records is more than one call. What matters is that ten times the scalars
    costs the records nothing.
    """
    assert _record_comparisons(500) == _record_comparisons(5000)
