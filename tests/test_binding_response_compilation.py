"""The compiled response walks must agree with the ones they replaced.

`_response_input` and `_jsonable` read an annotation and a value together.
`_compile_response_input` and `_compile_jsonable` read the annotation once, at
route-compile time, and return a closure that reads only the value. That is a
faster shape and an easy one to get subtly wrong: a compiled walk that resolves
an item type one node too early, or that decides from the annotation something
the original decided from the value, still returns *a* result for every input
the existing suite happens to contain.

So the originals stay as the definition and every case below is crossed against
them. The matrix is deliberately hostile where the two could disagree:

* `str`- and `int`-backed enums, which *are* `str` and `int` and must still
  reduce to their `.value` -- the exact-type fast path in `_JSON_SCALARS` exists
  precisely here, and an `isinstance` there would silently pass this file's
  happy cases and corrupt these;
* values whose runtime type contradicts the annotation, because `_jsonable`
  dispatches on the value and only consults the annotation for item types;
* a self-referential dataclass, where the compiled walk cannot be finite and
  must fall back to interpreting that node;
* `Annotated[...]` carrying a `Field`, which both walks strip.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import uuid
from decimal import Decimal
from typing import Annotated, Any

import pytest

from wreath.binding import (
    Field,
    _compile_jsonable,
    _compile_response_input,
    _jsonable,
    _projection_is_identity,
    _response_input,
)


class Colour(enum.StrEnum):
    """A `str` *and* an `Enum`, which is the whole hazard: `isinstance(RED, str)`
    is true, `type(RED) is str` is not, and `_jsonable` must still take `.value`."""

    RED = "red"
    BLUE = "blue"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 9


@dataclasses.dataclass
class Point:
    x: int
    y: int


@dataclasses.dataclass
class Tagged:
    name: Annotated[str, Field(alias="label")]
    weight: float


@dataclasses.dataclass
class Node:
    """Self-referential: no compiled walk over this can terminate."""

    value: int
    child: Node | None = None


@dataclasses.dataclass
class Recursive:
    """A direct recursion, used to exercise the compiler's cycle cut."""

    child: Recursive


@dataclasses.dataclass
class Holder:
    points: list[Point]
    lookup: dict[str, Point]
    when: datetime.datetime


@dataclasses.dataclass
class Found:
    kind: str
    value: int


@dataclasses.dataclass
class Missing:
    kind: str
    reason: str


@dataclasses.dataclass
class ResultEnvelope:
    result: Found | Missing


_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
_WHEN = datetime.datetime(2026, 7, 31, 12, 30, tzinfo=datetime.UTC)

#: (annotation, value). Values are not required to satisfy the annotation --
#: disagreement is exactly where a compiled walk goes wrong.
CASES: list[tuple[Any, Any]] = [
    (str, "plain"),
    (int, 7),
    (bool, True),
    (float, 1.5),
    (type(None), None),
    (Any, {"anything": [1, "two", None]}),
    (dict[str, Any], {"id": "42", "ok": True}),
    (dict[str, str], {"id": "42", "ok": "1"}),
    (dict[str, int], {}),
    (list[int], [1, 2, 3]),
    (list[str], []),
    (list[Any], [1, "two", None, _UUID]),
    (set[int], {1, 2, 3}),
    (frozenset[str], frozenset({"a"})),
    (tuple[int, str], (1, "two")),
    (tuple[int, ...], (1, 2, 3)),
    (list[list[int]], [[1], [2, 3]]),
    (dict[str, list[int]], {"a": [1, 2]}),
    (str | None, None),
    (str | None, "here"),
    (int | str, 5),
    (Point, Point(1, 2)),
    (list[Point], [Point(1, 2), Point(3, 4)]),
    (dict[str, Point], {"origin": Point(0, 0)}),
    (Point | None, None),
    (Found | Missing, Missing("missing", "gone")),
    (ResultEnvelope, ResultEnvelope(Missing("missing", "gone"))),
    (Tagged, Tagged("ada", 1.5)),
    (list[Tagged], [Tagged("ada", 1.5)]),
    (Holder, Holder([Point(1, 2)], {"a": Point(3, 4)}, _WHEN)),
    (Node, Node(1, Node(2, Node(3)))),
    (list[Node], [Node(1, Node(2))]),
    # Enums that are also primitives: the exact-type fast path must not claim
    # these, and the annotation must not be trusted over the value.
    (Colour, Colour.RED),
    (str, Colour.RED),
    (dict[str, str], {"shade": Colour.BLUE}),
    (list[str], [Colour.RED, Colour.BLUE]),
    (Level, Level.HIGH),
    (int, Level.LOW),
    (dict[str, int], {"level": Level.HIGH}),
    (list[Any], [Colour.RED, Level.LOW]),
    # Values whose type contradicts the annotation.
    (Point, {"x": 1, "y": 2}),
    (Point, {"x": 1, "y": 2, "undeclared": 3}),
    (Point, [1, 2]),
    (Point, "not a point at all"),
    (dict[str, int], ["not", "a", "mapping"]),
    (list[int], {"not": "a list"}),
    (int, "type disagreement"),
    (Tagged, {"label": "ada", "weight": 1.5}),
    (Tagged, {"name": "ada", "weight": 1.5}),
    # Scalars that are not JSON primitives.
    (uuid.UUID, _UUID),
    (Any, _UUID),
    (Decimal, Decimal("1.25")),
    (dict[str, Decimal], {"cost": Decimal("9.99")}),
    (bytes, b"\x00\xff binary"),
    (dict[str, bytes], {"blob": b"\x01\x02"}),
    (datetime.datetime, _WHEN),
    (datetime.date, _WHEN.date()),
    (datetime.time, _WHEN.time()),
    (list[datetime.datetime], [_WHEN, _WHEN]),
    # Annotated stripping.
    (Annotated[int, Field(ge=0)], 5),
    (Annotated[list[int], Field()], [1, 2]),
    (dict[str, Annotated[str, Field()]], {"a": "b"}),
    # Unparameterized containers: the item type is `Any`.
    (list, [1, _UUID]),
    (dict, {"a": _UUID}),
]


