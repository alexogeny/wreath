"""What `@message` compiles a declaration *into*.

The wire tests assert bytes, which exercises the compiled plan only indirectly —
and every message they use is declared at module level, so a mutation test
cannot attribute those declarations to any test at all. A mutation pass over
`_resolve` and `_unwrap_optional` reported exactly that: the branches that
decide repeated-ness, packing, presence and nesting were either unreached or
survived, because nothing here looked at the plan.

So this file declares messages *inside* the tests and asserts the plan directly.
The plan is the contract with every peer; asserting the bytes for one value does
not pin the flags that decide the bytes for every other one.
"""

from __future__ import annotations

import enum

import pytest

from wreath import _protobuf_plan as wire
from wreath.protobuf import ProtobufDeclarationError, field, message


class Colour(enum.IntEnum):
    NONE = 0
    RED = 1


@message
class Leaf:
    n: int = field(1)


def _plan(cls: type) -> tuple:
    return cls.__wreath_protobuf_plan__[0]


def _holders(cls: type) -> tuple:
    return cls.__wreath_protobuf_plan__[2]


def _row(cls: type, index: int = 0) -> tuple:
    return _plan(cls)[index]


# -- presence ---------------------------------------------------------------


def test_a_plain_scalar_has_implicit_presence() -> None:
    @message
    class M:
        a: int = field(1)

    _number, kind, flags, _sub = _row(M)
    assert kind == wire.KIND_INT64
    assert not flags & wire.FLAG_OPTIONAL


def test_an_optional_scalar_has_explicit_presence() -> None:
    @message
    class M:
        a: int | None = field(1)

    _number, kind, flags, _sub = _row(M)
    assert kind == wire.KIND_INT64
    assert flags & wire.FLAG_OPTIONAL


def test_a_union_that_is_not_optional_is_not_unwrapped() -> None:
    # `int | str` is two real alternatives, not explicit presence, and has no
    # wire mapping — so it must be refused rather than silently read as `int`.
    with pytest.raises(ProtobufDeclarationError):

        @message
        class M:
            a: int | str = field(1)


def test_a_three_way_union_including_none_is_refused() -> None:
    with pytest.raises(ProtobufDeclarationError):

        @message
        class M:
            a: int | str | None = field(1)


def test_a_map_with_a_none_value_type_is_refused() -> None:
    namespace = {"__annotations__": {"a": dict[str, None]}, "a": field(1)}
    with pytest.raises(ProtobufDeclarationError):
        message(type("M", (), namespace))


def test_a_two_arg_generic_carrying_nonetype_is_not_mistaken_for_optional() -> None:
    # `dict[str, NoneType]` has exactly the argument shape of `X | None`: two
    # arguments, one of them NoneType. Only the origin test separates them, and
    # without it this unwraps to an optional `str` — a malformed map silently
    # becoming a scalar field of an entirely different wire type.
    #
    # `dict[str, None]` above does *not* reach that path: a subscripted generic
    # keeps the literal `None`, while a union normalises to `NoneType`. The two
    # spellings take different routes to the same refusal, so both are pinned.
    namespace = {"__annotations__": {"a": dict[str, type(None)]}, "a": field(1)}
    with pytest.raises(ProtobufDeclarationError):
        message(type("M", (), namespace))


def test_an_instance_used_as_an_annotation_is_refused() -> None:
    # An instance carries its class's plan attribute, so a `hasattr` check alone
    # would accept it and then fail at decode time when the holder is called.
    # A declaration is a contract with every peer; import is when to find out.
    namespace = {"__annotations__": {"a": Leaf()}, "a": field(1)}
    with pytest.raises(ProtobufDeclarationError):
        message(type("M", (), namespace))


# -- default kind selection -------------------------------------------------


@pytest.mark.parametrize(
    "annotation,expected",
    [
        (int, wire.KIND_INT64),
        (float, wire.KIND_DOUBLE),
        (bool, wire.KIND_BOOL),
        (str, wire.KIND_STRING),
        (bytes, wire.KIND_BYTES),
    ],
)
def test_each_python_type_has_a_default_wire_kind(
    annotation: type, expected: int
) -> None:
    namespace = {"__annotations__": {"a": annotation}, "a": field(1)}
    M = message(type("M", (), namespace))
    assert _row(M)[1] == expected


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("int32", wire.KIND_INT32),
        ("uint32", wire.KIND_UINT32),
        ("sint64", wire.KIND_SINT64),
        ("fixed64", wire.KIND_FIXED64),
        ("sfixed32", wire.KIND_SFIXED32),
    ],
)
def test_an_explicit_kind_narrows_the_default(kind: str, expected: int) -> None:
    namespace = {"__annotations__": {"a": int}, "a": field(1, kind=kind)}
    M = message(type("M", (), namespace))
    assert _row(M)[1] == expected


# -- repeated and packing ---------------------------------------------------


def test_a_repeated_scalar_packs_by_default() -> None:
    @message
    class M:
        a: list[int] = field(1)

    _number, kind, flags, _sub = _row(M)
    assert kind == wire.KIND_INT64
    assert flags & wire.FLAG_REPEATED
    assert flags & wire.FLAG_PACKED


