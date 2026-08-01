"""Protocol Buffers, declared in Python and compiled once at startup.

A message is an ordinary dataclass whose fields carry their wire numbers:

```python
from wreath.protobuf import decode, encode, field, message

@message
class Position:
    collar_id: int = field(1)
    lat: float = field(2)
    lon: float = field(3)

raw = encode(Position(collar_id=7, lat=-33.8, lon=151.2))
back = decode(Position, raw)
```

**Field numbers are the wire contract, so they are stated rather than derived
from declaration order.** Reordering the class must not silently change the
bytes, which is exactly what a positional scheme would do.

The plan is compiled at class creation and never re-derived, in the same shape
as compiled route matching and compiled validators: a declaration error is a
startup error, and the request path walks a flat tuple.

## What this is, and is not

This is a **codec**, not an implementation of protobuf. It speaks the wire
format for declarations written here. It deliberately has no descriptors, no
reflection, no `Any`, no dynamic messages, and it does not parse `.proto` files
at runtime — declarations are Python, the way schemas, routes and validators
already are. Groups (deprecated since proto2) are refused by name rather than
mis-parsed. The canonical protobuf-JSON mapping is a separate specification and
is not implemented.

## Unknown fields are preserved, not rejected

This is the one place the codec deliberately parts from wreath's usual
strictness, and the reason is that the two situations are not the same one.

An unexpected *name* in a JSON body is a typo or a mistake, and rejecting it
catches a real class of bug — so `wreath.binding` rejects extra fields. An
unexpected *number* on a protobuf wire is a version-skew signal: field numbers
are a deliberate, allocated contract, and tolerating unknown ones is the
mechanism that lets a newer producer talk to an older consumer at all. A codec
that rejected them would break the property protobuf exists to provide.

So unknown fields are captured verbatim and re-emitted on encode, which means a
message survives a round trip through an intermediary built against an older
declaration. `unknown_fields(msg)` returns those bytes if you need to see them.

## Native and pure

`src/wreath/_pure/protobuf.py` is the reference implementation and the parity
contract; `src/wreath/_native/protobuf.c` is a faster twin held byte-for-byte
equal by `tests/test_protobuf_parity.py`. `WREATH_PURE=1` selects the reference.
"""

from __future__ import annotations

import annotationlib
import dataclasses
import enum
import typing
from typing import Any

from ._native import _core
from ._pure import protobuf as _ref
from ._pure.protobuf import ProtobufDecodeError

__all__ = [
    "ProtobufDecodeError",
    "ProtobufDeclarationError",
    "decode",
    "encode",
    "field",
    "is_message",
    "message",
    "unknown_fields",
]

class ProtobufDeclarationError(TypeError):
    """A message declaration cannot be compiled to a wire plan.

    Raised at class creation, never at encode time: a declaration is a contract
    with every peer, and the moment to find out it is wrong is import.
    """


# The pure codec stays the reference implementation and the parity contract, so
# WREATH_PURE=1 selects it exactly as it does for JSON and msgpack. The C side
# is handed the decode exception once, the way json.c is handed the temporal
# types, so a refusal raised in C is the same class a caller catches from the
# pure path.
if _core is not None and hasattr(_core, "protobuf_encode"):
    _core.protobuf_configure(ProtobufDecodeError)
    _encode_values = _core.protobuf_encode
    _decode_values = _core.protobuf_decode
else:
    _encode_values = _ref.encode_values
    _decode_values = _ref.decode_values

#: Field numbers 19000-19999 are reserved by the specification for the protobuf
#: implementation itself, and 2^29-1 is the largest expressible number.
_RESERVED = range(19000, 20000)
_MAX_FIELD_NUMBER = 536870911

_PLAN = "__wreath_protobuf_plan__"
_UNKNOWN = "__wreath_protobuf_unknown__"

