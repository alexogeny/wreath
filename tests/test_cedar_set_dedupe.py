"""Cedar set construction preserves Cedar's structural equality.

Scalar identity is tagged because Cedar treats `True` and `1` as different
values while Python compares them equal and hashes them alike. Records and
nested sets use structural equality, including order-independent nested sets.
"""

from __future__ import annotations

import pytest

from wreath._auth.cedar_engine import _to_cedar_value


def _convert(value: object) -> list:
    return _to_cedar_value(value, where="test")


# -- semantics ---------------------------------------------------------------


def test_bool_and_int_are_not_merged() -> None:
    """The case a plain set() would get wrong: hash(1) == hash(True)."""
    assert _convert([True, 1, False, 0]) == [True, 1, False, 0]
    assert _convert([1, True]) == [1, True]


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


@pytest.mark.parametrize("count", [50, 100, 200, 400])
def test_large_scalar_sets_preserve_every_distinct_member(count: int) -> None:
    values = [f"group-{index}" for index in range(count)]
    assert _convert(values + values) == values


def test_structural_members_do_not_change_scalar_deduplication() -> None:
    scalars = [f"s{index}" for index in range(500)]
    assert _convert([*scalars, {"k": 1}, *scalars, {"k": 1}, {"k": 2}]) == [
        *scalars,
        {"k": 1},
        {"k": 2},
    ]
