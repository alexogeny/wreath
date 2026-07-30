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
import math
import re
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
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


class WireList(list):
    """A list-valued parameter that names its own PostgreSQL OID.

    The driver infers a parameter's OID from the Python value it is handed, and
    a bare list is deliberately refused: `[1, 2]` is equally `int4[]`, `int8[]`
    or `numeric[]`, and `[]` names no element type at all. A pgvector value is a
    list too, and it *does* know its type -- so it carries the answer.

    A `list` subclass rather than a wrapper, so both codec twins keep taking the
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
        return WireList(wire, self.oid) if type(wire) is list else wire


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
    made-up OID (for the pure codec suites, which need no server) and a real one
    read from a live catalog, and `bind_extension_oid` is right to refuse those
    at the same time.

    The driver's codec table is deliberately *not* cleared: it is keyed by OID,
    an OID it already knows stays correct, and unregistering one would be the
    genuinely dangerous half of this.
    """
    for item in _DECLARED_EXTENSION_TYPES:
        item.oid = 0


#: The codec kind each extension type name is framed by. Adding a type means
#: adding a kind here and a branch in both codec twins -- never a silent
#: fall-through to "bytes".
_EXTENSION_KINDS: dict[str, int] = {
    "vector": EXT_KIND_VECTOR,
    "halfvec": EXT_KIND_HALFVEC,
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


#: pgvector's own ceiling on a `vector` column's dimension. Indexes stop at
#: 2000, which is a planner limit rather than a storage one and so is not
#: enforced here -- an unindexed 4000-dimension column is legal and useful.
MAX_VECTOR_DIM = 16000


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
    if dim.__class__ is not int or isinstance(dim, bool):
        raise DeclarationError(f"Vector() requires an int dimension, got {dim!r}")
    if not 1 <= dim <= MAX_VECTOR_DIM:
        raise DeclarationError(
            f"Vector({dim}) is out of range; pgvector allows 1 to {MAX_VECTOR_DIM} "
            "dimensions"
        )

    def coerce(value: Any) -> list[float]:
        if not isinstance(value, (list, tuple)):
            raise _type_error("list or tuple of floats", value)
        if len(value) != dim:
            raise ValueError(
                f"vector({dim}) requires exactly {dim} values, got {len(value)}"
            )
        out: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise _type_error("float", item)
            number = float(item)
            if not math.isfinite(number):
                raise ValueError(
                    "a vector element must be finite; pgvector stores neither NaN "
                    "nor infinity and every distance involving one is undefined"
                )
            out.append(number)
        return out

    return ExtensionType(
        "vector", "vector", f"vector({dim})", coerce, kind=EXT_KIND_VECTOR
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
    if dim.__class__ is not int or isinstance(dim, bool):
        raise DeclarationError(f"Halfvec() requires an int dimension, got {dim!r}")
    if not 1 <= dim <= MAX_HALFVEC_DIM:
        raise DeclarationError(
            f"Halfvec({dim}) is out of range; pgvector allows 1 to {MAX_HALFVEC_DIM} "
            "dimensions"
        )

    def coerce(value: Any) -> list[float]:
        if not isinstance(value, (list, tuple)):
            raise _type_error("list or tuple of floats", value)
        if len(value) != dim:
            raise ValueError(
                f"halfvec({dim}) requires exactly {dim} values, got {len(value)}"
            )
        out: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise _type_error("float", item)
            number = float(item)
            if not math.isfinite(number):
                raise ValueError(
                    "a halfvec element must be finite; pgvector stores neither NaN "
                    "nor infinity and every distance involving one is undefined"
                )
            if not -MAX_HALF_MAGNITUDE <= number <= MAX_HALF_MAGNITUDE:
                raise ValueError(
                    f"halfvec element {number!r} is outside binary16's range of "
                    f"+/-{MAX_HALF_MAGNITUDE}; it would round to an infinity, which "
                    "pgvector refuses. Use Vector() for values this large."
                )
            out.append(number)
        return out

    # The extension is `vector`, not `halfvec`: one `CREATE EXTENSION vector`
    # provides both types. Naming the type here would make the not-installed error
    # tell the reader to install an extension that does not exist.
    return ExtensionType(
        "vector", "halfvec", f"halfvec({dim})", coerce, kind=EXT_KIND_HALFVEC
    )


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
    if isinstance(sources, str) or not isinstance(sources, (list, tuple)):
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
    "BY_OID",
    "EXT_KIND_HALFVEC",
    "EXT_KIND_VECTOR",
    "Halfvec",
    "MAX_HALFVEC_DIM",
    "MAX_HALF_MAGNITUDE",
    "MAX_VECTOR_DIM",
    "TSVECTOR_OID",
    "Array",
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