_KINDS: dict[str, int] = {
    "int32": _ref.KIND_INT32,
    "int64": _ref.KIND_INT64,
    "uint32": _ref.KIND_UINT32,
    "uint64": _ref.KIND_UINT64,
    "sint32": _ref.KIND_SINT32,
    "sint64": _ref.KIND_SINT64,
    "bool": _ref.KIND_BOOL,
    "fixed64": _ref.KIND_FIXED64,
    "sfixed64": _ref.KIND_SFIXED64,
    "double": _ref.KIND_DOUBLE,
    "fixed32": _ref.KIND_FIXED32,
    "sfixed32": _ref.KIND_SFIXED32,
    "float": _ref.KIND_FLOAT,
    "string": _ref.KIND_STRING,
    "bytes": _ref.KIND_BYTES,
}

#: Which explicit kinds each Python annotation may be narrowed to. Declaring
#: `str = field(1, kind="sint32")` is a mistake worth catching at import.
_COMPATIBLE: dict[type, frozenset[int]] = {
    int: frozenset(
        {
            _ref.KIND_INT32,
            _ref.KIND_INT64,
            _ref.KIND_UINT32,
            _ref.KIND_UINT64,
            _ref.KIND_SINT32,
            _ref.KIND_SINT64,
            _ref.KIND_FIXED32,
            _ref.KIND_SFIXED32,
            _ref.KIND_FIXED64,
            _ref.KIND_SFIXED64,
        }
    ),
    float: frozenset({_ref.KIND_DOUBLE, _ref.KIND_FLOAT}),
    bool: frozenset({_ref.KIND_BOOL}),
    str: frozenset({_ref.KIND_STRING}),
    bytes: frozenset({_ref.KIND_BYTES}),
}

_DEFAULT_KIND: dict[type, int] = {
    int: _ref.KIND_INT64,
    float: _ref.KIND_DOUBLE,
    bool: _ref.KIND_BOOL,
    str: _ref.KIND_STRING,
    bytes: _ref.KIND_BYTES,
}


@dataclasses.dataclass(frozen=True, slots=True)
class FieldSpec:
    """What `field()` records. Replaced by a real default when the class is built."""

    number: int
    kind: str | None = None
    packed: bool | None = None
    oneof: str | None = None


