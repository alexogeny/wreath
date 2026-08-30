"""The `wreath` schema: the tables wreath owns, and how they get there.

Wreath's subsystems -- the job queue, the message bus, sessions, rate limits,
idempotency, the webhook inbox and outbox, the pass ledger, series buckets --
need tables of their own. Those tables are **not** the application's data model
and they do not belong in the application's migration artifact: the artifact
describes what the author declared, and nobody declared a job queue.

So they live in a schema of their own, named `wreath` by default, and this
module puts them there. A framework user who does nothing gets a working
database. A framework user whose role cannot `CREATE SCHEMA` is told exactly
what to hand a DBA, and is refused at startup rather than at the first enqueue.

Three properties hold this together, and each is load-bearing:

* **The user's migration diff cannot see these tables.** The catalog read is
  scoped to one named schema (`migrations.py`), so a diff of the application's
  schema never observes `wreath.*` and never proposes dropping it. Separation is
  by construction, not by a filter someone has to maintain.
* **Every reference is fully schema-qualified.** Nothing here resolves through
  `search_path`, which is what lets it compose with an isolated tenant session
  -- that binds `search_path` to the tenant's schema alone.
* **An upgrade step must be safe for the previous version to keep running
  against.** A fleet mid-rollout has two wreath versions live on one database.
  Steps are therefore additive: add a table, add a nullable or defaulted column,
  add an index. A column stops being read in one release and is dropped two
  releases later, once no supported version reads it. That gap is what makes a
  rollback survivable.

Read `~/code-maps/designs/35-wreath-schema.md` for the reasoning, the rejected
alternatives, and the staging.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ._pgname import quote_identifier

logger = logging.getLogger("wreath")

#: The schema wreath's own tables live in. Every subsystem already defaults to
#: this name; it is repeated here so the bootstrap and the subsystems cannot
#: disagree about where they are looking.
DEFAULT_SCHEMA = "wreath"

#: The applied-migration history lives in `wreath_migrations`, deliberately
#: *not* here. It answers a different question -- what the application applied
#: -- and it must survive a `DROP SCHEMA wreath CASCADE`. Merging them would tie
#: the record of the user's migrations to the lifetime of wreath's furniture.
HISTORY_SCHEMA = "wreath_migrations"


class SchemaError(RuntimeError):
    """A wreath-owned schema is missing, unmanageable, or inconsistent."""


class SchemaNotManaged(SchemaError):
    """A required relation is absent and wreath was told not to create it.

    Raised at startup, never at first use. A subsystem that registers is a
    subsystem that will be used, so deferring the check to the first enqueue
    reproduces exactly the failure this module exists to remove: a missing table
    surfacing as a runtime error, far from the configuration that caused it.
    """


@dataclass(frozen=True, slots=True)
class Step:
    """One ordered, additive change to a component's tables.

    `statements` is a tuple rather than a semicolon-joined string on purpose.
    The driver speaks the extended query protocol exclusively, so it prepares
    every statement and PostgreSQL refuses `cannot insert multiple commands into
    a prepared statement`. Four call sites in this repository had each grown the
    same `sql.split(";\\n")` workaround to get around a `schema_sql()` that
    returned a blob; a tuple of statements removes the workaround rather than
    repeating it a fifth time.

    Args:
        version: The version this step brings the component to. Steps apply in
            ascending order and a component's first step is version 1.
        statements: The statements to execute, in order, each on its own.
    """

    version: int
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError(f"step version must be >= 1, not {self.version}")
        if not self.statements:
            raise ValueError(f"step {self.version} has no statements")


@dataclass(frozen=True, slots=True)
class Component:
    """A subsystem's claim on the wreath schema.

    Args:
        name: Stable identifier, used as the version-marker key and the advisory
            lock key. Renaming one strands its recorded version, so treat it as
            part of the on-disk format.
        steps: Ordered steps. Step 1 creates the component's tables.
        relations: The relations that must exist for the component to work,
            unqualified. Used by `verify` to say precisely what is missing when
            wreath is not managing the schema.
        schema: The schema the component's tables live in.
    """

    name: str
    steps: tuple[Step, ...]
    relations: tuple[str, ...] = ()
    schema: str = DEFAULT_SCHEMA

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("component name must not be empty")
        versions = [step.version for step in self.steps]
        if versions != sorted(set(versions)):
            raise ValueError(
                f"component {self.name!r} steps must be ascending and unique, got {versions}"
            )

    @property
    def target_version(self) -> int:
        """The version a database is brought to once every step has applied."""
        return self.steps[-1].version if self.steps else 0

    def statements(self, *, from_version: int = 0) -> tuple[str, ...]:
        """Every statement of every step above `from_version`, in order."""
        return tuple(
            statement
            for step in self.steps
            if step.version > from_version
            for statement in step.statements
        )

    def sql(self, *, from_version: int = 0, create_schema: bool = True) -> str:
        """The component's DDL as one semicolon-joined script.

        **This is a derivation, not a source.** `Step.statements` holds the DDL;
        this joins it. Every `schema_sql()` in wreath returns this, so the tuple
        and the script cannot disagree -- which they did while `jobs` carried
        both, spelled out twice.

        Args:
            from_version: Emit only steps above this version.
            create_schema: Prepend `CREATE SCHEMA IF NOT EXISTS`. A component
                whose tables are unqualified has no schema to create, and
                emitting one would create a schema nothing then uses.

        Returns:
            SQL, one statement per `;\\n`, with a trailing `;\\n`. Callers that
            execute it must split on `;\\n` -- or better, use `statements()`,
            which is why the tuple exists.
        """
        parts = list(self.statements(from_version=from_version))
        if create_schema and self.qualified:
            parts.insert(0, f"CREATE SCHEMA IF NOT EXISTS {_quote(self.schema)}")
        return "".join(f"{part};\n" for part in parts)

    @property
    def qualified(self) -> bool:
        """Whether this component's tables live in `schema`.

        Not every one does, and the difference is history rather than design.
        Six components name their tables `"wreath"."jobs"`; five predate that
        and use an unqualified `wreath_`-prefixed name, which PostgreSQL
        resolves through `search_path` -- normally `public`. Moving them is a
        *non-additive* change: a worker on the previous version looks for
        `wreath_session` and would not find it, which is precisely what the
        additive rule exists to prevent. So they are registered where their rows
        actually are, and a move is a later, staged concern.
        """
        return self.schema != ""


@runtime_checkable
class SchemaContributor(Protocol):
    """A registered subsystem that declares one wreath-owned schema component."""

    def component(self) -> Component: ...


def _quote(identifier: str) -> str:
    """Quote an identifier, refusing one that cannot be quoted safely.

    Schema and component names reach SQL by interpolation because PostgreSQL has
    no parameter form for an identifier. Guarding the precondition is both the
    correct answer and the cheap one -- there is nothing to catch afterwards.
    """
    return quote_identifier(identifier, reject_quote=True)


def marker_statements(schema: str = DEFAULT_SCHEMA) -> tuple[str, ...]:
    """DDL for the schema and its version marker. Idempotent, applied first.

    Per-component versions, not one global counter: a global number forces
    unrelated subsystems into a single order, so a jobs change and a webhooks
    change collide for no reason and a deployment using neither still has to
    move it.
    """
    name = _quote(schema)
    return (
        f"CREATE SCHEMA IF NOT EXISTS {name}",
        f'CREATE TABLE IF NOT EXISTS {name}."schema_version" (\n'
        "  component text PRIMARY KEY,\n"
        "  version integer NOT NULL,\n"
        "  applied_at timestamptz NOT NULL DEFAULT now()\n"
        ")",
    )


def emit_sql(
    components: Sequence[Component], *, schema: str = DEFAULT_SCHEMA, from_version: int = 0
) -> str:
    """The full ordered DDL, for a DBA to apply by hand.

    This is the supported spelling for a deployment whose application role
    cannot `CREATE SCHEMA`. It is the same statements `bootstrap` would run, in
    the same order, so what a DBA applies and what wreath would have done cannot
    drift.

    Args:
        components: The components to emit, in registration order.
        schema: The schema to emit for.
        from_version: Emit only steps above this version, for a database that is
            already partly upgraded.

    Returns:
        Executable SQL, one statement per `;` on its own line.
    """
    lines = [
        "-- wreath-owned schema. Generated by `wreath schema sql`.",
        "-- Apply as a role with CREATE privilege on this database.",
        "",
    ]
    lines += [f"{statement};" for statement in marker_statements(schema)]
    for component in components:
        pending = [step for step in component.steps if step.version > from_version]
        if not pending:
            continue
        lines.append("")
        lines.append(f"-- {component.name}: to version {component.target_version}")
        for step in pending:
            lines += [f"{statement};" for statement in step.statements]
            lines.append(
                f'INSERT INTO {_quote(schema)}."schema_version" '
                f"(component, version) VALUES ('{component.name}', {step.version}) "
                "ON CONFLICT (component) DO UPDATE SET version = EXCLUDED.version, "
                "applied_at = now();"
            )
    return "\n".join(lines) + "\n"


async def _execute_all(connection: Any, statements: Sequence[str]) -> None:
    for statement in statements:
        await connection.execute(statement)


async def _relation_exists(connection: Any, schema: str, relation: str) -> bool:
    rows = await connection.fetch(
        "SELECT 1 FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1::text AND c.relname = $2::text "
        "AND c.relkind IN ('r', 'p', 'v', 'm')",
        schema,
        relation,
    )
    return bool(rows)


async def _recorded_versions(connection: Any, schema: str) -> dict[str, int]:
    rows = await connection.fetch(
        f'SELECT component, version FROM {_quote(schema)}."schema_version"'
    )
    return {str(row[0]): int(row[1]) for row in rows}


async def _present_in(connection: Any, schema: str) -> set[str]:
    rows = await connection.fetch(
        "SELECT c.relname::text FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1::text AND c.relkind IN ('r', 'p', 'v', 'm')",
        schema,
    )
    return {str(row[0]) for row in rows}


async def _resolves(connection: Any, relation: str) -> bool:
    """Whether an *unqualified* relation resolves through `search_path`.

    The qualified components can be answered with one catalog read per schema.
    An unqualified one cannot: which schema it lands in is a property of the
    session, so the only honest question is the one PostgreSQL itself answers.
    `to_regclass` returns NULL rather than raising for a name that does not
    resolve, which is why it is the right call here -- a `::regclass` cast
    would make "absent" an error to catch instead of a value to read.
    """
    rows = await connection.fetch("SELECT to_regclass($1::text) IS NOT NULL", relation)
    return bool(rows and rows[0][0])


async def _missing_relations(
    connection: Any, schema: str, components: Sequence[Component]
) -> dict[str, tuple[str, ...]]:
    """Which of each component's relations are absent, by component name.

    Two lookups, because wreath's tables are in two places. Six components name
    theirs `"wreath"."jobs"`; five predate that convention and use an
    unqualified `wreath_`-prefixed name that `search_path` resolves. Asking the
    catalog for `wreath.wreath_session` would report every one of those missing
    on a correctly configured database -- a refusal with nothing behind it,
    which is worse than no check at all.
    """
    missing: dict[str, tuple[str, ...]] = {}
    present: dict[str, set[str]] = {}
    for component in components:
        if not component.relations:
            continue
        if component.qualified:
            if component.schema not in present:
                present[component.schema] = await _present_in(connection, component.schema)
            absent = tuple(
                relation
                for relation in component.relations
                if relation not in present[component.schema]
            )
        else:
            unresolved = []
            for relation in component.relations:
                if not await _resolves(connection, relation):
                    unresolved.append(relation)
            absent = tuple(unresolved)
        if absent:
            missing[component.name] = absent
    return missing


def _refusal(component: Component, relations: Sequence[str]) -> str:
    if component.qualified:
        listed = ", ".join(f'"{component.schema}"."{relation}"' for relation in relations)
        where = f'as a role with CREATE privilege on schema "{component.schema}"'
    else:
        listed = ", ".join(relations)
        where = (
            "as a role with CREATE privilege on the schema your search_path "
            "resolves to (these tables are unqualified)"
        )
    plural = ("relations", "do") if len(relations) > 1 else ("relation", "does")
    return (
        f"{component.name} needs {plural[0]} {listed}, which {plural[1]} not exist.\n"
        "This application was configured with manage_schema=False, so wreath did "
        "not create it. Emit the DDL with:\n\n"
        f"    wreath schema sql --component {component.name}\n\n"
        f"Apply it {where}, then verify with `wreath schema check`. To let wreath "
        "manage its own schema instead, remove manage_schema=False."
    )


async def verify(
    database: Any,
    components: Sequence[Component],
    *,
    schema: str = DEFAULT_SCHEMA,
    workload: str = "write",
) -> None:
    """Refuse if any component's relations are absent. Never creates anything.

    Raises:
        SchemaNotManaged: A required relation is missing. The message names the
            component, the relation, and the command that emits the DDL.
    """
    connection = await database.acquire(workload)
    try:
        missing = await _missing_relations(connection, schema, components)
    finally:
        await database.release(workload, connection)
    if missing:
        name, relations = next(iter(missing.items()))
        component = next(candidate for candidate in components if candidate.name == name)
        raise SchemaNotManaged(_refusal(component, relations))


async def inspect_components(
    database: Any,
    components: Sequence[Component],
    *,
    schema: str = DEFAULT_SCHEMA,
    workload: str = "write",
) -> list[dict[str, Any]]:
    """What the catalog says about each component. Reads only; creates nothing.

    Behind `wreath schema check`, and deliberately reporting **both** the
    recorded version and the relations, because they can disagree in either
    direction: a database whose DDL was applied by hand has the relations and no
    marker, and a marker without its relations means somebody dropped a table
    under a running deployment. Trusting the marker alone would report the first
    as broken and the second as healthy, which is exactly backwards.
    """
    connection = await database.acquire(workload)
    try:
        marker = await _relation_exists(connection, schema, "schema_version")
        recorded = await _recorded_versions(connection, schema) if marker else {}
        missing = await _missing_relations(connection, schema, components)
    finally:
        await database.release(workload, connection)
    return [
        {
            "component": component.name,
            "schema": component.schema or "(search_path)",
            "recorded": recorded.get(component.name, 0),
            "expected": component.target_version,
            "missing": list(missing.get(component.name, ())),
        }
        for component in components
    ]


async def bootstrap(
    database: Any,
    components: Sequence[Component],
    *,
    schema: str = DEFAULT_SCHEMA,
    workload: str = "write",
    manage: bool = True,
) -> dict[str, int]:
    """Bring the wreath schema up to date, or refuse and say what is missing.

    Idempotent and safe to run concurrently: the work for each component happens
    under an advisory lock keyed on that component, so a fleet of workers
    starting together serialises rather than racing. Locking per component rather
    than globally means an unrelated subsystem's upgrade does not block a
    worker that does not use it.

    Each step applies in its own transaction together with the version row that
    records it, so a crash mid-bootstrap leaves a database that describes itself
    accurately rather than one whose marker overstates what landed.

    Args:
        database: A started `wreath.postgres.Database`.
        components: The components to bring up to date.
        schema: The schema to manage.
        workload: The pool to use. Must be a primary; DDL cannot run on a replica.
        manage: When False, create nothing and `verify` instead.

    Returns:
        The version of each component after the run.

    Raises:
        SchemaNotManaged: `manage` is False and a required relation is absent.
    """
    if not manage:
        await verify(database, components, schema=schema, workload=workload)
        connection = await database.acquire(workload)
        try:
            # Guard rather than catch: a deployment that applied the DDL by hand
            # has the marker, one mid-adoption has the relations but not the
            # marker, and reporting "unknown" beats failing a start that
            # `verify` has just allowed. Asking the catalog is both the correct
            # question and cheaper than raising through a missing relation.
            if not await _relation_exists(connection, schema, "schema_version"):
                logger.info(
                    "wreath schema %r has the relations but no version marker; "
                    "reporting versions as unknown",
                    schema,
                )
                return {}
            return await _recorded_versions(connection, schema)
        finally:
            await database.release(workload, connection)

    applied: dict[str, int] = {}
    for component in components:
        async with database.lock(f"wreath.schema:{component.name}", workload=workload):
            connection = await database.acquire(workload)
            try:
                await _execute_all(connection, marker_statements(schema))
                # The marker lives in one schema; a component's tables may not.
                # A job runner configured with `schema="tenant_ops"` puts its
                # tables there, and applying its DDL before that schema exists
                # fails with `schema "tenant_ops" does not exist` -- a confusing
                # error for a bootstrap whose whole job is creating things.
                if component.qualified and component.schema != schema:
                    await _execute_all(
                        connection,
                        (f"CREATE SCHEMA IF NOT EXISTS {_quote(component.schema)}",),
                    )
                recorded = (await _recorded_versions(connection, schema)).get(component.name, 0)
                if recorded > component.target_version:
                    logger.warning(
                        "wreath schema component %r is at version %d but this "
                        "build targets %d; steps are additive so this build will "
                        "run, but a gap this way round means a newer wreath has "
                        "already upgraded it",
                        component.name,
                        recorded,
                        component.target_version,
                    )
                for step in component.steps:
                    if step.version <= recorded:
                        continue
                    # Deliberately not wrapped in an explicit transaction, and
                    # correctness does not need one: every statement is written
                    # `IF NOT EXISTS`, so a crash between the last statement and
                    # the marker leaves a database that simply re-applies the
                    # step harmlessly on the next start. The marker is a fast
                    # path -- it lets a current database skip the DDL -- not the
                    # source of truth, which is the catalog itself.
                    # That idempotence is therefore a *requirement* on a step,
                    # not a convenience, and it is why `verify` reads relations
                    # from the catalog rather than trusting the marker.
                    # (The pure driver also refuses `async with
                    # connection.transaction()` on a pooled connection outright
                    # -- see the report accompanying this branch -- so a
                    # transaction here would not have been available anyway.)
                    await _execute_all(connection, step.statements)
                    await connection.execute(
                        f'INSERT INTO {_quote(schema)}."schema_version" '
                        "(component, version) VALUES ($1, $2) "
                        "ON CONFLICT (component) DO UPDATE SET "
                        "version = EXCLUDED.version, applied_at = now()",
                        component.name,
                        step.version,
                    )
                    recorded = step.version
                applied[component.name] = recorded
            finally:
                await database.release(workload, connection)
    return applied


__all__ = [
    "DEFAULT_SCHEMA",
    "HISTORY_SCHEMA",
    "Component",
    "SchemaContributor",
    "SchemaError",
    "SchemaNotManaged",
    "Step",
    "bootstrap",
    "emit_sql",
    "marker_statements",
    "verify",
]