def test_packed_false_clears_the_flag() -> None:
    @message
    class M:
        a: list[int] = field(1, packed=False)

    flags = _row(M)[2]
    assert flags & wire.FLAG_REPEATED
    assert not flags & wire.FLAG_PACKED


def test_a_repeated_string_is_never_packed() -> None:
    @message
    class M:
        a: list[str] = field(1)

    _number, kind, flags, _sub = _row(M)
    assert kind == wire.KIND_STRING
    assert flags & wire.FLAG_REPEATED
    assert not flags & wire.FLAG_PACKED


def test_a_repeated_bytes_field_is_never_packed() -> None:
    @message
    class M:
        a: list[bytes] = field(1)

    assert not _row(M)[2] & wire.FLAG_PACKED


def test_a_repeated_message_is_repeated_and_not_packed() -> None:
    @message
    class M:
        a: list[Leaf] = field(1)

    _number, kind, flags, subplan = _row(M)
    assert kind == wire.KIND_MESSAGE
    assert flags & wire.FLAG_REPEATED
    assert not flags & wire.FLAG_PACKED
    assert subplan == _plan(Leaf)


# -- nesting ----------------------------------------------------------------


def test_a_nested_message_carries_the_inner_plan_and_explicit_presence() -> None:
    @message
    class M:
        a: Leaf | None = field(1)

    _number, kind, flags, subplan = _row(M)
    assert kind == wire.KIND_MESSAGE
    # A message always has explicit presence: there is no zero value that could
    # stand in for absent.
    assert flags & wire.FLAG_OPTIONAL
    assert subplan == _plan(Leaf)
    assert _holders(M)[0] is Leaf


def test_a_nested_message_declared_without_none_still_gets_explicit_presence() -> None:
    @message
    class M:
        a: Leaf = field(1)

    assert _row(M)[2] & wire.FLAG_OPTIONAL


def test_the_subplan_is_a_plan_and_never_the_class() -> None:
    # The plan crosses into C, where a class is not something it can walk.
    @message
    class M:
        a: Leaf | None = field(1)

    subplan = _row(M)[3]
    assert isinstance(subplan, tuple)
    assert all(isinstance(row, tuple) for row in subplan)


# -- enums ------------------------------------------------------------------


def test_an_enum_compiles_to_the_enum_kind_and_keeps_its_class() -> None:
    @message
    class M:
        a: Colour = field(1)

    _number, kind, flags, subplan = _row(M)
    assert kind == wire.KIND_ENUM
    assert subplan is None
    assert _holders(M)[0] is Colour


def test_an_enum_field_defaults_to_its_zero_member() -> None:
    @message
    class M:
        a: Colour = field(1)

    assert M().a is Colour.NONE


# -- maps -------------------------------------------------------------------


def test_a_map_compiles_to_a_key_value_entry_plan() -> None:
    @message
    class M:
        a: dict[str, int] = field(1)

    _number, kind, flags, subplan = _row(M)
    assert kind == wire.KIND_MESSAGE
    assert flags & wire.FLAG_MAP
    assert subplan == (
        (1, wire.KIND_STRING, 0, None),
        (2, wire.KIND_INT64, 0, None),
    )


def test_a_map_of_messages_carries_the_value_plan_and_holder() -> None:
    @message
    class M:
        a: dict[str, Leaf] = field(1)

    subplan = _row(M)[3]
    assert subplan[1][1] == wire.KIND_MESSAGE
    assert subplan[1][3] == _plan(Leaf)
    assert _holders(M)[0] is Leaf


@pytest.mark.parametrize("key", [float, Leaf])
def test_a_map_key_that_cannot_be_one_is_refused(key: type) -> None:
    namespace = {"__annotations__": {"a": dict[key, int]}, "a": field(1)}
    with pytest.raises(ProtobufDeclarationError):
        message(type("M", (), namespace))


def test_an_integer_map_key_is_allowed() -> None:
    @message
    class M:
        a: dict[int, str] = field(1)

    assert _row(M)[3][0][1] == wire.KIND_INT64


# -- field numbers ----------------------------------------------------------


def test_the_plan_preserves_declared_numbers_not_positions() -> None:
    # Field numbers are the wire contract; declaration order is not.
    @message
    class M:
        a: int = field(5)
        b: int = field(2)

    assert [row[0] for row in _plan(M)] == [5, 2]


@pytest.mark.parametrize("number", [1, 15, 16, 2047, 18999, 20000, 536870911])
def test_legal_field_numbers_either_side_of_every_boundary(number: int) -> None:
    namespace = {"__annotations__": {"a": int}, "a": field(number)}
    M = message(type("M", (), namespace))
    assert _row(M)[0] == number


@pytest.mark.parametrize("number", [19000, 19999])
def test_the_reserved_range_is_refused_at_both_ends(number: int) -> None:
    namespace = {"__annotations__": {"a": int}, "a": field(number)}
    with pytest.raises(ProtobufDeclarationError):
        message(type("M", (), namespace))


def test_a_bool_field_number_is_refused() -> None:
    # `True` is an int subclass and would otherwise compile to field number 1.
    namespace = {"__annotations__": {"a": int}, "a": field(True)}
    with pytest.raises(ProtobufDeclarationError):
        message(type("M", (), namespace))