def field(
    number: int,
    *,
    kind: str | None = None,
    packed: bool | None = None,
    oneof: str | None = None,
) -> Any:
    """Declare a message field with its wire number.

    Args:
        number: The field's wire number, 1..536870911 and outside 19000..19999.
            This is the contract with every peer; changing it is a breaking change.
        kind: Narrow the wire type: `int` defaults to `int64`, so pass
            `"sint32"` for a small signed value or `"fixed64"` for a hash. Must
            be compatible with the annotation.
        packed: Force the packed representation on or off for a repeated scalar.
            Defaults to packed, which is proto3's default. A decoder accepts
            both regardless.
        oneof: Group name. At most one field in a group may be set; setting one
            on decode clears the others. Every member must be optional.

    Returns:
        A marker consumed by `@message`; never a value you read yourself.
    """
    return FieldSpec(number=number, kind=kind, packed=packed, oneof=oneof)


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Split `X | None` into `(X, True)`; anything else is `(annotation, False)`.

    One `is typing.Union` covers both spellings: Python 3.14 unified the two, so
    `typing.Union is types.UnionType` and `get_origin` answers `typing.Union` for
    `int | None` and `Optional[int]` alike. Testing both would be the same test
    written twice.

    The union check is load-bearing rather than decorative. Any two-argument
    generic carrying an explicit `NoneType` -- `dict[str, NoneType]`,
    `tuple[int, NoneType]` -- has the same argument shape as `X | None`, so
    without the origin test it would unwrap to an optional `X` and silently
    become a scalar field of a different wire type instead of being refused.

    Note the asymmetry that makes this easy to get wrong: `dict[str, None]`
    keeps a literal `None` in `get_args`, while `int | None` normalises its to
    `NoneType`. Only the spelled-out `NoneType` reaches the filter below.

    One non-`None` argument is the whole test, with no arity check beside it. A
    union deduplicates, so `Union[int, None, None]`, `int | None | None` and
    `Optional[Optional[int]]` all normalise to exactly two arguments -- given a
    union origin, "one argument survives the filter" already implies "the other
    one was NoneType". A `len(get_args()) == 2` clause here read as defensive
    but could never be false when evaluated, which a mutation pass surfaced by
    removing it without any test objecting.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _resolve(annotation: Any, name: str, spec: FieldSpec) -> tuple[int, int, Any, Any]:
    """Map one annotation to `(kind, flags, subplan, holder)`.

    `subplan` is wire-level and crosses into C: a nested plan tuple or None.
    `holder` never crosses: it is the Python class needed to rebuild the value
    (a message class, an enum class, or a map's value-message class). Keeping
    them apart is what lets the plan stay a tuple of ints all the way down.
    """
    inner, optional = _unwrap_optional(annotation)
    flags = _ref.FLAG_OPTIONAL if optional else 0

    origin = typing.get_origin(inner)
    if origin is list:
        (item,) = typing.get_args(inner)
        kind, _sub_flags, subplan, holder = _resolve(item, name, spec)
        if kind == _ref.KIND_MESSAGE:
            return kind, flags | _ref.FLAG_REPEATED, subplan, holder
        packed = True if spec.packed is None else spec.packed
        if packed and kind not in (_ref.KIND_STRING, _ref.KIND_BYTES):
            flags |= _ref.FLAG_PACKED
        return kind, flags | _ref.FLAG_REPEATED, None, holder
    if origin is dict:
        key_type, value_type = typing.get_args(inner)
        key_kind, _kf, _ks, _kh = _resolve(key_type, name, spec)
        value_kind, _vf, value_sub, value_holder = _resolve(value_type, name, spec)
        if key_kind in (_ref.KIND_DOUBLE, _ref.KIND_FLOAT, _ref.KIND_MESSAGE):
            raise ProtobufDeclarationError(
                f"field {name!r}: a map key may not be a float or a message"
            )
        subplan = (
            (1, key_kind, 0, None),
            (2, value_kind, 0, value_sub),
        )
        return _ref.KIND_MESSAGE, flags | _ref.FLAG_MAP, subplan, value_holder

    if isinstance(inner, type) and issubclass(inner, enum.IntEnum):
        if 0 not in {member.value for member in inner}:
            raise ProtobufDeclarationError(
                f"field {name!r}: enum {inner.__name__} has no zero member, and "
                "proto3 requires one because zero is what an absent field decodes to"
            )
        return _ref.KIND_ENUM, flags, None, inner
    if isinstance(inner, type) and hasattr(inner, _PLAN):
        # A nested message always has explicit presence: there is no zero value
        # that could stand in for absent.
        return (
            _ref.KIND_MESSAGE,
            flags | _ref.FLAG_OPTIONAL,
            getattr(inner, _PLAN)[0],
            inner,
        )

    if not isinstance(inner, type):
        # Everything below looks `inner` up in a dict, and an annotation that is
        # not a type may be unhashable -- a message *instance* is, because
        # dataclass equality drops __hash__. Guarding here keeps that from
        # surfacing as `TypeError: unhashable type` from inside a lookup, which
        # names neither the field nor the mistake.
        raise ProtobufDeclarationError(
            f"field {name!r}: {inner!r} is not a type, so it has no protobuf "
            "wire mapping"
        )

    if spec.kind is not None:
        code = _KINDS.get(spec.kind)
        if code is None:
            raise ProtobufDeclarationError(
                f"field {name!r}: unknown kind {spec.kind!r}; "
                f"expected one of {', '.join(sorted(_KINDS))}"
            )
        allowed = _COMPATIBLE.get(inner)
        if allowed is None or code not in allowed:
            raise ProtobufDeclarationError(
                f"field {name!r}: kind {spec.kind!r} is not compatible with "
                f"annotation {getattr(inner, '__name__', inner)!r}"
            )
        return code, flags, None, None

    default_kind = _DEFAULT_KIND.get(inner)
    if default_kind is None:
        raise ProtobufDeclarationError(
            f"field {name!r}: {getattr(inner, '__name__', inner)!r} has no "
            "protobuf wire mapping"
        )
    return default_kind, flags, None, None


