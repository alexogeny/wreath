"""Alembic migrations, split by how much of each operation the ORM image proves."""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED, UNSUPPORTED

MIGRATIONS: dict[str, tuple[str, str, str, str]] = {
    "mig.manual": (
        "migration_op",
        "other",
        UNSUPPORTED,
        "postgresql_using= is a cast that only you can write -- nothing about the model says how the old values become the new ones. Keep this revision in Alembic.",
    ),
    # Alembic operations are the single biggest file count in a mature app.
    # Most are ordinary DDL that `wreath migrations generate`
    # derives from the models; the ones that are not are worth separating,
    # because they are the ones that make a deploy slow, risky, or wrong.
    # `mig.derived` is translated for the same reason `pydantic.config_forbid`
    # and `resp.jsonable` are: the determined target is *no hand-written code*.
    # Wreath's migration source of truth is the ORM image, and detection covers
    # tables, columns (type, nullability, identity, generated, server default),
    # primary keys, unique constraints, foreign keys and btree indexes. Every operation in
    # that set is a function of the model change the porter is already making,
    # so there is nothing left to decide at the revision. What is NOT in that
    # set gets its own verdict below rather than riding along on this one.
    "mig.derived": (
        "migration_op",
        "other",
        TRANSLATED,
        "There is nothing to write here. wreath compares the models with the database and produces this migration itself: check the ported model declares the end state, then run `wreath migrations generate`. A migration that drops something needs --allow-destructive when it is applied.",
    ),
    "mig.schema_op": (
        "migration_op",
        "other",
        NEEDS_REVIEW,
        "This changes something wreath's models cannot describe yet (a check or exclusion constraint, a constraint whose kind the call does not name, or an argument that is not a literal). Either move the object onto the model, or leave this revision in Alembic.",
    ),
    "mig.rename": (
        "migration_op",
        "other",
        NEEDS_REVIEW,
        "A rename is the one ordinary-looking migration that goes wrong on its own. wreath compares the shape of the models with the shape of the database, and a renamed table or column looks exactly like one thing dropped and another created -- which would throw the data away. Keep this revision in Alembic, or do the rename directly in the database first.",
    ),
    "mig.index_manual": (
        "migration_op",
        "other",
        UNSUPPORTED,
        "wreath's migrations cover plain btree indexes. This one is an expression, partial, covering or non-btree index, and it would be written out as an operation that cannot actually be applied. Keep it in Alembic.",
    ),
    "mig.unmodelled_type": (
        "migration_op",
        "other",
        NEEDS_REVIEW,
        "This column's type has no equivalent in wreath's ORM (Time, Interval, Enum, INET, TSVECTOR and a fixed-width CHAR are the usual ones), so nothing on the model can produce it. Either pick a type wreath does model, or keep this table in Alembic.",
    ),
    "mig.raw_sql": (
        "migration_op",
        "other",
        UNSUPPORTED,
        "op.execute() runs SQL nobody can derive from a model. Keep this revision in Alembic.",
    ),
    # Deferred data migrations shipped (design 24), so "keep it in Alembic" stopped
    # being true. The verdict stays needs-review because the *body* is bespoke —
    # a Recode wants the old->new mapping written out, which is the thing the
    # `op.execute(UPDATE ...)` in this revision encodes and a differ cannot read.
    "mig.data": (
        "migration_op",
        "other",
        NEEDS_REVIEW,
        "op.get_bind() means this revision rewrites rows, not just the schema -- the kind of migration that holds a deploy open for an hour on a large table. Wreath does this without the outage: declare Recode(Model.col, mapping={...}) next to the model for a change of values in place, or Retype for a change of type (new column, backfill, verify, swap), and drive it with jobs.drive(). The app starts and serves immediately while the rows convert in chunks, and wreath refuses a later migration that would narrow the column too early. The mapping is the part only you can write.",
    ),
}