def _identifier(case: tuple[Any, Any]) -> str:
    annotation, value = case
    return f"{getattr(annotation, '__name__', annotation)!s}<-{type(value).__name__}"


@pytest.mark.parametrize("annotation,value", CASES, ids=[_identifier(c) for c in CASES])
def test_the_compiled_projection_agrees_with_the_interpreted_one(
    annotation: Any, value: Any
) -> None:
    expected = _response_input(annotation, value)
    actual = _compile_response_input(annotation)(value)
    assert actual == expected
    assert type(actual) is type(expected)


@pytest.mark.parametrize("annotation,value", CASES, ids=[_identifier(c) for c in CASES])
def test_the_compiled_json_conversion_agrees_with_the_interpreted_one(
    annotation: Any, value: Any
) -> None:
    expected = _jsonable(annotation, value)
    actual = _compile_jsonable(annotation)(value)
    assert actual == expected
    assert type(actual) is type(expected)


@pytest.mark.parametrize("annotation,value", CASES, ids=[_identifier(c) for c in CASES])
def test_the_two_compiled_walks_compose_as_the_interpreted_ones_do(
    annotation: Any, value: Any
) -> None:
    """Projection feeds conversion, so they must agree end to end as well."""
    expected = _jsonable(annotation, _response_input(annotation, value))
    project = _compile_response_input(annotation)
    to_json = _compile_jsonable(annotation)
    assert to_json(project(value)) == expected


#: The subset of `CASES` whose annotation `_projection_is_identity` vouches for.
#: Filtered rather than skipped, so the run reports the cases that actually
#: carry the assertion instead of a wall of skips for cases making no claim.
CLAIMED = [case for case in CASES if _projection_is_identity(case[0])]


@pytest.mark.parametrize("annotation,value", CLAIMED, ids=[_identifier(c) for c in CLAIMED])
def test_claiming_the_projection_is_identity_means_it_is(annotation: Any, value: Any) -> None:
    """`compile_response_validator` skips the projection on this claim.

    A wrong `True` is silent: the validator is handed the raw handler value
    instead of the projected one, which for a dataclass means undeclared keys
    reach it and for a sequence means a `tuple` arrives where a `list` was
    expected. So the claim is checked against the projection itself, over the
    same matrix -- one direction only, because answering `False` for something
    that happens to be identity merely keeps a walk nobody needed.
    """
    projected = _response_input(annotation, value)
    assert projected == value
    assert type(projected) is type(value)


def test_the_identity_claim_is_refused_where_the_projection_does_work() -> None:
    """The two shapes that make it false, stated so a loosened rule fails here."""
    # A dataclass filters undeclared keys out of a mapping.
    assert not _projection_is_identity(Point)
    assert not _projection_is_identity(dict[str, Point])
    assert not _projection_is_identity(list[Point])
    # A sequence origin turns a tuple or a set into a list.
    assert not _projection_is_identity(list[int])
    assert not _projection_is_identity(tuple[int, ...])
    assert not _projection_is_identity(set[str])
    assert not _projection_is_identity(dict[str, list[int]])
    # What it is for.
    assert _projection_is_identity(dict[str, Any])
    assert _projection_is_identity(dict[str, str])
    assert _projection_is_identity(str)
    assert _projection_is_identity(str | None)
    assert _projection_is_identity(Annotated[int, Field(ge=0)])


