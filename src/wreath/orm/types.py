"""Explicit PostgreSQL column types.

Every type names one PostgreSQL OID that both driver backends encode and
decode. There is no inference and no implicit widening: a column declares the
type it has in the database, and values that do not fit it are rejected before
any SQL runs.

`coerce` validates and normalizes a Python value on assignment. `to_wire`
and `from_wire` convert between that Python value and the representation the
driver codec exchanges, and are the identity for everything except JSON.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import struct
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
from .._native import _core
from .._sparsevec import MAX_SPARSEVEC_DIM, MAX_SPARSEVEC_NNZ, SparseVector
from ..geospatial import Coordinate
from .errors import DeclarationError, ExtensionNotInstalledError


class PgType:
    """One PostgreSQL type, its OID, and its Python conversion rules."""

    __slots__ = (
        "_coerce",
        "_from_wire",
        "_to_wire",
        "fingerprint_oid",
        "name",
        "oid",
        "shape_value",
        "sql",
    )

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
        #: The OID a *model fingerprint* records. Identical to `oid` for a
        #: built-in type. An extension type's real OID is assigned by
        #: `CREATE EXTENSION` and differs between databases, so it substitutes a
        #: stable value derived from its SQL spelling -- a fingerprint that
        #: changed when the same models were pointed at a second database would
        #: report every model as drifted.
        self.fingerprint_oid = oid
        self._coerce = coerce
        self._to_wire = to_wire
        self._from_wire = from_wire

    def coerce(self, value: Any) -> Any:
        """Validate `value` for this type, returning the normalized value."""
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
    to carry the element `PgType` around: coercion validates each element with
    it, and `any_eq`/`all_eq` bind a scalar against it.
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


class GeneratedType(PgType):
    """A type whose column value the *database* computes, never the application.

    A column declared with one of these renders as
    `GENERATED ALWAYS AS (<expression>) STORED`, so PostgreSQL recomputes it
    inside the same statement that changed the columns it reads. That is what
    keeps a derived index correct without a trigger, and it is also why such a
    column is not writable: an INSERT or UPDATE naming one is an error, so
    `coerce` refuses the value at the assignment rather than at the flush.

    The expression itself is not held here. It names *other columns*, whose
    database names and types are only known once the registry has compiled the
    model, so it is rendered there -- see `wreath.orm._generated`.
    """

    __slots__ = ()


#: Codec kinds the driver backends know how to frame. One per extension wire
#: format, not one per type: `vector` and `halfvec` are different kinds because
#: their elements are different widths, but every `Vector(dim)` shares kind 1 and
#: every `Halfvec(dim)` shares kind 2.
EXT_KIND_VECTOR = 1
EXT_KIND_HALFVEC = 2
EXT_KIND_SPARSEVEC = 3
#: PostGIS's `geography`. A kind of its own rather than a reuse of a vector
#: kind: the wire formats share nothing, and a codec that guessed wrong would
#: return a plausible value for the wrong type rather than raising.
EXT_KIND_GEOGRAPHY = 4


class WireList(list):
    """A list-valued parameter that names its own PostgreSQL OID.

    The driver infers a parameter's OID from the Python value it is handed, and
    a bare list is deliberately refused: `[1, 2]` is equally `int4[]`, `int8[]`
    or `numeric[]`, and `[]` names no element type at all. A pgvector value is a
    list too, and it *does* know its type -- so it carries the answer.

    A `list` subclass rather than a wrapper, so both codecs keep taking the
    sequence they already take: `PySequence_Fast` and `struct.pack` are unchanged
    by this, and nothing in the encode path has to unwrap anything.
    """

    __slots__ = ("_pg_oid",)

    def __init__(self, values: Any, oid: int) -> None:
        super().__init__(values)
        self._pg_oid = oid


class ExtensionType(PgType):
    """A type an extension adds, whose OID the *database* assigns.

    Every built-in type here names a compile-time OID. An extension type cannot:
    `CREATE EXTENSION vector` allocates `vector`'s OID from the same sequence as
    everything else in that database, so it differs between one database and the
    next, and between a schema and the one beside it if the extension was
    installed twice.

    Three consequences shape this class.

    * `oid` starts at `0` and is filled in at startup, by
      `wreath.orm.introspection.resolve_extension_types`, which reads
      `pg_catalog` once and hands the answer to both driver backends. Binding a
      value before that raises rather than encoding against OID 0.
    * `shape_value` is derived from the type's *name*, never its OID. It is the
      type's contribution to the plan-cache key: an OID in there would give the
      same query two cache entries against two databases, and would change under
      a reinstall of the extension. The failure mode is a silently duplicated
      plan cache, which is invisible until someone profiles it.
    * `fingerprint_oid` is likewise name-derived, for the same reason one layer
      up: a model fingerprint that moved with the database would report drift
      that is not there.

    Nothing here is specific to pgvector. `hstore`, `citext`, and geometry would
    all resolve through the same path.
    """

    __slots__ = ("extension", "type_name")

    def __init__(
        self,
        extension: str,
        type_name: str,
        sql: str,
        coerce: Callable[[Any], Any],
        *,
        kind: int,
        to_wire: Callable[[Any], Any] | None = None,
        from_wire: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__(sql, 0, sql, coerce, to_wire=to_wire, from_wire=from_wire)
        #: The extension that provides this type, as `CREATE EXTENSION` spells it.
        self.extension = extension
        #: The bare `pg_type.typname`, which is what the catalog is queried by
        #: and what distinguishes `vector` from `halfvec`, which share an
        #: extension but not a wire format.
        self.type_name = type_name
        self.shape_value = b"x" + sql.encode("utf-8")
        # A stable stand-in for the OID a fingerprint would otherwise record.
        # md5 because this is a name, not a security boundary; the top bit is
        # set so it can never collide with a real built-in OID.
        self.fingerprint_oid = 0x80000000 | int.from_bytes(
            hashlib.md5(sql.encode("utf-8"), usedforsecurity=False).digest()[:4], "big"
        ) & 0x7FFFFFFF
        _DECLARED_EXTENSION_TYPES.append(self)

    def require_oid(self, doing: str) -> int:
        """This type's resolved OID, refusing if resolution never reached it.

        Call this wherever the OID decides something -- wire framing, a
        migration descriptor -- and never where an unresolved type is the
        legitimate state (declaration, fingerprinting, introspection, the
        probe that is *about* to resolve it).

        The unbound state is not only "startup has not run yet".
        `bind_extension_oid` walks the types declared *when it is called*, so an
        `ExtensionType` constructed afterwards -- a model class defined after
        the application started -- keeps OID 0 until something resolves again.
        Zero is a legal-looking OID that means "unspecified" on the wire and
        "built-in" in a descriptor, so it has to be refused rather than framed.
        """
        oid = self.oid
        if oid == 0:
            raise ExtensionNotInstalledError(
                f"the {self.type_name!r} type has no OID yet, so {doing} "
                f"({self.sql}) cannot be decided: the {self.extension!r} extension "
                "is resolved once, against a live catalog, by "
                "wreath.orm.introspection.resolve_extension_types(registry) -- "
                "which the application lifespan runs at startup. A model class "
                "declared after that resolution was not there to be bound, and "
                "must be resolved again before it is used.",
                extension=self.extension,
            )
        return oid

    def to_wire(self, value: Any) -> Any:
        if self.oid == 0:
            self.require_oid("the bind parameter's type")
        wire = super().to_wire(value)
        # The driver infers a parameter's OID from its Python value, and a bare
        # list is refused there because it names no element type. An extension
        # value is a list that *does* know its type, so it carries the answer;
        # see `WireList`.
        if type(wire) is list:
            return WireList(wire, self.oid)
        # `sparsevec` is the same problem in a different shape: its value is not
        # a list at all, so it carries the OID on a copy rather than on the
        # caller's own object, which may outlive this bind and be bound again
        # against another database.
        if type(wire) is SparseVector:
            return wire._with_oid(self.oid)
        return wire


#: Every `ExtensionType` this process has declared, in declaration order.
#: Bounded by the application's model declarations, appended to only while a
#: class body runs, and read only by OID resolution at startup.
_DECLARED_EXTENSION_TYPES: list[ExtensionType] = []


def declared_extension_types() -> tuple[ExtensionType, ...]:
    """Every extension type declared in this process, in declaration order."""
    return tuple(_DECLARED_EXTENSION_TYPES)


def bind_extension_oid(type_name: str, oid: int) -> int:
    """Give every declared type named `type_name` the OID this database uses.

    Returns how many types were bound. Idempotent for a repeated identical OID,
    which is the ordinary case when several registries share one database.

    Raises:
        ValueError: the same type resolved to a *different* OID than a previous
            call. One process holds one codec table, so two databases whose
            `vector` OIDs disagree cannot both be served from it; that is
            reported here rather than by decoding one database's rows with the
            other's rules.
    """
    if oid <= 0:
        raise ValueError(f"{type_name!r} resolved to an invalid OID {oid}")
    bound = 0
    kind = 0
    for item in _DECLARED_EXTENSION_TYPES:
        if item.type_name != type_name:
            continue
        if item.oid not in (0, oid):
            raise ValueError(
                f"the {type_name!r} type is already bound to OID {item.oid} in this "
                f"process and this database assigns it {oid}; wreath resolves an "
                "extension type once per process, so two databases whose extension "
                "OIDs differ cannot share one interpreter"
            )
        item.oid = oid
        kind = _EXTENSION_KINDS[type_name]
        bound += 1
    if bound:
        from ..postgres import register_extension_codec

        register_extension_codec(type_name, oid, kind)
    return bound


def _unbind_extension_oids() -> None:
    """Return every declared extension type to its unresolved state.

    **For tests, and only for tests.** An application resolves once at startup
    and never again; calling this in a running process would leave a type
    unresolvable-looking while connections are still decoding rows with the OID
    it just forgot. It exists because a test process legitimately holds both a
    made-up OID (for the codec suites, which need no server) and a real one
    read from a live catalog, and `bind_extension_oid` is right to refuse those
    at the same time.

    The driver's codec table is deliberately *not* cleared: it is keyed by OID,
    an OID it already knows stays correct, and unregistering one would be the
    genuinely dangerous half of this.
    """
    for item in _DECLARED_EXTENSION_TYPES:
        item.oid = 0


#: The codec kind each extension type name is framed by. Adding a type means
#: adding a kind here and a branch in both codecs -- never a silent
#: fall-through to "bytes".
_EXTENSION_KINDS: dict[str, int] = {
    "vector": EXT_KIND_VECTOR,
    "halfvec": EXT_KIND_HALFVEC,
    "sparsevec": EXT_KIND_SPARSEVEC,
    "geography": EXT_KIND_GEOGRAPHY,
}


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

    `float` is refused rather than converted. A column is declared `numeric`
    precisely because binary floating point cannot hold its values -- accepting
    a float would put the collapse back in, one layer up from the codec that
    just removed it. `Decimal(str(x))` at the call site is the explicit way to
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
    #
    # This encoding is deliberately **discarded** rather than cached for
    # `_json_to_wire`, and the two dumps are not the same work done twice. The
    # cell holds the caller's own object, so `document.body["a"].append(3)`
    # changes what the column contains without coming back through this
    # function -- a cached encoding would write the value the caller had at
    # assignment rather than the one it has at flush. Pinned by
    # `tests/orm/test_declaration.py::test_a_json_column_serializes_the_value_
    # it_holds_at_the_wire_not_at_assignment`.
    _json_dumps(value)
    return value


def _json_to_wire(value: Any) -> str:
    return _json_dumps(value).decode("utf-8")


def _check_point(value: Any) -> Coordinate:
    if type(value) is not Coordinate:
        raise _type_error("Coordinate", value)
    return value


def _point_to_wire(value: Any) -> str:
    """The literal PostgreSQL parses: `(x,y)`, and x is longitude.

    PostGIS, GeoJSON and PostgreSQL's own `point` all put longitude first;
    only humans say "lat, lon". `Coordinate` refuses a positional pair for
    exactly that reason, and this is the one place the order is written down
    in the direction the database wants it.
    """
    return f"({value.lon!r},{value.lat!r})"


def _point_from_wire(value: Any) -> Coordinate:
    """Read `point` off the wire in either format the driver may hand over.

    An OID the driver's codec does not enumerate falls through to its terminal
    arm, which returns the field's bytes unchanged -- so what arrives here is
    the server's own representation and not a decoded value. That is the text
    form `(x,y)` on an unprepared path and 16 bytes of two big-endian float8 on
    a prepared one, so both are read rather than one being assumed.
    """
    if isinstance(value, Coordinate):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) == 16 and not raw.startswith(b"("):
            x, y = struct.unpack("!dd", raw)
            return Coordinate(lat=y, lon=x)
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid point wire value {raw!r}") from exc
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"invalid point wire value {value!r}")
    body = text.strip()
    if not body.startswith("(") or not body.endswith(")") or body.count(",") != 1:
        raise ValueError(f"invalid point wire value {text!r}")
    x_text, _, y_text = body[1:-1].partition(",")
    try:
        x = float(x_text)
        y = float(y_text)
    except ValueError as exc:
        raise ValueError(f"invalid point wire value {text!r}") from exc
    return Coordinate(lat=y, lon=x)


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
#: A WGS84 position, stored in core PostgreSQL's `point`. Not an extension type:
#: OID 600 is in the catalog, and core ships a GiST `point_ops` opclass, so the
#: index a proximity search needs is available with nothing installed. That is
#: the whole tier-1 claim -- see `docs/guides/geospatial.md`.
Point = PgType(
    "point", 600, "point", _check_point, to_wire=_point_to_wire, from_wire=_point_from_wire
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
    """Declare a one-dimensional PostgreSQL array of `element`.

    Each element is validated and wired through `element`'s own rules, so
    `Array(Uuid)` accepts a `list`/`tuple` of UUIDs and rejects anything
    else. Elements are non-nullable unless `nullable_elements=True`. Nested
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
        return _core.array_coerce(
            value, nullable_elements, element.name, element.oid, element.coerce
        )

    def to_wire(value: list[Any]) -> list[Any]:
        return _core.map_nullable(value, element.to_wire)

    def from_wire(value: list[Any]) -> list[Any]:
        return _core.map_nullable(value, element.from_wire)

    return _ArrayType(
        element,
        f"{element.name}[]",
        array_oid,
        f"{element.sql}[]",
        coerce,
        to_wire=to_wire,
        from_wire=from_wire,
    )


