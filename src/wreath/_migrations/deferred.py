"""Deferred data migrations: the two shapes, and the pass each one derives.

A migration that adds a column takes milliseconds and belongs in front of
startup. A migration that rewrites ten million rows takes an hour and does not.
`wreath.passes` already walks a big table durably, resumably and paced;
what it cannot do is say whether running it is *safe*, and that is what these
declarations are for.

Two shapes, because doc 16 conflated them and they need different mechanisms:

`Recode`
    Same column, same PostgreSQL type, different values -- a status column
    moving from `"1"` to `"planned"`. Rows genuinely differ mid-window and
    the hazard is **semantic**: the column reads fine and means something else.
    Its safety mechanism is the predicate scan in `._migrations.scan`.

`Retype`
    The column's type changes. It cannot be done in place --
    `ALTER COLUMN … TYPE` rewrites the table under `ACCESS EXCLUSIVE`, which
    is the hour-long deploy block being avoided -- so it is the four-step form:
    add a nullable column, fill it, prove it full, and let a *later* migration
    drop the old one. Two columns exist throughout, so the hazard is
    **nullability**, which is loud where a wrong comparison is silent. Its
    safety mechanism is the gate.

There is deliberately no `Transitional` type. A PostgreSQL column has exactly
one type, so "one column holds both shapes mid-window" is not representable --
`orm/types.py` opens with that rule and `orm/model.py` puts the OID in a
plan-cache key. The declaration carries the mapping instead, and the mapping is
what makes the scan decidable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..passes import Ceiling, ChunkedPass, DutyCycle, Rewrite, Rows, Sql, column_fact
from .scan import ScanReport, _column_key, scan


class DeferredDeclarationError(ValueError):
    """A deferred migration that could not be declared."""


#: Why a deferred migration may take a fixed ceiling over a key it cannot prove
#: is monotone.
#:
#: `Ceiling.at_launch()` normally refuses such a key, because a row inserted
#: behind the cursor is one the pass will never see. For a deferred migration
#: that row is *harmless* rather than absent, and the reason is the precondition
#: every pass is declared under (`passes.py:168-169`): a pass converts the
#: past, and the application writes the future in the shape being converted to.
#: A row arriving mid-walk is therefore already in the new encoding and needs no
#: visit.
#:
#: This is the escape hatch used as intended -- a sentence a reviewer reads,
#: naming the rule that makes it sound -- rather than a flag that switches the
#: check off. It holds only while the precondition does, which is why the guide
#: states the precondition before it states anything else.
PRECONDITION = (
    "a deferred migration converts the past while the application writes the "
    "future in the shape being converted to, so a row inserted behind the "
    "cursor during the walk is already in the new encoding"
)


def _model_of(column: Any) -> Any:
    inner = getattr(column, "column", column)
    owner = getattr(inner, "owner", None)
    if owner is None:
        raise DeferredDeclarationError(
            f"expected a model column such as Trek.grade, got {column!r}"
        )
    return owner


def _primary_key(model: Any) -> tuple[Any, ...]:
    columns = tuple(
        item for item in getattr(model, "__wreath_columns__", ()) if item.primary_key
    )
    if not columns:
        raise DeferredDeclarationError(
            f"{model.__name__} has no primary key, so a chunked walk over it has "
            f"no ordered key to page by"
        )
    return columns


def _walk_key(model: Any) -> Any:
    """The keyset key: the model's primary key, which is unique and indexed.

    `Rows(key=...)` takes column *expressions* (`Trek.id`), so the raw
    `Column` objects on the model are resolved back
    through the class to the descriptors' expressions.
    """
    expressions = tuple(
        getattr(model, item.python_name) for item in _primary_key(model)
    )
    return expressions[0] if len(expressions) == 1 else expressions


@dataclass(frozen=True, slots=True)
class Recode:
    """Shape A: re-encode one column's values in place.

    Args:
        column: the column being re-encoded.
        mapping: old value to new value. **Finite and invertible**, because that
            is exactly what lets the scan widen `col == new` into
            `col IN (new, old)` rather than merely permitting it. A conversion
            that cannot enumerate its pairs is not a `Recode`.
        chunk: rows per chunk, passed straight to the pass.
        pace: how much of the machine the walk may be.
        name: the pass's ledger identity; derived from the column by default.

    The declaration lives beside the model rather than inside a migration file,
    because this repository generates migration artifacts from the catalog and
    has no authored-migration surface to hang a decorator on. See the guide.
    """

    column: Any
    mapping: Any
    chunk: int = 10_000
    pace: Any = None
    name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mapping, dict) or not self.mapping:
            raise DeferredDeclarationError(
                "Recode(mapping=...) must be a non-empty dict of old value to "
                "new value. A conversion that cannot enumerate its pairs is not "
                "a Recode -- it forfeits the scan, and the framework says so "
                "rather than pretending."
            )
        targets = list(self.mapping.values())
        if len(set(targets)) != len(targets):
            raise DeferredDeclarationError(
                "Recode(mapping=...) must be invertible: two old values mapping "
                "to one new value cannot be widened back, so a predicate over "
                "the new value has no single old value to also accept."
            )
        overlap = set(self.mapping) & set(targets)
        if overlap:
            raise DeferredDeclarationError(
                f"Recode(mapping=...) has {sorted(overlap)[0]!r} on both sides, so "
                f"a row holding it is indistinguishable from converted and "
                f"unconverted; the walk could not tell when it had finished."
            )
        if not isinstance(self.chunk, int) or isinstance(self.chunk, bool) or self.chunk < 1:
            raise DeferredDeclarationError(f"chunk must be a positive int; got {self.chunk!r}")
        _model_of(self.column)

    # -- identity ---------------------------------------------------------

    @property
    def converts(self) -> str:
        """`schema.table.column` for the column being converted."""
        return _column_key(self.column)

    @property
    def pass_name(self) -> str:
        return self.name or f"recode_{self.converts.replace('.', '_')}"

    # -- what it derives --------------------------------------------------

    def build(self) -> ChunkedPass:
        """The walk that performs the conversion.

        No gate: Shape A adds no column, so there is nothing for a later
        migration to narrow and no fact for it to wait on. The safety mechanism
        is `scan`, and it runs before the walk starts rather than after it
        finishes.

        It does declare `rewrites`, which is the *downgrade* half of the same
        question. Having no gate means having no `guards`, so before this the
        ledger recorded no association between this pass and the column at all
        -- a downgrade could not have found it to refuse. `rewrites` is that
        association, and unlike `guards` it is never cleared: the old values
        are gone from the table and finishing does not bring them back.
        """
        model = _model_of(self.column)
        inner = getattr(self.column, "column", self.column)
        name = inner.database_name
        cases = " ".join(
            f"WHEN {_literal(old)} THEN {_literal(new)}" for old, new in self.mapping.items()
        )
        return ChunkedPass(
            self.pass_name,
            over=model,
            units=Rows(key=_walk_key(model), limit=self.chunk),
            frontier=Ceiling.at_launch(monotone=PRECONDITION),
            work=Rewrite(
                set_={name: Sql(f"CASE {name} {cases} ELSE {name} END", ())},
                # An `IN` list rather than `= ANY(?)`, because the driver infers
                # each parameter's type from its Python value and has no case for
                # `list` -- `ANY` with one bound array raises `unsupported
                # PostgreSQL value type` the first time it reaches a real server.
                # The mapping is an enum's worth of values, so one placeholder
                # each is the right size.
                where=Sql(
                    f"{name} IN ({', '.join('?' * len(self.mapping))})",
                    tuple(self.mapping),
                ),
            ),
            rewrites=column_fact(*self.converts.split(".", 2)),
            pace=self.pace or DutyCycle(0.25),
        )

    def scan(self, **populations: Any) -> ScanReport:
        """Every read of this column, classified. See `._migrations.scan`."""
        return scan(self.column, dict(self.mapping), **populations)


@dataclass(frozen=True, slots=True)
class Retype:
    """Shape B: change one column's type by draining it into a new one.

    Args:
        column: the column being drained -- **the one a later migration will
            narrow**, which is what the published fact names.
        into: the new column's name, added by this migration's own DDL as
            nullable. Nullable-then-not is a change the ORM already models.
        using: the SQL expression producing the new value.
        chunk: rows per chunk, passed straight to the pass.
        pace: how much of the machine the walk may be.
        name: the pass's ledger identity; derived from the column by default.

    The gate is not optional here and it is the half X1 cannot ship without.
    Verification is `NOT VALID` then `VALIDATE CONSTRAINT` -- the constraint
    the swap migration is *about to add* -- so the thing proven and the thing
    later enforced are the same predicate, and a walk that was subtly wrong
    cannot verify its own bug.
    """

    column: Any
    into: str
    using: Any
    chunk: int = 10_000
    pace: Any = None
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.into or not str(self.into).replace("_", "").isalnum():
            raise DeferredDeclarationError(
                f"Retype(into=...) must be a plain column name; got {self.into!r}"
            )
        inner = getattr(self.column, "column", self.column)
        if self.into == inner.database_name:
            raise DeferredDeclarationError(
                f"Retype(into={self.into!r}) names the column being drained. The "
                f"whole point of the two-column form is that the old column "
                f"survives the window so old code keeps working."
            )
        if self.using is None:
            raise DeferredDeclarationError(
                "Retype(using=...) needs the expression that produces the new value"
            )
        if not isinstance(self.chunk, int) or isinstance(self.chunk, bool) or self.chunk < 1:
            raise DeferredDeclarationError(f"chunk must be a positive int; got {self.chunk!r}")
        _model_of(self.column)

    @property
    def converts(self) -> str:
        return _column_key(self.column)

    @property
    def publishes(self) -> str:
        """The fact a later migration waits on before dropping the old column."""
        schema, table, name = self.converts.split(".", 2)
        return column_fact(schema, table, name)

    @property
    def pass_name(self) -> str:
        return self.name or f"retype_{self.converts.replace('.', '_')}"

    @property
    def constraint_name(self) -> str:
        return f"{self.into}_present"

    def build(self) -> ChunkedPass:
        """The walk, its gate, and the fact the gate publishes.

        `Ceiling.at_launch()` and `scope="pass"` are not a choice: a gate
        with `scope="unit"` cannot publish a fact, and a whole-pass gate is
        refused on a recurring pass, so a pass that guards a column is
        necessarily a bounded one that finishes.
        """
        from ..passes import Constraint, Gate  # noqa: PLC0415 - avoids a cycle at import

        model = _model_of(self.column)
        expression = self.using if isinstance(self.using, Sql) else Sql(str(self.using), ())
        return ChunkedPass(
            self.pass_name,
            over=model,
            units=Rows(key=_walk_key(model), limit=self.chunk),
            frontier=Ceiling.at_launch(monotone=PRECONDITION),
            work=Rewrite(set_={self.into: expression}, where=Sql(f"{self.into} IS NULL", ())),
            gate=Gate(
                verify=Constraint(self.constraint_name, f"{self.into} IS NOT NULL"),
                publishes=self.publishes,
                scope="pass",
            ),
            pace=self.pace or DutyCycle(0.25),
        )

    def scan(self, **populations: Any) -> ScanReport:
        """Shape B has no re-encode window, so there is nothing to scan.

        Both columns are separately typed and correct throughout; an unconverted
        row reads `NULL`, which the ORM already models and the gate proves
        gone. Returning an explicitly empty report rather than raising keeps one
        calling shape for both declarations.
        """
        return ScanReport(column=self.converts, examined=0, shape="retype")


def _literal(value: Any) -> str:
    """A SQL literal for a mapping key or value.

    Mapping entries are declared in Python source, never supplied by a request,
    so this is a declaration-time rendering rather than a value binding path.
    Anything that is not a string or a number is refused rather than guessed at.
    """
    if isinstance(value, bool) or value is None:
        raise DeferredDeclarationError(
            f"a Recode mapping entry must be a string or a number; got {value!r}"
        )
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    raise DeferredDeclarationError(
        f"a Recode mapping entry must be a string or a number; got {value!r}"
    )
