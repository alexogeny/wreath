"""The typed plan `wreath.infra.infer` returns.

Every type here is frozen and holds only data: no method reaches back into the
application, opens a connection, or reads the environment. That separation is
deliberate -- the plan is meant to be rendered, compared, serialised, and read
by a person before anything touches an account, so it must be inert once built.

The vocabulary is small on purpose. Stage 1 does not model cloud resources; it
models *what an application requires*, in the words the application already
used. A database is a database, not an `aws_db_instance`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "ConnectionBudget",
    "DatabaseRequirement",
    "EgressRequirement",
    "Gap",
    "GapKind",
    "InfrastructurePlan",
    "ListenerRequirement",
    "ObjectStoreRequirement",
    "Presence",
    "SchemaComponent",
    "SettingsContract",
    "SettingsKey",
    "SharedSubsystem",
    "WorkloadPool",
    "as_dict",
]


class Presence(StrEnum):
    """Whether a subsystem was found, absent, or simply cannot be seen.

    The third value is the honest one and the reason this is not a boolean. Some
    subsystems register themselves on the application (`app.jobs(...)`), and for
    those a plan can say `ABSENT` and mean it. Others are constructed by the
    application author and handed straight to a handler -- a `RoomRegistry`, a
    `Workflow` -- so the application object never learns they exist. Reporting
    those as absent would be a false negative dressed as a finding.
    """

    DECLARED = "declared"
    ABSENT = "absent"
    UNOBSERVABLE = "unobservable"


class GapKind(StrEnum):
    """What kind of hole in the deployment a `Gap` names."""

    #: A settings field whose environment key nothing supplies.
    SETTINGS_KEY = "settings-key"
    #: An environment key a supplier offers that no settings field reads.
    UNREAD_KEY = "unread-key"
    #: A pool that cannot serve a request because long-lived holders fill it.
    CAPACITY = "capacity"
    #: Something the application declares that this stage cannot derive.
    UNDERIVABLE = "underivable"


@dataclass(frozen=True, slots=True)
class WorkloadPool:
    """One `wreath.postgres` workload pool, as the application configured it."""

    workload: str
    min_size: int
    max_size: int
    #: The endpoint this workload connects to. Normally the database's own; a
    #: `workload_dsns={"read": ...}` entry sends reads to a replica instead, and
    #: that is a second thing to provision rather than a setting.
    endpoint: str


@dataclass(frozen=True, slots=True)
class ConnectionBudget:
    """How many backends one process wants from one endpoint, and who holds them.

    `held` counts connections taken at startup and never given back for the life
    of the process: every `LISTEN` doorbell acquires from a workload pool and
    keeps its connection, so a pool sized without allowing for them is a pool
    that starves. `available` is what is left for requests.
    """

    endpoint: str
    workload: str
    pool_max: int
    held: int
    holders: tuple[str, ...]

    @property
    def available(self) -> int:
        return self.pool_max - self.held


@dataclass(frozen=True, slots=True)
class SchemaComponent:
    """One subsystem's tables, and the database they belong in."""

    name: str
    schema: str
    relations: tuple[str, ...]
    declared_by: str


@dataclass(frozen=True, slots=True)
class DatabaseRequirement:
    """One PostgreSQL database an application registered with `app.postgres`."""

    name: str
    endpoint: str
    database: str
    user: str | None
    pools: tuple[WorkloadPool, ...]
    budgets: tuple[ConnectionBudget, ...]
    schemas: tuple[str, ...]
    extensions: tuple[str, ...]
    #: ORM models compiled against this database. Their tables come from
    #: `wreath migrations`, not from anything this plan can create.
    models: int
    components: tuple[SchemaComponent, ...]