#: pgvector's own ceiling on a `vector` column's dimension. Indexes stop at
#: 2000, which is a planner limit rather than a storage one and so is not
#: enforced here -- an unindexed 4000-dimension column is legal and useful.
MAX_VECTOR_DIM = 16000


def _vector_dimension(kind: str, dim: int, maximum: int) -> int:
    """Validate the dimension shared by pgvector's dense vector types."""
    if dim.__class__ is not int:
        raise DeclarationError(f"{kind}() requires an int dimension, got {dim!r}")
    if not 1 <= dim <= maximum:
        raise DeclarationError(
            f"{kind}({dim}) is out of range; pgvector allows 1 to {maximum} dimensions"
        )
    return dim


def _dense_vector(
    kind: str,
    type_name: str,
    dim: int,
    maximum: int,
    *,
    half: bool,
    codec_kind: int,
) -> ExtensionType:
    dim = _vector_dimension(kind, dim, maximum)

    def coerce(value: Any) -> list[float]:
        return _core.float_sequence(value, dim, half)

    return ExtensionType(
        "vector", type_name, f"{type_name}({dim})", coerce, kind=codec_kind
    )


def Vector(dim: int) -> ExtensionType:
    """Declare a pgvector `vector(dim)` column.

    The Python value is a `list[float]` of exactly `dim` finite floats.
    Coercion is strict in both directions that matter: a wrong length is a
    dimension mismatch the database would reject anyway, and NaN or infinity is
    refused because pgvector stores neither and every distance involving one is
    meaningless.

    ```python
    class Document(Model, table="documents"):
        embedding: Mapped[list[float]] = column(
            Vector(1536), index="hnsw", index_ops="vector_cosine_ops"
        )
    ```
    Requires `CREATE EXTENSION vector` in the column's database. A registry that
    declares one against a database without it fails at startup, naming the
    extension and the schema, rather than at the first query with an OID error.
    """
    # `__class__` rather than `isinstance`, for the reason `_integer` gives above:
    # bool is an int subclass, and `Vector(True)` is a mistake rather than a
    # one-dimensional column. That also makes an `isinstance(dim, bool)` clause
    # here unreachable -- it was one, and a mutant sweep reported it as dead.
    return _dense_vector(
        "Vector", "vector", dim, MAX_VECTOR_DIM, half=False, codec_kind=EXT_KIND_VECTOR
    )


