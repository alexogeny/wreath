"""Infrastructure inferred from an application's own declarations.

You cannot infer the infrastructure a Django or a FastAPI application needs,
because it could need anything: Redis, Celery, RabbitMQ, Mongo, a broker, a
cache, any combination, wired any way. Nothing in the framework knows, so
nothing in the framework can say.

You *can* infer it for a wreath application, and the reason is the
no-mandatory-dependency rule rather than anything clever here. Wreath already
owns the answers. The queue is PostgreSQL. So are the message bus, sessions,
rate limits, idempotency, workflows, chunked passes, progress and rooms. Blobs
are `wreath.objects`. Locks are advisory locks. Caches are this process. There
is no broker to discover, because there is no broker.

So `wreath infra infer myapp:app` reads an application and prints what it
requires:

```console
$ wreath infra infer camera_trap.app:app \\
    --settings camera_trap.config:Settings=CAMERA_TRAP \\
    --env example/.env.example
```

Three things make that output worth reading rather than merely plausible.

**It is a derivation, not an analysis.** `app.postgres("main", dsn=...)` built a
`wreath.postgres.Database` that still holds its DSN and its pool sizes;
`app.objects("media", backend="s3", ...)` built a store that still holds its
bucket. Inference walks objects the application constructed at import. It reads
no source, matches no pattern, and imports no cloud SDK -- and it opens no
socket, so it is safe to run anywhere, including against a production target
whose database is not reachable from where you are standing.

**A settings field with no supplier is a gap, reported by name.** That is the
seam this exists to close: an infrastructure definition maintains a list of
environment variables, an application declares a settings type that reads them,
and nothing connects the two. Rename a field and the stack still applies
cleanly; the container starts and dies. `wreath.config.Environment.bind` is the
app-side half of that contract, so the keys it will read are derived here and
checked against whatever is supposed to supply them.

**Absence is stated, never implied.** A subsystem that needs no new
infrastructure is listed saying so. The plan says the queue, the bus and the
rate limiter all live in `main`, rather than printing one database and leaving
you to conclude it. The absence of a broker is the finding nobody believes, so
it is written down.

Both shipped operations are deliberately offline. `infer` emits the plan;
`bundle` combines a gap-free plan with an immutable OCI image digest and
writes a checksummed Compose deployment contract. There is no provider, state
file, image build, or `apply`. A human checks the artifacts before anything
touches an account.

"""

from __future__ import annotations

from .deploy import DeploymentArtifact, DeploymentBundle, deployment_bundle
from .inference import infer
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
    as_dict,
)
from .render import render_json, render_text
from .settings import settings_keys

__all__ = [
    "ConnectionBudget",
    "DatabaseRequirement",
    "DeploymentArtifact",
    "DeploymentBundle",
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
    "deployment_bundle",
    "infer",
    "render_json",
    "render_text",
    "settings_keys",
]
