"""Explicit PostgreSQL column types.

Every type names one PostgreSQL OID that both driver backends encode and
decode. There is no inference and no implicit widening: a column declares the
type it has in the database, and values that do not fit it are rejected before
any SQL runs.

``coerce`` validates and normalizes a Python value on assignment. ``to_wire``
and ``from_wire`` convert between that Python value and the representation the
driver codec exchanges, and are the identity for everything except JSON.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from .._json import dumps as _json_dumps
from .._json import loads as _json_loads


class PgType:
    """One PostgreSQL type, its OID, and its Python conversion rules."""

    __slots__ = ("_coerce", "_from_wire", "_to_wire", "name", "oid", "shape_value", "sql")

    def __init__(
        self,
        name: str,
        oid: int,
        sql: str,
        coerce: Callable[[Any], Any],
        *,
        to_wire: Callable[[Any], Any] | None = None,
        from_wire: Callable[[Any], Any] | None = None,
    ) -> None:
        self.name = name
        self.oid = oid
        self.sql = sql
        #: This type's contribution to a plan-cache key. Only the type shapes
        #: the SQL, never the value, so this is constant -- and `shape_of` was
        #: rebuilding it per request.
        self.shape_value = b"v" + str(oid).encode("ascii")
        self._coerce = coerce
        self._to_wire = to_wire
        self._from_wire = from_wire

    def coerce(self, value: Any) -> Any:
        """Validate ``value`` for this type, returning the normalized value."""
        return self._coerce(value)

    def to_wire(self, value: Any) -> Any:
        if value is None or self._to_wire is None:
            return value
        return self._to_wire(value)

    def from_wire(self, value: Any) -> Any:
        if value is None or self._from_wire is None:
            return value
        return self._from_wire(value)

    def __repr__(self) -> str:
        return f"<PgType {self.name} oid={self.oid}>"


class _ArrayType(PgType):
    """A one-dimensional PostgreSQL array type that remembers its element type.

    An array reuses its element's scalar codec value-by-value, so it only needs
    to carry the element ``PgType`` around: coercion validates each element with
    it, and ``any_eq``/``all_eq`` bind a scalar against it.
    """

    __slots__ = ("element",)

    def __init__(
        self,
        element: PgType,
        name: str,
        oid: int,
        sql: str,
        coerce: Callable[[Any], Any],
        *,
        to_wire: Callable[[Any], Any] | None = None,
        from_wire: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__(name, oid, sql, coerce, to_wire=to_wire, from_wire=from_wire)
        self.element = element


def _type_error(expected: str, value: object) -> TypeError:
    return TypeError(f"expected {expected}, got {type(value).__name__}")


def _check_bool(value: Any) -> bool:
    if value.__class__ is not bool:
        raise _type_error("bool", value)
    return value


def _integer(bits: int) -> Callable[[Any], int]:
    low, high = -(2 ** (bits - 1)), 2 ** (bits - 1)

    def check(value: Any) -> int:
        # bool is an int subclass; storing True in a bigint column is a
        # declaration mistake worth surfacing rather than coercing to 1.
        if value.__class__ is not int:
            raise _type_error(f"int{bits // 8}", value)
        if not low <= value < high:
            raise OverflowError(f"value out of range for int{bits // 8}: {value}")
        return value

    return check


def _real(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _type_error("float", value)
    return float(value)


def _check_str(value: Any) -> str:
    if not isinstance(value, str):
        raise _type_error("str", value)
    return value


def _check_bytes(value: Any) -> bytes:
    if not isinstance(value, bytes):
        raise _type_error("bytes", value)
    return value


def _check_uuid(value: Any) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise _type_error("UUID", value)
    return value


def _check_date(value: Any) -> datetime.date:
    # datetime subclasses date, so a datetime in a date column is rejected
    # rather than silently truncated.
    if value.__class__ is not datetime.date:
        raise _type_error("date", value)
    return value


def _check_naive_datetime(value: Any) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise _type_error("datetime", value)
    if value.tzinfo is not None:
        raise TypeError("timestamp requires a naive datetime; use TimestampTz")
    return value


def _check_aware_datetime(value: Any) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise _type_error("datetime", value)
    if value.tzinfo is None:
        raise TypeError("timestamptz requires an aware datetime; use Timestamp")
    return value


def _check_numeric(value: Any) -> Decimal:
    """Accept the exact numeric types only.

    ``float`` is refused rather than converted. A column is declared ``numeric``
    precisely because binary floating point cannot hold its values -- accepting
    a float would put the collapse back in, one layer up from the codec that
    just removed it. ``Decimal(str(x))`` at the call site is the explicit way to
    say what rounding you wanted.
    """
    if isinstance(value, Decimal):
        return value
    if value.__class__ is int:
        return Decimal(value)
    raise TypeError(
        f"expected Decimal or int, got {type(value).__name__}; "
        "a float cannot hold a numeric exactly -- use Decimal(str(value))"
    )


def _check_json(value: Any) -> Any:
    # Serializing here rejects unencodable values on assignment instead of at
    # flush time, when the failure is far from the offending line.
    _json_dumps(value)
    return value


def _json_to_wire(value: Any) -> str:
    return _json_dumps(value).decode("utf-8")


Bool = PgType("bool", 16, "boolean", _check_bool)
Int16 = PgType("int2", 21, "smallint", _integer(16))
Int32 = PgType("int4", 23, "integer", _integer(32))
Int64 = PgType("int8", 20, "bigint", _integer(64))
Float32 = PgType("float4", 700, "real", _real)
Float64 = PgType("float8", 701, "double precision", _real)
Text = PgType("text", 25, "text", _check_str)
Varchar = PgType("varchar", 1043, "character varying", _check_str)
Bytea = PgType("bytea", 17, "bytea", _check_bytes)
Uuid = PgType("uuid", 2950, "uuid", _check_uuid)
Date = PgType("date", 1082, "date", _check_date)
Timestamp = PgType("timestamp", 1114, "timestamp without time zone", _check_naive_datetime)
TimestampTz = PgType("timestamptz", 1184, "timestamp with time zone", _check_aware_datetime)
Numeric = PgType("numeric", 1700, "numeric", _check_numeric)
Json = PgType(
    "json", 114, "json", _check_json, to_wire=_json_to_wire, from_wire=_json_loads
)
Jsonb = PgType(
    "jsonb", 3802, "jsonb", _check_json, to_wire=_json_to_wire, from_wire=_json_loads
)


#: PostgreSQL array OID for each element OID Wreath can frame. Element types
#: absent here have no scalar codec, so they have no array form either.
_ARRAY_OID: dict[int, int] = {
    16: 1000,    # boolean[]
    17: 1001,    # bytea[]
    20: 1016,    # bigint[]
    21: 1005,    # smallint[]
    23: 1007,    # integer[]
    25: 1009,    # text[]
    114: 199,    # json[]
    700: 1021,   # real[]
    701: 1022,   # double precision[]
    1043: 1015,  # varchar[]
    1082: 1182,  # date[]
    1114: 1115,  # timestamp[]
    1184: 1185,  # timestamptz[]
    1700: 1231,  # numeric[]
    2950: 2951,  # uuid[]
    3802: 3807,  # jsonb[]
}


def Array(element: PgType, *, nullable_elements: bool = False) -> _ArrayType:
    """Declare a one-dimensional PostgreSQL array of ``element``.

    Each element is validated and wired through ``element``'s own rules, so
    ``Array(Uuid)`` accepts a ``list``/``tuple`` of UUIDs and rejects anything
    else. Elements are non-nullable unless ``nullable_elements=True``. Nested
    arrays are not supported: an element type may not itself be an array.
    """
    if not isinstance(element, PgType):
        raise TypeError(f"Array() requires a PgType element, got {element!r}")
    if isinstance(element, _ArrayType):
        raise TypeError("nested arrays are not supported")
    array_oid = _ARRAY_OID.get(element.oid)
    if array_oid is None:
        raise TypeError(f"{element.name} has no array type")

    def coerce(value: Any) -> list[Any]:
        if not isinstance(value, (list, tuple)):
            raise _type_error("list or tuple", value)
        out: list[Any] = []
        for item in value:
            if item is None:
                if not nullable_elements:
                    raise TypeError(
                        f"{element.name}[] elements are not nullable; pass "
                        "nullable_elements=True to allow NULL entries"
                    )
                out.append(None)
            else:
                out.append(element.coerce(item))
        return out

    def to_wire(value: list[Any]) -> list[Any]:
        return [None if item is None else element.to_wire(item) for item in value]

    def from_wire(value: list[Any]) -> list[Any]:
        return [None if item is None else element.from_wire(item) for item in value]

    return _ArrayType(
        element,
        f"{element.name}[]",
        array_oid,
        f"{element.sql}[]",
        coerce,
        to_wire=to_wire,
        from_wire=from_wire,
    )


# PostgreSQL-spelled aliases for the integer and float widths.
Int2 = Int16
Int4 = Int32
Int8 = Int64
Float4 = Float32
Float8 = Float64

#: Every declarable type, keyed by OID, for result validation and introspection.
BY_OID: dict[int, PgType] = {
    item.oid: item
    for item in (
        Bool, Int16, Int32, Int64, Float32, Float64, Text, Varchar,
        Bytea, Uuid, Date, Timestamp, TimestampTz, Numeric, Json, Jsonb,
    )
}

# Canonical array types, one per supported element, registered so result
# validation and introspection can decode an array column by its OID. ``Array``
# always returns the same OID for a given element, so any of these decodes any
# column of that array type.
for _element in (
    Bool, Int16, Int32, Int64, Float32, Float64, Text, Varchar,
    Bytea, Uuid, Date, Timestamp, TimestampTz, Numeric, Json, Jsonb,
):
    _canonical_array = Array(_element)
    BY_OID[_canonical_array.oid] = _canonical_array

#: The ``text[]`` type the jsonb key operators (``?|``, ``?&``, ``#>>`` paths)
#: bind their operands as.
TextArray = Array(Text)

__all__ = [
    "BY_OID",
    "Array",
    "Bool",
    "Bytea",
    "Date",
    "Float4",
    "Float8",
    "Float32",
    "Float64",
    "Int2",
    "Int4",
    "Int8",
    "Int16",
    "Int32",
    "Int64",
    "Json",
    "Jsonb",
    "Numeric",
    "PgType",
    "Text",
    "TextArray",
    "Timestamp",
    "TimestampTz",
    "Uuid",
    "Varchar",
]