#: The largest finite IEEE-754 binary16. An element beyond it does not saturate,
#: it becomes an infinity -- which pgvector then refuses -- so the bound is part
#: of what a `halfvec` column can hold, not a codec detail.
MAX_HALF_MAGNITUDE = 65504.0

#: pgvector's ceiling on a `halfvec` column's dimension. Higher than `vector`'s
#: because each element is half the width; HNSW indexes it to 4000, again a
#: planner limit rather than a storage one and so not enforced here.
MAX_HALFVEC_DIM = 16000


def Halfvec(dim: int) -> ExtensionType:
    """Declare a pgvector `halfvec(dim)` column -- half-precision, half the storage.

    The same Python value as `Vector`: a `list[float]` of exactly `dim` finite
    floats. What differs is what the database keeps. Each element is stored as an
    IEEE-754 binary16 rather than a binary32, which halves both the column and any
    HNSW index over it -- the reason to reach for this at 1536 dimensions and up,
    where the index is usually the thing that stopped fitting in memory.

    ```python
    class Document(Model, table="documents"):
        embedding: Mapped[list[float]] = column(
            Halfvec(1536), index="hnsw", index_ops="halfvec_cosine_ops"
        )
    ```

    **Be deliberate about the precision.** binary16 carries about three decimal
    digits, so a value written and read back is *not* the value you wrote:
    `0.1` returns `0.0999755859375`. For embedding similarity that is
    normally irrelevant -- the ranking is what matters and it is robust to the
    third digit -- but a `halfvec` is the wrong type for anything you intend to
    compare for equality or accumulate.

    Two refusals, both at coercion time rather than at the server:

    * A magnitude above `MAX_HALF_MAGNITUDE` (65504) would round to an infinity,
      which pgvector rejects. Refused here so the error names the element rather
      than arriving as an `INSERT` failure naming neither it nor the column.
    * NaN and infinity, exactly as for `Vector`.

    Note the operator classes are the type's own -- `halfvec_cosine_ops`, not
    `vector_cosine_ops`. Naming `vector`'s opclass on a `halfvec` column is an
    error pgvector reports at index creation.

    Requires `CREATE EXTENSION vector` (the same extension provides both types).
    """
    # The extension is `vector`, not `halfvec`: one `CREATE EXTENSION vector`
    # provides both types. Naming the type here would make the not-installed error
    # tell the reader to install an extension that does not exist.
    return _dense_vector(
        "Halfvec",
        "halfvec",
        dim,
        MAX_HALFVEC_DIM,
        half=True,
        codec_kind=EXT_KIND_HALFVEC,
    )


