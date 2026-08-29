"""Alembic operations, split by how much of each one the ORM image proves.
Read by `.scan`, which is where a migration call is recognized."""

from __future__ import annotations

# Alembic operations shaped like something `wreath migrations detect` derives from
# the ORM image. Scoped to what detection actually covers — tables, columns,
# primary keys, unique constraints, foreign keys and btree indexes.
# Being in this set only makes an operation a *candidate*; the arguments decide.
_MIG_DERIVED_OPS = frozenset(
    {
        "add_column",
        "drop_column",
        "create_table",
        "drop_table",
        "create_index",
        "drop_index",
        "alter_column",
        "create_unique_constraint",
        "create_primary_key",
        "create_foreign_key",
    }
)
# A rename reads as drop+create to an image differ, which would move no data.
_MIG_RENAME_OPS = frozenset({"rename_table"})
# Operations naming an object the ORM cannot declare, or whose kind the call does
# not say (`drop_constraint("uq_x", "t")` — unique? check? exclusion?).
_MIG_REVIEW_OPS = frozenset(
    {
        "create_check_constraint",
        "drop_constraint",
        "create_exclude_constraint",
    }
)
# `sa.<T>` / `postgresql.<T>` column types that have a wreath PgType
# (wreath/orm/types.py). Time, Interval, Enum, INET, TSVECTOR, HSTORE, MONEY and
# a bare `CHAR(n)` are absent on purpose: there is no PgType to derive them from.
# Numeric/DECIMAL used to be on that absent list and no longer belong there —
# `wreath.orm.types.Numeric` ships, so every money column was being told to stay
# in Alembic over a type wreath has had all along. That is the specific way this
# table goes stale, and it is why each entry is a name that was checked against
# `orm/types.py` rather than remembered.
_SA_MODELLED_TYPES = frozenset(
    {
        "Integer",
        "INTEGER",
        "BigInteger",
        "BIGINT",
        "SmallInteger",
        "SMALLINT",
        "String",
        "VARCHAR",
        "Text",
        "TEXT",
        "Unicode",
        "UnicodeText",
        "Boolean",
        "BOOLEAN",
        "Float",
        "REAL",
        "DOUBLE_PRECISION",
        "Numeric",
        "NUMERIC",
        "DECIMAL",
        "Date",
        "DATE",
        "DateTime",
        "TIMESTAMP",
        "LargeBinary",
        "BYTEA",
        "UUID",
        "JSON",
        "JSONB",
        "ARRAY",
    }
)
# Fully-qualified column types that are a wreath type wearing a foreign name.
# `ormar.fields.sqlalchemy_uuid.CHAR` is ormar's own UUID column — the module
# exists only to store a UUID as text on backends without a uuid type — and it
# is how every Alembic revision generated from an ormar model spells a UUID
# primary key, so it is by far the most common column type in a generated
# revision. Reading it as "an unmodelled CHAR" keeps a large share of the
# migrations in Alembic over what is really a `Uuid` column. A plain `sa.CHAR`
# is *not* here: `character(n)` pads, and wreath has no type for that.
_MODELLED_TYPE_ORIGINS = frozenset(
    {
        "ormar.fields.sqlalchemy_uuid.CHAR",
    }
)
# Table-level constraint objects inside a `create_table(...)` that detection
# reads. CheckConstraint/Index/ExcludeConstraint are deliberately absent.
_SA_TABLE_CONSTRAINTS = frozenset(
    {
        "Column",
        "PrimaryKeyConstraint",
        "UniqueConstraint",
        "ForeignKeyConstraint",
    }
)
# Index kwargs that take the index outside "btree over plain columns".
_MIG_INDEX_MANUAL_KWARGS = frozenset(
    {
        "postgresql_where",
        "postgresql_using",
        "postgresql_include",
        "postgresql_ops",
        "postgresql_concurrently",
        "mysql_using",
    }
)
# alter_column kwargs whose whole effect lives in the column signature detect
# reads. `comment=` is absent: wreath does not model column comments.
_MIG_ALTER_KWARGS = frozenset(
    {
        "nullable",
        "type_",
        "server_default",
        "new_column_name",
        "schema",
        "existing_type",
        "existing_nullable",
        "existing_server_default",
    }
)
# Referential actions belong to the constraint, not to a column the ORM declares.
_FK_ACTION_KWARGS = frozenset({"ondelete", "onupdate", "deferrable", "initially"})