def _zero(kind: int, flags: int, subplan: Any) -> Any:
    if flags & _ref.FLAG_MAP:
        return dataclasses.field(default_factory=dict)
    if flags & _ref.FLAG_REPEATED:
        return dataclasses.field(default_factory=list)
    if flags & _ref.FLAG_OPTIONAL:
        return None
    if kind == _ref.KIND_ENUM:
        return subplan(0)
    if kind == _ref.KIND_STRING:
        return ""
    if kind == _ref.KIND_BYTES:
        return b""
    if kind == _ref.KIND_BOOL:
        return False
    if kind in (_ref.KIND_DOUBLE, _ref.KIND_FLOAT):
        return 0.0
    return 0


@typing.dataclass_transform(field_specifiers=(field,))
def message[T](cls: type[T]) -> type[T]:
    """Compile `cls` into a protobuf message and make it a dataclass.

    Raises:
        ProtobufDeclarationError: A field number is duplicated, reserved, out of
            range, or annotated with a type that has no wire mapping — all at
            class creation, never at encode time.
    """
    # `annotationlib`, not `typing.get_type_hints`. Under PEP 649 -- a module
    # without `from __future__ import annotations` -- this evaluates the class's
    # annotations through the closure they were written in, so a message or an
    # enum declared inside a function resolves; `get_type_hints` rebuilds the
    # namespace from module globals and raises NameError for exactly those.
    #
    # `eval_str` covers the other half. A module that *does* use
    # `from __future__ import annotations` (PEP 563) stringifies first, and
    # those strings evaluate against module globals only -- so a locally
    # declared type is out of reach there, which is a property of PEP 563 and
    # not something this decorator can repair. Declare nested messages and enums
    # at module level and the distinction never arises.
    annotations = annotationlib.get_annotations(
        cls, format=annotationlib.Format.VALUE, eval_str=True
    )
    rows: list[tuple] = []
    holders: list[Any] = []
    seen: dict[int, str] = {}
    oneofs: dict[str, list[int]] = {}

    for name, annotation in annotations.items():
        spec = getattr(cls, name, None)
        if not isinstance(spec, FieldSpec):
            raise ProtobufDeclarationError(
                f"field {name!r} has no field() declaration; every field of a "
                "protobuf message needs an explicit wire number"
            )
        number = spec.number
        if not isinstance(number, int) or isinstance(number, bool):
            raise ProtobufDeclarationError(f"field {name!r}: number must be an int")
        if number < 1 or number > _MAX_FIELD_NUMBER:
            raise ProtobufDeclarationError(
                f"field {name!r}: number {number} is outside 1..{_MAX_FIELD_NUMBER}"
            )
        if number in _RESERVED:
            raise ProtobufDeclarationError(
                f"field {name!r}: number {number} is inside the reserved range "
                "19000..19999"
            )
        if number in seen:
            raise ProtobufDeclarationError(
                f"field {name!r}: number {number} is already used by "
                f"{seen[number]!r}"
            )
        seen[number] = name

        kind, flags, subplan, holder = _resolve(annotation, name, spec)
        if spec.oneof is not None:
            if not flags & _ref.FLAG_OPTIONAL:
                raise ProtobufDeclarationError(
                    f"field {name!r}: a oneof member must be optional "
                    f"(annotate it as `{annotation} | None`), because at most "
                    "one member of a group is set"
                )
            oneofs.setdefault(spec.oneof, []).append(len(rows))

        holders.append(holder)
        rows.append((number, kind, flags, subplan))
        setattr(cls, name, _zero(kind, flags, holder))

    plan = tuple(rows)
    setattr(cls, _PLAN, (plan, tuple(annotations), tuple(holders), oneofs))
    built = dataclasses.dataclass(cls)
    return built