def Sparsevec(dim: int) -> ExtensionType:
    """Declare a pgvector `sparsevec(dim)` column -- only the non-zero positions.

    The Python value is a `SparseVector`, not a list. That is the whole
    difference from `Vector`: a dimension here can be a million, and storing a
    million floats to say that nine of them are non-zero is what the type exists
    to avoid. A `dict` alone would not do, because it names no dimension, and
    the dimension is not recoverable from the elements.

    ```python
    from wreath.postgres import SparseVector

    class Document(Model, table="documents"):
        terms: Mapped[SparseVector] = column(
            Sparsevec(30000), index="hnsw", index_ops="sparsevec_l2_ops"
        )

    await session.add(Document(terms=SparseVector(30000, {17: 0.9, 4021: 1.4})))
    ```

    Indices are **1-based**, matching pgvector's own text form
    (`'{1:1.5,3:3.5}/5'`); the binary wire format is 0-based and the conversion
    lives in the codec, so the two numberings never both appear in application
    code.

    The dimension is checked at coercion: a `SparseVector` declaring a different
    one is refused here rather than by the server, because the server's message
    names neither the column nor which of the two dimensions it expected.

    At most `MAX_SPARSEVEC_NNZ` (16,000) elements may be non-zero, whatever the
    dimension -- pgvector's own ceiling, and a value denser than that wants
    `Vector` or `Halfvec` instead.

    Requires `CREATE EXTENSION vector` (the same extension provides all three
    types). Note the operator classes are the type's own -- `sparsevec_l2_ops`,
    not `vector_l2_ops` -- and that pgvector indexes a `sparsevec` to 1,000
    non-zero elements even though the column may hold 16,000.
    """
    if dim.__class__ is not int:
        raise DeclarationError(f"Sparsevec() requires an int dimension, got {dim!r}")
    if not 1 <= dim <= MAX_SPARSEVEC_DIM:
        raise DeclarationError(
            f"Sparsevec({dim}) is out of range; pgvector allows 1 to "
            f"{MAX_SPARSEVEC_DIM} dimensions"
        )

    def coerce(value: Any) -> SparseVector:
        if not isinstance(value, SparseVector):
            raise _type_error("SparseVector", value)
        if value.dim != dim:
            raise ValueError(
                f"sparsevec({dim}) requires a SparseVector of dimension {dim}, got "
                f"one of dimension {value.dim}"
            )
        return value

    return ExtensionType(
        "vector", "sparsevec", f"sparsevec({dim})", coerce, kind=EXT_KIND_SPARSEVEC
    )