def test_an_enum_backed_by_a_primitive_still_reduces_to_its_value() -> None:
    """The failure the exact-type fast path exists to avoid, stated directly.

    `isinstance(Colour.RED, str)` is true, so an `isinstance` fast path would
    return the member itself and put a non-JSON object into a response body
    that every other test in this file would still call equal.
    """
    assert _compile_jsonable(str)(Colour.RED) == "red"
    assert type(_compile_jsonable(str)(Colour.RED)) is str
    assert _compile_jsonable(int)(Level.HIGH) == 9
    assert type(_compile_jsonable(int)(Level.HIGH)) is int


def test_a_scalar_mapping_entry_bypasses_its_compiled_converter() -> None:
    """The exact-type branch is a cost contract, not an output distinction.

    Every compiled converter deliberately returns JSON scalars unchanged, so
    comparing only the result cannot distinguish the fast path from an extra
    Python call per entry.  Instrument the converter captured by the compiled
    mapping and assert that the overwhelmingly common scalar case never enters
    it while a value that needs conversion still does.
    """
    convert = _compile_jsonable(dict[str, Decimal])
    sequence, mapping = convert.__defaults__
    calls: list[object] = []

    def watched(value: object) -> object:
        calls.append(value)
        return mapping(value)

    convert.__defaults__ = (sequence, watched)

    assert convert({"count": 7, "cost": Decimal("1.25")}) == {
        "count": 7,
        "cost": "1.25",
    }
    assert calls == [Decimal("1.25")]


def test_a_self_referential_dataclass_compiles_and_still_converts() -> None:
    """The compiled walk cannot be finite here, so that node is interpreted."""
    deep = Node(1, Node(2, Node(3, Node(4))))
    assert _compile_jsonable(Node)(deep) == _jsonable(Node, deep)


def test_a_self_referential_response_mapping_uses_the_finite_fallback() -> None:
    payload = {"child": None}

    assert _compile_response_input(Recursive)(payload) == _response_input(
        Recursive, payload
    )


# -- the exact-type fast path must not change what any subclass does ----------


class _DictSubclass(dict):
    """A Mapping that is not exactly a `dict`, so it must take the old path."""


class _ListSubclass(list):
    """Likewise for the sequence arm."""


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"id": 42, "ok": True},
        {"n": None, "f": 1.5, "s": "x"},
        {"nested": {"deep": [1, {"x": 2}]}},
        [],
        [1, 2, 3],
        [{"a": 1}, [2], "3"],
        {"u": uuid.UUID(int=1)},
        {"d": Decimal("1.5")},
        {"when": datetime.datetime(2024, 1, 1, 12, 30)},
        {"raw": b"bytes"},
        _DictSubclass(a=1, b={"c": 2}),
        _ListSubclass([1, {"a": 2}]),
        {"mixed": _DictSubclass(x=1)},
        (1, 2, 3),
        {"1", "2"},
        frozenset({"a"}),
    ],
)
def test_the_fast_path_agrees_with_the_full_ladder(value: object) -> None:
    """Every shape must serialise to what the unreordered ladder produced.

    The oracle is the ladder itself, run with the fast path disabled, so this
    compares the two orderings rather than trusting a hand-written expectation.
    """
    from wreath import binding

    fast = binding._jsonable_any(value)
    slow = _ladder_without_fast_path(value)

    assert fast == slow
    assert type(fast) is type(slow)


def _ladder_without_fast_path(value: object) -> object:
    """`_jsonable_any` as it stood before the exact-type checks were added."""
    from collections.abc import Mapping

    from wreath import binding

    if type(value) in binding._JSON_SCALARS:
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return binding._b64encode_str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_ladder_without_fast_path(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _ladder_without_fast_path(item) for key, item in value.items()}
    return value


def test_a_subclass_never_takes_the_fast_path() -> None:
    """Why the guard is `type(value) is dict` and not `isinstance`.

    A subclass may match an *earlier* rung of the ladder than its container
    shape suggests -- a `str`-backed enum is a `str`, a dict subclass could be
    anything -- and the ladder's order encodes which rung wins. Keying the fast
    path on the exact type means only a plain `dict` or `list` can take it, so
    no subclass's resolution can move. These assert that directly.
    """
    from wreath import binding

    assert binding._jsonable_any(Colour.RED) == Colour.RED.value
    assert binding._jsonable_any(_DictSubclass(a=1)) == {"a": 1}
    assert binding._jsonable_any(_ListSubclass([1])) == [1]
    # And a plain dict/list is unchanged in value by taking it.
    assert binding._jsonable_any({"a": 1}) == {"a": 1}
    assert binding._jsonable_any([1]) == [1]