def _to_values(msg: Any) -> list:
    plan, names, holders, _oneofs = getattr(type(msg), _PLAN)
    values: list = []
    for index, row in enumerate(plan):
        _number, kind, flags, _subplan = row
        value = getattr(msg, names[index])
        if flags & _ref.FLAG_MAP:
            holder_plan = row[3]
            if holder_plan[1][1] == _ref.KIND_MESSAGE:
                value = {k: _pair(v) for k, v in value.items()}
            values.append(value)
        elif kind == _ref.KIND_MESSAGE:
            if flags & _ref.FLAG_REPEATED:
                values.append([_pair(item) for item in value])
            else:
                values.append(None if value is None else _pair(value))
        elif kind == _ref.KIND_ENUM:
            if flags & _ref.FLAG_REPEATED:
                values.append([int(item) for item in value])
            else:
                values.append(None if value is None else int(value))
        else:
            values.append(value)
    return values


def _pair(msg: Any) -> tuple[list, bytes]:
    return _to_values(msg), getattr(msg, _UNKNOWN, b"")


def encode(msg: Any) -> bytes:
    """Encode `msg` to protobuf bytes.

    Unknown fields captured when this message was decoded are re-emitted, so a
    round trip through a build with an older declaration loses nothing.

    Raises:
        ValueError: A value is outside the range its declared kind can hold.
    """
    plan, _names, _holders, _oneofs = getattr(type(msg), _PLAN)
    return _encode_values(plan, _to_values(msg), getattr(msg, _UNKNOWN, b""))


def decode(cls: type, data: bytes) -> Any:
    """Decode `data` as `cls`.

    Raises:
        ProtobufDecodeError: The buffer is truncated, carries a length prefix
            past its end, a varint longer than ten bytes, a group wire type, an
            unknown wire type, field number zero, or invalid UTF-8 in a string.
    """
    plan, _names, _holders, _oneofs = getattr(cls, _PLAN)
    values, unknown = _decode_values(plan, bytes(data))
    return _build(cls, values, unknown)


def _build(cls: type, values: list, unknown: bytes) -> Any:
    plan, names, holders, oneofs = getattr(cls, _PLAN)
    kwargs: dict[str, Any] = {}
    for index, row in enumerate(plan):
        _number, kind, flags, _subplan = row
        value = values[index]
        if flags & _ref.FLAG_MAP:
            holder_plan = row[3]
            if holder_plan[1][1] == _ref.KIND_MESSAGE:
                value = {k: _build(holders[index], v[0], v[1]) for k, v in value.items()}
        elif kind == _ref.KIND_MESSAGE:
            if flags & _ref.FLAG_REPEATED:
                value = [_build(holders[index], v[0], v[1]) for v in value]
            elif value is not None:
                value = _build(holders[index], value[0], value[1])
        elif kind == _ref.KIND_ENUM and value is not None:
            holder = holders[index]
            if flags & _ref.FLAG_REPEATED:
                value = [holder(v) for v in value]
            else:
                # An enum value this build does not know is kept as the int the
                # peer sent, since proto3 open enums say a decoder must not lose it.
                value = holder(value) if value in set(holder) else value
        kwargs[names[index]] = value

    for members in oneofs.values():
        present = [i for i in members if kwargs[names[i]] is not None]
        # Last one on the wire wins, which is what the specification requires.
        for i in present[:-1]:
            kwargs[names[i]] = None

    built = cls(**kwargs)
    object.__setattr__(built, _UNKNOWN, unknown)
    return built


def is_message(candidate: Any) -> bool:
    """Whether `candidate` is a `@message` class (or an instance of one).

    Public because consumers need to ask. Without it, a caller that wants to
    branch on "is this a protobuf message?" reads the private plan marker
    directly and thereby encodes its own second notion of what a message is --
    which then drifts from this one the first time the marker moves.
    """
    return hasattr(candidate, _PLAN)


def unknown_fields(msg: Any) -> bytes:
    """The raw bytes of fields this build did not recognise when decoding `msg`."""
    return getattr(msg, _UNKNOWN, b"")