# --- PostGIS geography --------------------------------------------------------
#
# Tier 2 of `wreath.geospatial`, and the only part of it that needs an extension.
# Tier 1 -- `Point`, its GiST index, `within()` and `nearest()` -- is core
# PostgreSQL and stays available on a server with nothing installed; see
# `docs/guides/geospatial.md` for which questions each tier answers.

#: EWKB's geometry code for a 2D point, ORed with the flag that says an SRID
#: follows. PostGIS writes this on output and reads it on input, so it is the
#: one form that needs no conversion in either direction.
_EWKB_POINT = 0x00000001
_EWKB_SRID_FLAG = 0x20000000

#: One EWKB point with an SRID: order byte, geometry code, SRID, x, y.
_EWKB_SRID_POINT = struct.Struct("<BIIdd")
_EWKB_BARE_POINT = struct.Struct("<BIdd")


def _geography_to_wire(srid: int) -> Callable[[Any], str]:
    """Build the encoder for one SRID: a `Coordinate` to EWKB hex.

    Hex rather than either of the two things it could be instead, because it is
    the one spelling *both* parameter paths accept: `geography_in` reads EWKB
    hex on the text path, and un-hexing it gives `geography_recv` exactly what
    it wants on the binary one. `Point` makes the same choice for the same
    reason -- one `to_wire` per column type, so the longitude-first order is
    written down once rather than once per path.
    """

    def to_wire(value: Any) -> str:
        # Longitude is x. PostGIS, GeoJSON and PostgreSQL's `point` all agree on
        # that and every human sentence disagrees, which is why `Coordinate`
        # refuses a positional pair and why this is the only place the order is
        # transcribed for this type.
        return _EWKB_SRID_POINT.pack(
            1, _EWKB_POINT | _EWKB_SRID_FLAG, srid, value.lon, value.lat
        ).hex()

    return to_wire