@dataclass(frozen=True, slots=True)
class ObjectStoreRequirement:
    """One `app.objects(...)` registration and what it implies."""

    name: str
    backend: str
    #: `local` only: the directory that must exist and survive a restart.
    root: str | None = None
    bucket: str | None = None
    region: str | None = None
    #: The host requests are signed for. For AWS this is the virtual-hosted
    #: bucket name; for MinIO/R2 it is the endpoint the application was given.
    host: str | None = None
    path_style: bool = False
    #: Where the credentials came from at registration: the process environment
    #: or an explicit argument. Only ever a source, never a value.
    credentials: str | None = None
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EgressRequirement:
    """One outbound origin the application pinned a client to.

    An egress rule is a firewall's business, and this is the declaration it
    should be generated from: `HTTPClient` fixes its origin at construction and
    `DestinationPolicy` states which schemes, hosts and ports it will ever
    resolve to. Both are read here rather than guessed from call sites.
    """

    name: str
    origin: str
    base_path: str
    max_connections: int
    #: `DestinationPolicy`'s allowance, rendered. Never absent: a client always
    #: carries a policy, and the default one already denies every private,
    #: loopback and link-local address.
    destination: str
    declared_by: str


@dataclass(frozen=True, slots=True)
class ListenerRequirement:
    """The application's own inbound surface.

    One listener, always, because a wreath application is one ASGI callable.
    Whether it sits behind a load balancer, and on which port, is a deployment
    decision the application does not make and this plan does not invent.
    """

    protocol: str
    routes: int
    websocket_routes: int
    methods: tuple[str, ...]
    mounts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SharedSubsystem:
    """A subsystem that would be a separate service in another framework.

    This is the row a reader will not believe, so every one is listed whether or
    not the application uses it -- an omission would read as an oversight rather
    than as the claim it is. `backing` is where the state actually lives.
    """

    name: str
    module: str
    instead_of: tuple[str, ...]
    backing: str
    presence: Presence
    detail: str


@dataclass(frozen=True, slots=True)
class SettingsKey:
    """One environment key a settings model reads, and whether anything sets it."""

    field: str
    key: str
    annotation: str
    required: bool
    secret: bool
    #: The label of whatever supplies this key -- a dotenv path, `"process"`, or
    #: `"default"` when the field carries its own. `None` is the gap.
    supplied_by: str | None


@dataclass(frozen=True, slots=True)
class SettingsContract:
    """The contract between one settings dataclass and a deployment's environment.

    `wreath.config.Environment.bind` is the app-side half: it turns field names
    into environment keys by a fixed rule. This records the keys that rule
    produces, so the other half -- whatever supplies them -- can be checked
    against it instead of maintained beside it.
    """

    model: str
    prefix: str
    keys: tuple[SettingsKey, ...]
    #: Keys an explicitly named supplier offers that no field reads. Computed
    #: only for authored dotenv files, never for the process environment, where
    #: hundreds of unrelated keys would drown the signal.
    unread: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Gap:
    """Something the deployment must resolve, named precisely enough to act on."""

    kind: GapKind
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class InfrastructurePlan:
    """Everything one application requires, derived from its own declarations."""

    application: str
    databases: tuple[DatabaseRequirement, ...] = ()
    object_stores: tuple[ObjectStoreRequirement, ...] = ()
    egress: tuple[EgressRequirement, ...] = ()
    listeners: tuple[ListenerRequirement, ...] = ()
    subsystems: tuple[SharedSubsystem, ...] = ()
    settings: tuple[SettingsContract, ...] = ()
    gaps: tuple[Gap, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def as_dict(plan: InfrastructurePlan) -> dict[str, object]:
    """The plan as plain JSON-compatible data, for `--format json`.

    `dataclasses.asdict` on its own drops `ConnectionBudget.available`, because
    a property is not a field; it is re-added here rather than stored so the two
    renderings cannot disagree about arithmetic.
    """
    data = dataclasses.asdict(plan)
    for database, source in zip(data["databases"], plan.databases, strict=True):
        for budget, origin in zip(database["budgets"], source.budgets, strict=True):
            budget["available"] = origin.available
    return data
