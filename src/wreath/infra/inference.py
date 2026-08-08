"""Derive an infrastructure plan from an application's own declarations.

Nothing here guesses. Every value in the returned plan is read off an object the
application constructed at import: `app.postgres(...)` built a
`wreath.postgres.Database` and it still holds its DSN and its pool sizes;
`app.objects(...)` built a store and it still holds its bucket or its root;
`app.http_client(...)` built a client and it still holds the origin it is pinned
to. Inference is a walk over those, not an analysis of anything.

**Nothing here connects to anything either.** No socket is opened, no DSN is
resolved, no bucket is listed, and no cloud SDK is imported -- the plan is
derived entirely from objects already in memory. `tests/test_infra_infer.py`
asserts both of those directly rather than trusting this paragraph.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from ..app import walk_claims
from .model import (
    ConnectionBudget,
    DatabaseRequirement,
    EgressRequirement,
    Gap,
    GapKind,
    InfrastructurePlan,
    ListenerRequirement,
    ObjectStoreRequirement,
    Presence,
    SchemaComponent,
    SettingsContract,
    SettingsKey,
    SharedSubsystem,
    WorkloadPool,
)
from .settings import settings_keys

__all__ = ["infer"]


#: Every subsystem wreath answers in PostgreSQL, in-process, or on disk that a
#: comparable stack answers with a separate service to provision.
#:
#: The table is a constant rather than a derivation because its *whole* content
#: is the finding. A plan that listed only what an application happened to
#: declare would say nothing at all about the broker it does not need, and the
#: absent broker is the claim a reader has come to check.
_SUBSYSTEM_CATALOGUE: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("durable jobs", "wreath.jobs", ("Celery", "RQ", "Redis", "RabbitMQ"), "PostgreSQL"),
    ("message bus", "wreath.messaging", ("Kafka", "RabbitMQ", "SNS + SQS"), "PostgreSQL"),
    ("webhook inbox/outbox", "wreath.webhooks", ("SQS", "Celery"), "PostgreSQL"),
    ("sessions", "wreath.session_store", ("Redis", "Memcached"), "PostgreSQL"),
    ("rate limits", "wreath.policy.ratelimit", ("Redis",), "PostgreSQL"),
    ("idempotency", "wreath.middleware.idempotency", ("Redis",), "PostgreSQL"),
    ("workflows", "wreath.workflows", ("Temporal", "Airflow"), "PostgreSQL"),
    ("chunked passes", "wreath.passes", ("Celery", "a bespoke backfill script"), "PostgreSQL"),
    ("progress", "wreath.progress", ("Redis pub/sub",), "PostgreSQL LISTEN/NOTIFY"),
    ("rooms", "wreath.rooms", ("Redis pub/sub",), "PostgreSQL LISTEN/NOTIFY"),
    ("calculated views", "wreath.series", ("a warehouse", "dbt"), "PostgreSQL"),
    (
        "distributed locks", "wreath.locks", ("Redis", "etcd", "ZooKeeper"),
        "PostgreSQL advisory locks",
    ),
    ("application cache", "wreath.cache", ("Redis", "Memcached"), "in-process memory"),
    ("response cache", "wreath.response_cache", ("Varnish", "Redis"), "in-process memory"),
)

#: The three subsystems that reach an application as *middleware* rather than as
#: a registration, keyed by the schema component their store claims. There is no
#: registry to walk for them: the middleware holds the store and the store holds
#: the tables, so the component name is the only thing that identifies which
#: subsystem a claim belongs to.
_STORE_COMPONENTS: Mapping[str, str] = {
    "session": "wreath.session_store",
    "ratelimit": "wreath.policy.ratelimit",
    "idempotency": "wreath.middleware.idempotency",
}

#: Subsystems the application object cannot see, and why. Each is a declaration
#: this stage would use if it could reach it; see `docs/guides/infra.md`.
_UNOBSERVABLE: Mapping[str, str] = {
    "wreath.workflows": (
        "a Workflow is constructed with a store and handed to a handler; nothing "
        "registers it on the application"
    ),
    "wreath.rooms": (
        "a RoomRegistry is constructed over a message bus and held by the "
        "application author, not by the application"
    ),
    "wreath.series": (
        "a Series is a declared class queried through a session; it registers "
        "nothing on the application"
    ),
    "wreath.locks": (
        "advisory locks are taken per call site, so there is no declaration to read"
    ),
    "wreath.cache": ("@cached decorates a function; the application never sees it"),
    "wreath.response_cache": (
        "response caching is a route decoration, not an application registration"
    ),
}


def _endpoint(dsn: str) -> tuple[str, str, str | None]:
    """`(host:port, database, user)` from a DSN, with the password dropped.

    A password is dropped rather than masked. A plan is written to be pasted
    into a review, a ticket, or a docs page, and `***` in that position invites
    someone to paste the unmasked one beside it for comparison.
    """
    parts = urlsplit(dsn)
    host = parts.hostname or "localhost"
    port = parts.port or 5432
    database = parts.path.lstrip("/")
    return f"{host}:{port}", database, parts.username


def _doorbell_holders(app: Any, database: Any) -> list[tuple[str, str]]:
    """`(workload, description)` for every connection held for the process life.

    A `wreath._doorbell.Doorbell` acquires from a workload pool and never gives
    the connection back, so each one permanently removes a slot from that pool.
    Job runners and message buses each hold exactly one.
    """
    holders: list[tuple[str, str]] = []
    for name, runner in app._job_runners.items():
        if getattr(runner, "_db", None) is database:
            holders.append((runner._workload, f"jobs runner {name!r} LISTEN doorbell"))
    for name, bus in app._message_buses.items():
        if getattr(bus, "_db", None) is database:
            holders.append((bus._workload, f"message bus {name!r} LISTEN doorbell"))
    return holders


def _declared_by(app: Any, holder: Any) -> str:
    """How the application came to hold `holder`, for the plan to quote.

    A label, not a lookup key: the plan says where a table came from instead of
    only that it exists. Middleware is named by its class, because it was handed
    to `add_middleware` rather than produced by a named declaration.
    """
    for registry, call, keyword in (
        (app._job_runners, "app.jobs", ""),
        (app._message_buses, "app.messaging", ""),
        (app._webhook_hubs, "app.webhooks", ""),
        (app._entity_registries, "app.entities", ""),
        # Keyed by the database it settles on rather than by a name of its own,
        # because one pair of tables serves every sealed view on a database.
        (app._series_stores, "app.series", "database="),
    ):
        for name, held in registry.items():
            if held is holder:
                return f"{call}({keyword}{name!r})"
    return type(holder).__name__


def _component_owners(app: Any) -> list[tuple[Any, Any, str]]:
    """`(database, component, declared_by)` for every subsystem that owns tables.

    The holder walk is `wreath.app.walk_claims`, shared with
    `Wreath.schema_components`, and the attribution is `Wreath._schema_database`.
    Inference used to own a third copy of the walk plus a `_held_database` that
    guessed among `_db`, `_database` and `database`, because the subsystems had
    never been made to agree on a name. Both are gone: an owner now *says* which
    database it belongs to, either because the application recorded the
    declaration that built it or because it answers `schema_database`, so there
    is nothing left to guess at and no fourth name to miss when one is added.

    The single-database fallback is kept only for owners that genuinely hold no
    database -- a webhook hub is handed a session per call and never sees one.
    A claim that still cannot be attributed comes back with `None` rather than
    being dropped: those tables have to exist somewhere, and an application with
    two databases and a webhook inbox is a real, unresolved question rather than
    an empty answer. `_database_requirements` turns it into a gap.
    """
    databases = list(app._databases.values())
    only = databases[0] if len(databases) == 1 else None
    found: list[tuple[Any, Any, str]] = []
    seen: set[str] = set()
    for holder, candidate, component in walk_claims(app.schema_holders()):
        if component.name in seen:
            continue
        seen.add(component.name)
        database = app._schema_database(holder, candidate) or only
        found.append((database, component, _declared_by(app, holder)))
    return found


def _unattributed(app: Any, owners: list[tuple[Any, Any, str]]) -> list[Gap]:
    """Tables nobody can say which database they belong in.

    Only ever a gap when the application registers more than one database. With
    none at all the question is vacuous -- there is no database in which the
    table could be missing -- and with exactly one there is nothing to decide.
    """
    known = ", ".join(sorted(app._databases))
    return [
        Gap(
            kind=GapKind.UNDERIVABLE,
            subject=f"{component.name} tables",
            detail=(
                f"{declared} is handed a session per call rather than a database, "
                f"and this application registers {len(app._databases)} ({known}), so "
                "nothing says which one holds these tables. Wreath's own startup "
                f"refuses here too; `wreath schema sql --component {component.name}` "
                "emits the DDL"
            ),
        )
        for database, component, declared in owners
        if database is None and app._databases
    ]


def _database_requirements(app: Any) -> tuple[tuple[DatabaseRequirement, ...], list[Gap]]:
    owners = _component_owners(app)
    gaps: list[Gap] = _unattributed(app, owners)
    requirements: list[DatabaseRequirement] = []
    for name, database in app._databases.items():
        endpoint, dbname, user = _endpoint(database._dsn)
        pools: list[WorkloadPool] = []
        for workload, config in database._configs.items():
            own = database._workload_dsns.get(workload)
            where = _endpoint(own)[0] if own else endpoint
            pools.append(
                WorkloadPool(
                    workload=workload,
                    min_size=config.min_size,
                    max_size=config.max_size,
                    endpoint=where,
                )
            )
        holders = _doorbell_holders(app, database)
        budgets: list[ConnectionBudget] = []
        for pool in pools:
            held = tuple(what for workload, what in holders if workload == pool.workload)
            budget = ConnectionBudget(
                endpoint=pool.endpoint,
                workload=pool.workload,
                pool_max=pool.max_size,
                held=len(held),
                holders=held,
            )
            budgets.append(budget)
            if budget.available < 1:
                gaps.append(
                    Gap(
                        kind=GapKind.CAPACITY,
                        subject=f"{name}.{pool.workload} pool",
                        detail=(
                            f"max_size={pool.max_size} and {budget.held} connection(s) "
                            f"are held for the life of the process ("
                            f"{', '.join(held)}), leaving {budget.available} for "
                            "requests"
                        ),
                    )
                )
        components = tuple(
            SchemaComponent(
                name=component.name,
                schema=component.schema,
                relations=tuple(component.relations),
                declared_by=declared,
            )
            for owned, component, declared in owners
            if owned is database
        )
        registry = app._orm_registries.get(name)
        models, extensions, schemas = _orm_facts(registry)
        requirements.append(
            DatabaseRequirement(
                name=name,
                endpoint=endpoint,
                database=dbname,
                user=user,
                pools=tuple(pools),
                budgets=tuple(budgets),
                schemas=tuple(sorted({*schemas, *(c.schema for c in components)})),
                extensions=extensions,
                models=models,
                components=components,
            )
        )
    return tuple(requirements), gaps


def _orm_facts(registry: Any) -> tuple[int, tuple[str, ...], set[str]]:
    """Model count, required extensions, and schemas, from a compiled registry."""
    if registry is None:
        return 0, (), set()
    from ..orm.introspection import declared_extension_columns

    specs = tuple(registry.specs)
    extensions = {
        column.pg_type.extension
        for _spec, column in declared_extension_columns(registry)
    }
    schemas = {spec.schema for spec in specs if getattr(spec, "schema", None)}
    return len(specs), tuple(sorted(extensions)), schemas


def _object_stores(app: Any) -> tuple[ObjectStoreRequirement, ...]:
    stores: list[ObjectStoreRequirement] = []
    for name, store in app._object_stores.items():
        # `app.objects` builds exactly two kinds, and only one has a bucket.
        bucket = getattr(store, "_bucket", None)
        if bucket is None:
            stores.append(
                ObjectStoreRequirement(
                    name=name,
                    backend="local",
                    root=store._root,
                    requires=(
                        "a writable directory that survives a restart -- a volume, "
                        "not a container layer",
                    ),
                )
            )
            continue
        stores.append(
            ObjectStoreRequirement(
                name=name,
                backend="s3",
                bucket=bucket,
                region=getattr(store, "_region", None),
                host=getattr(store, "_host", None),
                path_style=bool(getattr(store, "_path_style", False)),
                # The store keeps the resolved key, never where it came from,
                # so this names both sources rather than picking one.
                credentials=(
                    "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, unless app.objects "
                    "was passed access_key= and secret_key= directly"
                ),
                requires=(
                    "a bucket",
                    "a role or key permitted to GET/PUT/DELETE and to start, "
                    "complete and abort multipart uploads on it",
                    "a lifecycle rule that reaps incomplete multipart uploads",
                    "egress to the bucket host",
                ),
            )
        )
    return tuple(stores)


def _destination(policy: Any) -> str:
    """A `DestinationPolicy` as the egress rule it already is.

    Never optional: `HTTPClient` defaults the argument rather than allowing
    `None`, so every registered client has stated where it may connect.
    """
    parts = [f"schemes {'/'.join(sorted(policy.schemes))}"]
    if policy.hosts:
        parts.append(f"hosts {', '.join(policy.hosts)}")
    if policy.ports:
        parts.append(f"ports {', '.join(str(port) for port in sorted(policy.ports))}")
    allowed = [
        label
        for label, flag in (
            ("private", policy.allow_private),
            ("loopback", policy.allow_loopback),
            ("link-local", policy.allow_link_local),
        )
        if flag
    ]
    parts.append(f"also permits {', '.join(allowed)}" if allowed else "global addresses only")
    return "; ".join(parts)


def _egress(app: Any) -> tuple[EgressRequirement, ...]:
    found: list[EgressRequirement] = []
    for key, client in app._http_clients.items():
        if key.startswith("__objects_"):
            declared = f"app.objects({key.removeprefix('__objects_')!r}, backend='s3')"
            name = key.removeprefix("__")
        else:
            declared = f"app.http_client({key!r})"
            name = key
        host = client._host
        port = client._port
        scheme = client._scheme
        default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        origin = f"{scheme}://{host}" if default else f"{scheme}://{host}:{port}"
        found.append(
            EgressRequirement(
                name=name,
                origin=origin,
                base_path=client._base_path,
                max_connections=client._limits.max_connections,
                destination=_destination(client._destination),
                declared_by=declared,
            )
        )
    return tuple(found)


def _listener(app: Any) -> ListenerRequirement:
    methods = {method for definition in app._routes for method in definition.methods}
    return ListenerRequirement(
        protocol="http",
        routes=len(app._routes),
        websocket_routes=len(app._ws_routes),
        methods=tuple(sorted(methods)),
        mounts=tuple(sorted(app._mount_names)),
    )


def _subsystems(app: Any) -> tuple[SharedSubsystem, ...]:
    detail = _observed(app)
    rows: list[SharedSubsystem] = []
    for name, module, instead_of, backing in _SUBSYSTEM_CATALOGUE:
        if module in detail:
            presence, text = Presence.DECLARED, detail[module]
        elif module in _UNOBSERVABLE:
            presence, text = Presence.UNOBSERVABLE, _UNOBSERVABLE[module]
        else:
            presence, text = Presence.ABSENT, "not registered on this application"
        rows.append(
            SharedSubsystem(
                name=name,
                module=module,
                instead_of=instead_of,
                backing=backing,
                presence=presence,
                detail=text,
            )
        )
    return tuple(rows)


def _observed(app: Any) -> dict[str, str]:
    """The subsystems this application registered, and where each one lives."""
    seen: dict[str, list[str]] = {}

    def note(module: str, where: str) -> None:
        seen.setdefault(module, []).append(where)

    for name, runner in app._job_runners.items():
        note("wreath.jobs", f"runner {name!r} on {_database_name(app, runner)}")
        if runner.progress is not None:
            note("wreath.progress", f"watched queue {name!r}")
        if getattr(runner, "_passes", None):
            note("wreath.passes", f"driven by runner {name!r}")
    for name, bus in app._message_buses.items():
        note("wreath.messaging", f"bus {name!r} on {_database_name(app, bus)}")
    for name in app._webhook_hubs:
        note("wreath.webhooks", f"hub {name!r}")
    owners = _component_owners(app)
    for claimed, module in _STORE_COMPONENTS.items():
        for database, component, declared in owners:
            if component.name == claimed:
                note(module, f"{declared} on {_named(app, database)}")
    return {module: "; ".join(where) for module, where in seen.items()}


def _database_name(app: Any, holder: Any) -> str:
    return _named(app, getattr(holder, "_db", None))


def _named(app: Any, database: Any) -> str:
    for name, registered in app._databases.items():
        if registered is database:
            return f"database {name!r}"
    return "an unregistered database"


def _settings(
    models: Iterable[tuple[type, str, str]],
    supplied: Mapping[str, str],
    dotenv_keys: Mapping[str, str],
) -> tuple[tuple[SettingsContract, ...], list[Gap]]:
    contracts: list[SettingsContract] = []
    gaps: list[Gap] = []
    read: set[str] = set()
    for model, label, prefix in models:
        keys: list[SettingsKey] = []
        for derived in settings_keys(model, prefix=prefix):
            read.add(derived.key)
            source = supplied.get(derived.key)
            if source is None and not derived.required:
                source = "default"
            keys.append(
                SettingsKey(
                    field=derived.field,
                    key=derived.key,
                    annotation=derived.annotation,
                    required=derived.required,
                    secret=derived.secret,
                    supplied_by=source,
                )
            )
            if source is None:
                gaps.append(
                    Gap(
                        kind=GapKind.SETTINGS_KEY,
                        subject=derived.key,
                        detail=(
                            f"{label} requires {derived.field} ({derived.annotation}) "
                            f"and nothing supplies {derived.key}"
                        ),
                    )
                )
        contracts.append(
            SettingsContract(model=label, prefix=prefix, keys=tuple(keys), unread=())
        )
    # Without a contract nothing is known to read anything, so every key would
    # be "unread" and the report would be noise. An empty `unread` then adds
    # nothing below, which is why the emptiness is not tested for separately.
    unread = tuple(sorted(key for key in dotenv_keys if key not in read))
    if contracts:
        last = contracts[-1]
        contracts[-1] = SettingsContract(
            model=last.model, prefix=last.prefix, keys=last.keys, unread=unread
        )
        for key in unread:
            gaps.append(
                Gap(
                    kind=GapKind.UNREAD_KEY,
                    subject=key,
                    detail=(
                        f"{dotenv_keys[key]} supplies {key}, and no settings field "
                        "reads it"
                    ),
                )
            )
    return tuple(contracts), gaps


def infer(
    app: Any,
    *,
    application: str,
    settings: Iterable[tuple[type, str, str]] = (),
    supplied: Mapping[str, str] | None = None,
    dotenv_keys: Mapping[str, str] | None = None,
) -> InfrastructurePlan:
    """Read one built `wreath.app.Wreath` and return what it requires.

    The application must already be constructed -- inference reads the objects
    its declarations built, so a factory has to have been called first. Nothing
    is started: no pool opens, no lifespan runs, no socket is created.

    Args:
        app: a built application.
        application: how the application was named on the command line, for the
            plan's header.
        settings: `(dataclass, label, prefix)` for each settings model whose
            environment contract should be checked. Empty means the contract is
            not checked at all, and a note says so -- an unchecked contract must
            not read as a satisfied one.
        supplied: environment key to the label of whatever supplies it.
        dotenv_keys: the subset of `supplied` that came from an authored dotenv
            file, which is the only source worth reporting unread keys against.

    Returns:
        The plan. Read it, then decide; nothing here decides anything.

    Raises:
        TypeError: `app` is not a built wreath application.
    """
    if not hasattr(app, "_databases") or not hasattr(app, "schema_components"):
        raise TypeError(
            f"{application} is not a built wreath application; "
            "`wreath infra infer` reads the declarations a `Wreath` object holds"
        )
    databases, gaps = _database_requirements(app)
    contracts, settings_gaps = _settings(
        settings, supplied or {}, dotenv_keys or {}
    )
    notes: list[str] = []
    if not contracts:
        notes.append(
            "No settings model was named, so the environment contract is unchecked. "
            "Pass --settings module:Class[=PREFIX]; a field with no supplier is the "
            "gap this command exists to find."
        )
    if not supplied:
        notes.append(
            "No environment supplier was named, so every required key reads as a gap. "
            "Pass --env PATH for a dotenv file, or --environ to use this process's "
            "own environment."
        )
    notes.append(
        "Telemetry sinks are not derivable from an application: TelemetryConfig is a "
        "field of wreath.server.ServerConfig, not of Wreath, so a sink is a property "
        "of how the application is served rather than of the application."
    )
    return InfrastructurePlan(
        application=application,
        databases=databases,
        object_stores=_object_stores(app),
        egress=_egress(app),
        listeners=(_listener(app),),
        subsystems=_subsystems(app),
        settings=contracts,
        gaps=tuple([*gaps, *settings_gaps]),
        notes=tuple(notes),
    )