def _geography_from_wire(value: Any) -> Coordinate:
    """Read a `geography` off the wire in either format the driver may hand over.

    A prepared read returns the server's own EWKB bytes and an unprepared one
    returns the same bytes spelled as hex text; the codec hands both back
    unread, so this is the single place a geography is interpreted. Byte order
    is a field of the format rather than an assumption, and the SRID is
    optional, so both are read rather than either being presumed.

    `bytes.fromhex` here rather than the strict `binascii.unhexlify` the
    *encoder* uses: PostgreSQL is the writer on this side, so leniency admits
    nothing wreath has to reason about, while on the way out a lenient reading
    would have let the two codecs accept different input.
    """
    if isinstance(value, Coordinate):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    elif isinstance(value, str):
        raw = value.encode("ascii", "replace")
    else:
        raise ValueError(f"invalid geography wire value {value!r}")
    if raw[:1] not in (b"\x00", b"\x01"):
        # Not raw EWKB, so it is the hex spelling of it.
        try:
            raw = bytes.fromhex(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"invalid geography wire value {value!r}") from exc
    # Not redundant with the exact-length check below: the geometry code is read
    # *before* the layout is known, and `raw[1:5]` on a short buffer silently
    # yields a shorter slice and a plausible-looking integer, so the length
    # failure would be reported as "not a point".
    if len(raw) < _EWKB_BARE_POINT.size:
        raise ValueError(f"geography wire value is too short to be a point: {raw!r}")
    big_endian = raw[0] == 0
    code = int.from_bytes(raw[1:5], "big" if big_endian else "little")
    if code & ~_EWKB_SRID_FLAG != _EWKB_POINT:
        raise ValueError(
            f"wreath declares geography(Point,...) columns and this value is EWKB "
            f"geometry type {code & ~_EWKB_SRID_FLAG}, not a point"
        )
    layout = _EWKB_SRID_POINT if code & _EWKB_SRID_FLAG else _EWKB_BARE_POINT
    if len(raw) != layout.size:
        raise ValueError(
            f"geography wire value is {len(raw)} bytes where an EWKB point is "
            f"{layout.size}: {raw!r}"
        )
    # `struct` has no runtime byte-order switch, so the big-endian arm reverses
    # the two doubles rather than a second Struct being kept in step with the
    # first. Big-endian EWKB is what a peer wrote, never what wreath writes.
    if big_endian:
        x = struct.unpack_from(">d", raw, layout.size - 16)[0]
        y = struct.unpack_from(">d", raw, layout.size - 8)[0]
    else:
        x, y = struct.unpack_from("<dd", raw, layout.size - 16)
    return Coordinate(lat=y, lon=x)


def Geography(*, srid: int = 4326) -> ExtensionType:
    """Declare a PostGIS `geography(Point,srid)` column.

    The Python value is a `wreath.geospatial.Coordinate`, the same value type a
    tier-1 `Point` column holds, so a model can move between the tiers without
    its handlers changing.

    ```python
    class Station(Model, table="stations"):
        at: Mapped[Coordinate] = column(Geography(), index="gist")
    ```

    **Only the point form is declarable.** `geography` also spells polygons and
    line strings, and wreath has no value type for either -- a
    `geography(Polygon,4326)` column would have nothing to hold and nothing to
    validate, so it is left to a hand-written migration rather than declared
    with a hole in it.

    Requires `CREATE EXTENSION postgis` in the column's database. A registry
    that declares one against a database without it fails at startup, naming
    the extension and the schema, rather than at the first query with an OID
    error -- the same contract `Vector` has, because it is the same mechanism.

    **Tier 1 needs none of this.** `Point` stores a coordinate in core
    PostgreSQL and answers "within" and "nearest" through a core GiST index.
    Reach for `Geography` when you need what tier 1 does not do: a true KNN
    ordering, containment against a polygon, or a projection.
    """
    # `__class__` rather than `isinstance`, for the reason `_integer` gives:
    # bool is an int subclass, and `Geography(srid=True)` is a mistake rather
    # than a declaration of SRID 1.
    if srid.__class__ is not int or srid <= 0:
        raise DeclarationError(
            f"Geography() requires a positive int SRID, got {srid!r}; 4326 is "
            "WGS84 and is what every GPS fix, GeoJSON document and "
            "wreath.geospatial.Coordinate is already in"
        )
    return ExtensionType(
        "postgis",
        "geography",
        f"geography(Point,{srid})",
        _check_point,
        kind=EXT_KIND_GEOGRAPHY,
        to_wire=_geography_to_wire(srid),
        from_wire=_geography_from_wire,
    )


#: PostgreSQL's own `bit`. Binary quantization needs no extension for the *type*
#: -- only for the operators over it -- so this is a compile-time constant like
#: every other built-in here, and none of the dynamic-OID machinery applies.
BIT_OID = 1560

#: How long a `bit` column wreath will declare. PostgreSQL's own limit is
#: `INT_MAX` bits; this one is `Vector`'s ceiling times the 32 bits a float4
#: quantizes down to, which is the largest embedding anyone is quantizing.
MAX_BIT_LENGTH = 512000


def Bit(length: int) -> PgType:
    """Declare a PostgreSQL `bit(length)` column -- one bit per dimension.

    This is the storage half of *binary quantization*: an embedding whose
    elements are replaced by their signs, so 1,536 float4s (6,148 bytes) become
    1,536 bits (192 bytes), a 32x reduction that keeps enough of the geometry to
    rank candidates before a re-scoring pass over the real vectors.

    ```python
    class Document(Model, table="documents"):
        signature: Mapped[str] = column(
            Bit(1536), index="hnsw", index_ops="bit_hamming_ops"
        )
    ```

    The Python value is a `str` of `'0'` and `'1'`, exactly `length` of them --
    what `psql` shows and what `'101'::bit(3)` means. `bytes` is accepted on the
    way *in* as a convenience for the quantizers that produce it
    (`numpy.packbits(...).tobytes()`), and is unpacked using the declared
    length; the trailing bits of the final byte must be zero, since they name
    positions the column does not have. A read always returns the `str`, because
    a `bytes` return would have to carry its own bit count to be unambiguous.

    **`bit` is a built-in type; the *operators* are pgvector's.**
    `hamming_distance` and `jaccard_distance` (`<~>` and `<%>`) and the
    `bit_hamming_ops` / `bit_jaccard_ops` index classes come from
    `CREATE EXTENSION vector`. So a `Bit` column needs no extension to *store*,
    and there is no OID to resolve -- but a query that ranks by one of those
    distances does.
    """
    if length.__class__ is not int:
        raise DeclarationError(f"Bit() requires an int length, got {length!r}")
    if not 1 <= length <= MAX_BIT_LENGTH:
        raise DeclarationError(
            f"Bit({length}) is out of range; wreath declares 1 to {MAX_BIT_LENGTH} bits"
        )
    width = (length + 7) // 8

    def coerce(value: Any) -> str:
        if isinstance(value, (bytes, bytearray)):
            if len(value) != width:
                raise ValueError(
                    f"bit({length}) packs into {width} bytes, got {len(value)}"
                )
            padding = -length % 8
            number = int.from_bytes(value, "big")
            if padding and number & ((1 << padding) - 1):
                raise ValueError(
                    f"bit({length}) leaves {padding} unused bits in the last byte "
                    "and they must be zero; they name positions this column does "
                    "not have"
                )
            return format(number >> padding, f"0{length}b")
        if not isinstance(value, str):
            raise _type_error("str of '0' and '1', or packed bytes", value)
        if len(value) != length:
            raise ValueError(
                f"bit({length}) requires exactly {length} bits, got {len(value)}"
            )
        if len(value) != value.count("0") + value.count("1"):
            raise ValueError("a bit string may hold only '0' and '1'")
        return value

    return PgType(f"bit({length})", BIT_OID, f"bit({length})", coerce)


#: PostgreSQL's own `tsvector`. Full-text search needs no extension, so unlike
#: pgvector's types this is a compile-time constant like every other built-in
#: here -- none of the dynamic-OID machinery applies to it.
TSVECTOR_OID = 3614

#: A text-search configuration, as `to_tsvector('english', ...)` spells it. It
#: reaches DDL and query text as a quoted literal rather than a bind, so the
#: shape is fixed at declaration instead of escaped at render. Matched with
#: `fullmatch`, never `match`: `$` also matches immediately before a trailing
#: newline, so `^...$` accepted `TsVector("english\n")` into that literal.
_TS_CONFIG = re.compile(r"[a-z_][a-z0-9_]*")


def _refuse_tsvector_write(value: Any) -> Any:
    raise TypeError(
        "a tsvector column is generated: PostgreSQL derives it from its source "
        "columns on every write, so assigning it would be discarded -- write the "
        "source columns instead"
    )


class TsVectorType(GeneratedType):
    """A `tsvector` PostgreSQL derives from other columns of the same row.

    Carries the two things the DDL needs and nothing else: the text-search
    `config` to analyse under, and the `sources` to analyse. The expression is
    rendered once the registry knows those columns' database names and types.
    """

    __slots__ = ("config", "sources")

    def __init__(self, config: str, sources: tuple[str, ...]) -> None:
        super().__init__("tsvector", TSVECTOR_OID, "tsvector", _refuse_tsvector_write)
        #: The text-search configuration, e.g. `english` or `simple`.
        self.config = config
        #: The columns this vector is derived from, in the order they concatenate.
        self.sources = sources

    def __repr__(self) -> str:
        return f"<TsVector {self.config} over {', '.join(self.sources)}>"


def TsVector(config: str = "english", *, sources: Any) -> TsVectorType:
    """Declare a generated `tsvector` column over `sources`.

    ```python
    class Document(Model, table="documents"):
        title: Mapped[str] = column(Text)
        body: Mapped[str] = column(Text)
        search: Mapped[bytes] = column(
            TsVector("english", sources=("title", "body")), index="gin"
        )
    ```
    That renders
    `GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'') || ' ' ||
    coalesce(body,''))) STORED`, which is the form that keeps the GIN index
    correct without a trigger: PostgreSQL recomputes the column inside the same
    statement that changed `title` or `body`, so there is no window where the
    index disagrees with the row.

    Each source must be a declared `Text` column of the same model. Other types
    are refused because PostgreSQL's own deparsing of the expression casts them
    (`(COALESCE(vch, ''::character varying))::text`), and a spelling wreath
    cannot predict is a permanent, self-renewing migration drift -- the same
    reason `wreath.orm._index_predicate` keeps its vocabulary small.

    The value is the database's, not the application's: assigning one raises,
    and passing one to the constructor raises. Query it with `.matches()` and
    `.rank()` rather than reading it.
    """
    if not isinstance(config, str) or not _TS_CONFIG.fullmatch(config):
        raise DeclarationError(
            f"TsVector(config={config!r}) must name a text-search configuration "
            "such as 'english' or 'simple'; it is rendered into DDL rather than "
            "bound"
        )
    if not isinstance(sources, (list, tuple)):
        raise DeclarationError(
            "TsVector(sources=...) takes a sequence of column names, such as "
            "sources=('title', 'body')"
        )
    names = tuple(sources)
    if not names:
        raise DeclarationError(
            "TsVector(sources=...) requires at least one column to analyse"
        )
    for name in names:
        if not isinstance(name, str) or not name:
            raise DeclarationError(f"TsVector source {name!r} is not a column name")
    if len(set(names)) != len(names):
        raise DeclarationError(
            f"TsVector(sources={names!r}) names the same column twice; a repeated "
            "source only weights it, which setweight() is for"
        )
    return TsVectorType(config, names)


#: PostgreSQL type names that *index* content rather than carry it: pgvector's
#: `vector` and PostgreSQL's own `tsvector`. Matched on the bare catalog name,
#: so `vector(1536)` and `vector(3)` are one entry and a future `halfvec` is a
#: one-word addition.
_RETRIEVAL_TYPE_NAMES = frozenset({"tsvector", "vector"})


def _is_retrieval_type(pg_type: PgType) -> bool:
    """Whether a column of `pg_type` is a retrieval index rather than content.

    Such a column is infrastructure: a `tsvector` is *derived* from columns that
    are already in the same row, and an embedding is a lookup key for content
    that lives elsewhere in it. Neither is something a person edits, both are
    large -- a `Vector(1536)` is six kilobytes -- and neither sorts or filters
    usefully. The generated layers therefore withhold them by default rather
    than treating them as ordinary columns: see `wreath.crud.retrieval_fields`
    and `wreath.pagination.sortable_fields`.

    Kept here, beside the types themselves, so the two layers cannot disagree
    about what counts and a third type is added in one place.
    """
    name = pg_type.type_name if isinstance(pg_type, ExtensionType) else pg_type.name
    return name in _RETRIEVAL_TYPE_NAMES


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
# validation and introspection can decode an array column by its OID. `Array`
# always returns the same OID for a given element, so any of these decodes any
# column of that array type.
for _element in (
    Bool, Int16, Int32, Int64, Float32, Float64, Text, Varchar,
    Bytea, Uuid, Date, Timestamp, TimestampTz, Numeric, Json, Jsonb,
):
    _canonical_array = Array(_element)
    BY_OID[_canonical_array.oid] = _canonical_array

#: The `text[]` type the jsonb key operators (`?|`, `?&`, `#>>` paths)
#: bind their operands as.
TextArray = Array(Text)

__all__ = [
    "BIT_OID",
    "BY_OID",
    "EXT_KIND_GEOGRAPHY",
    "EXT_KIND_HALFVEC",
    "EXT_KIND_SPARSEVEC",
    "EXT_KIND_VECTOR",
    "Geography",
    "Halfvec",
    "MAX_BIT_LENGTH",
    "MAX_HALFVEC_DIM",
    "MAX_HALF_MAGNITUDE",
    "MAX_SPARSEVEC_DIM",
    "MAX_SPARSEVEC_NNZ",
    "MAX_VECTOR_DIM",
    "TSVECTOR_OID",
    "Array",
    "Bit",
    "Bool",
    "Bytea",
    "Date",
    "ExtensionType",
    "Float4",
    "Float8",
    "GeneratedType",
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
    "Point",
    "SparseVector",
    "Sparsevec",
    "Text",
    "TextArray",
    "Timestamp",
    "TimestampTz",
    "TsVector",
    "TsVectorType",
    "Uuid",
    "Varchar",
    "Vector",
    "bind_extension_oid",
    "declared_extension_types",
]
