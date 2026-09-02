"""Erasure, retention and subject access, over the graph the ORM already has.

A right-to-erasure request has a one-month clock under GDPR Article 17 and a
45-day one under CCPA/CPRA, and the European Data Protection Board's February
2026 coordinated enforcement report found late and *incomplete* responses still
widespread. Incomplete is the interesting half. Almost nobody refuses an
erasure on purpose; they miss a table.

Missing a table is a graph problem, and wreath is already holding the graph.
`wreath.migrations` derives every foreign key with its referential action and
its deferrability, because diffing a schema needs exactly that. A vendor
selling "graph discovery and cascade-safe deletes" is selling a hand-maintained
copy of the thing your models already declare, and the copy goes stale on the
next migration.

```python
privacy = Privacy(registry)                       # a compiled ORM registry

privacy.subject(User, key="id", delete=True)
privacy.classify(Photo, subject="owner_id", personal={"exif_gps": Erase.NULL})
privacy.classify(Comment, personal={"body": Erase.REDACT})
privacy.classify(AuditRecord, exempt=Declared(
    "retained under Art. 17(3)(b): the record of an erasure is the evidence it "
    "was performed, and erasing it would defeat the obligation to demonstrate "
    "compliance"))
privacy.retain(SupportTicket, after=days(90), on="closed_at")

print(privacy.render(privacy.plan("4711")))       # read it
await privacy.erase(database, "4711", digest="6f1c…")   # then run it
```

**Plan first, and the plan is the product.** `wreath privacy plan` prints the
traversal; `wreath privacy erase` runs a plan that was printed, and refuses
when the digest has moved. `wreath.infra` set this precedent for the same
reason -- an inference that is subtly wrong is worse than none, because it
looks authoritative -- and an erasure that is subtly wrong is worse still,
because it cannot be undone and the subject has been told it is done.

**Five findings, because five things go wrong quietly.** A classified table no
foreign key connects to the subject; a `SET NULL` edge that orphans a child row
where nothing can ever find it again; a `NO ACTION` edge from a row that
*survives* the erasure to one it deletes, which the database refuses so the
erasure stops half-way; a foreign-key cycle that admits no ordering; and rows
retained under an exemption. All but the last block the erasure outright.

**And the erasure records that it happened.** `erase` writes one row per
completed erasure -- subject, timestamp, plan digest, counts, and nothing else
-- in a transaction that first establishes from the pass ledger that every walk
finished. That row is the evidence the erasure was performed and the only thing
that lets a restore from backup know to replay it. See
`wreath._privacy.record`.

## Two limits, stated here rather than discovered later

**Backups are out of scope.** This walks live tables. A restore from a backup
taken before an erasure reinstates the data, and no amount of application code
changes that. What wreath does instead is *record* the erasure, so a restore
can replay it -- which is small, checkable, and more than the alternative of
implying a guarantee nobody can keep.

**Anonymisation is not erasure unless it is irreversible.** A hash of an email
address is not irreversible: it is a stable identifier for the same person,
which is the definition of pseudonymous data. So there is no hash disposition
here. `Erase.NULL` and `Erase.REDACT` destroy the value;
`Pseudonymise(Declared("why"))` keeps it joinable and *says so*, and every plan
containing one prints that the subject is still identifiable in those columns.

**And one inherited limit worth knowing before you need it.** The generated
passes reach their tables the way every `wreath.passes.ChunkedPass` over a
model does: a model whose `schema=` is a plain string renders *unqualified* and
resolves through `search_path`, because only a logical `SchemaRef` is qualified
into the statement. So an erasure over models in a schema that is not on the
connection's `search_path` will not find them. The nested subqueries this module
builds are rendered by `wreath.passes`' own resolver rather than by qualifying
the name here, so the subquery and the walk can never disagree about *which*
table they mean -- but they agree on being unqualified too. Put the schema on
the `search_path`, or use a logical schema.

"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ._privacy.declare import Classified, Personal, Subject, classified
    from ._privacy.execute import (
        ErasureBlocked,
        ErasureIncomplete,
        PlanMoved,
        PreparedErasure,
    )
    from ._privacy.model import (
        ColumnAction,
        CycleFinding,
        Disposal,
        Edge,
        Erase,
        ErasurePlan,
        ExportPlan,
        OrphanRisk,
        Pseudonymise,
        Reach,
        Retained,
        SurvivingReference,
        TableAction,
        Unreachable,
        as_dict,
    )
    from ._privacy.registry import (
        Classification,
        PrivacyDeclarationError,
        Retention,
    )
    from ._privacy.render import render_export_text, render_json, render_text
    from ._privacy.retention import schema_sql

__all__ = [
    "Classification",
    "Classified",
    "ColumnAction",
    "CycleFinding",
    "Disposal",
    "Edge",
    "Erase",
    "ErasureBlocked",
    "ErasureIncomplete",
    "ErasurePlan",
    "ExportPlan",
    "OrphanRisk",
    "Personal",
    "PlanMoved",
    "PreparedErasure",
    "Privacy",
    "PrivacyDeclarationError",
    "Pseudonymise",
    "Reach",
    "Retained",
    "Retention",
    "Subject",
    "SurvivingReference",
    "TableAction",
    "Unreachable",
    "as_dict",
    "classified",
    "render_export_text",
    "render_json",
    "render_text",
    "schema_sql",
]


class Privacy:
    """One application's data classification, and what can be done with it.

    Args:
        registry: the compiled `wreath.orm.Registry` whose foreign-key graph an
            erasure walks. Held rather than passed per call, because a plan
            derived from a different registry than the declarations were made
            against is a plan for a different application.
        erasure_record_retain: how long an erasure record is kept, in seconds.
            Set it to your backup horizon. There is deliberately no default:
            the honest one is "as long as your oldest backup" and nothing here
            can know that, so unset means kept-and-reported rather than a
            silent forever. See `wreath._privacy.retention`.
    """

    __slots__ = ("_erasure_record_retain", "_registry", "_orm")

    def __init__(self, registry: Any = None, *, erasure_record_retain: float | None = None) -> None:
        from ._privacy.registry import PrivacyRegistry

        self._orm = registry
        self._registry = PrivacyRegistry()
        self._erasure_record_retain = erasure_record_retain
        if registry is not None:
            # Annotated declarations are read once, here. Eagerly rather than at
            # plan time because the personal column names are published to the
            # log-site layer and a log site is built at import; and loudly
            # rather than best-effort, because a marker nobody read is the
            # silent gap this module exists to make visible.
            from ._privacy.declare import declare_registry

            declare_registry(self._registry, registry)
            self._publish()

    def subject(self, model: type, *, key: str = "id", delete: bool = False) -> None:
        """Name the model that is a data subject, and its identity column."""
        self._registry.subject(model, key=key, delete=delete)
        self._publish()

    def classify(
        self,
        model: type,
        *,
        subject: str | None = None,
        personal: dict[str, Any] | None = None,
        delete: bool = False,
        exempt: Any = None,
    ) -> None:
        """Declare a model's personal columns and how erasure treats them.

        See `wreath._privacy.registry.PrivacyRegistry.classify` for the
        refusals; the important one is that a disposition claiming to erase
        while leaving the subject identifiable is rejected by name.
        """
        self._registry.classify(
            model,
            subject=subject,
            personal=personal,
            delete=delete,
            exempt=exempt,
        )
        self._publish()

    def declare(self, model: type) -> bool:
        """Read one model's `Subject`/`Personal` annotations and its facet.

        `Privacy(registry)` already does this for every model the registry
        compiled. This is for a model that was not in it -- one imported after
        construction, or one a test declares in a function body.

        Returns:
            Whether the model declared anything.
        """
        from ._privacy.declare import declare_model

        found = declare_model(self._registry, model)
        if found:
            self._publish()
        return found

    def retain(self, model: type, *, after: float, on: str, reason: str = "") -> None:
        """Declare how long this model's rows live, measured from `on`."""
        self._registry.retain(model, after=after, on=on, reason=reason)

    def plan(self, subject_id: str, *, registry: Any = None) -> ErasurePlan:
        """What erasing one subject would do. Opens nothing, writes nothing."""
        from ._privacy.planner import build_plan

        return build_plan(self._registry, self._resolve(registry), str(subject_id))

    def access(self, subject_id: str, *, registry: Any = None) -> ExportPlan:
        """The read-mode traversal behind a subject-access request."""
        from ._privacy.planner import build_export_plan

        return build_export_plan(self._registry, self._resolve(registry), str(subject_id))

    def render(self, plan: ErasurePlan | ExportPlan, *, format: str = "text") -> str:
        """A plan as text to read, or JSON to diff."""
        from ._privacy.model import ExportPlan
        from ._privacy.render import render_export_text, render_json, render_text

        if format == "json":
            return render_json(plan)
        if isinstance(plan, ExportPlan):
            return render_export_text(plan)
        return render_text(plan)

    def retention(self) -> tuple[str, ...]:
        """Every declared retention window, and every table that lacks one."""
        from ._privacy.retention import describe_retention

        return describe_retention(self._registry, erasure_record_retain=self._erasure_record_retain)

    def retention_passes(self, **kwargs: Any) -> tuple[tuple[Retention, Any], ...]:
        """One `ChunkedPass` per declared window, for `jobs.drive` to schedule."""
        from ._privacy.retention import retention_passes

        return retention_passes(self._registry, **kwargs)

    def graph(self, *, registry: Any = None) -> Any:
        """The foreign-key graph this application declares."""
        from ._privacy.graph import build_graph

        return build_graph(self._resolve(registry))

    async def unmodelled_edges(
        self,
        database: Any,
        *,
        schema: str = "public",
        registry: Any = None,
        workload: str = "write",
    ) -> list[tuple[str, str, str, str]]:
        """Foreign keys the live catalog has that the ORM does not model.

        The one method here that opens a connection, and it is a *check* rather
        than part of planning: every edge it returns is a path an erasure would
        not walk. Run it in CI against a real schema and treat a non-empty
        result as a finding.
        """
        from ._privacy.graph import CATALOG_EDGES, build_graph, catalog_edge_rows, missing_edges

        graph = build_graph(self._resolve(registry))
        connection = await database.acquire(workload)
        try:
            rows = await connection.fetch(CATALOG_EDGES, schema)
        finally:
            await database.release(workload, connection)
        return missing_edges(graph, catalog_edge_rows(rows, schema))

    def prepare(
        self,
        subject_id: str,
        *,
        digest: str | None = None,
        registry: Any = None,
        **kwargs: Any,
    ) -> PreparedErasure:
        """Resolve a reviewed plan into passes, refusing anything unreviewed.

        Raises:
            PlanMoved: the plan changed since `digest` was printed.
            ErasureBlocked: the plan would leave personal data behind.
        """
        orm = self._resolve(registry)
        from ._privacy.planner import build_plan

        plan = build_plan(self._registry, orm, str(subject_id))
        from ._privacy.execute import prepare as _prepare

        kwargs.setdefault("record_retain", self._erasure_record_retain)
        return _prepare(self._registry, orm, str(subject_id), plan=plan, digest=digest, **kwargs)

    async def erase(
        self,
        database: Any,
        subject_id: str,
        *,
        digest: str | None = None,
        registry: Any = None,
        **kwargs: Any,
    ) -> PreparedErasure:
        """Run a reviewed plan, children before parents.

        Each table is a `wreath.passes.ChunkedPass`: paced, resumable, with the
        cursor advanced inside the chunk transaction. A crash resumes rather
        than restarting, which matters at the size where an erasure takes long
        enough to be interrupted.

        **It records itself.** When every pass has finished, one transaction
        re-reads the pass ledger, establishes that they all reached `done`, and
        writes the erasure record -- the evidence the erasure happened, and the
        only thing that lets a restore from backup know to replay it. A record
        is never written for an erasure that did not finish; see
        `wreath._privacy.record` for what the record holds and why it is
        retained despite naming the subject.

        The statutory clock started when the subject asked, not when this ran.
        A caller driving this from a durable job owes the request an age alarm
        -- a silently retrying erasure job is a compliance incident, and it is
        the one failure mode this module cannot see from in here.

        Raises:
            ErasureIncomplete: a pass stopped before the end of its walk. The
                walks resume, so the fix is to run this again.
        """
        prepared = self.prepare(subject_id, digest=digest, registry=registry, **kwargs)
        for _action, walk in prepared.steps:
            if walk is not None:
                await walk.run(database)
        from ._privacy.execute import record_erasure

        await record_erasure(prepared, database)
        return prepared

    def erasure_records(self, database: Any, *, schema: str = "wreath") -> Any:
        """The erasure record table as a `wreath.log.PostgresLog`.

        The read side, and the only place a `Database` belongs: replaying
        outstanding erasures after a restore reads it, and enforcing
        `erasure_record_retain` calls `purge()` on it -- from a durable job, as
        `wreath.log` says, because nothing calls that for you.
        """
        from ._privacy.record import ErasureRecord

        return ErasureRecord(schema=schema, retain=self._erasure_record_retain).bind(database)

    def _resolve(self, registry: Any) -> Any:
        resolved = registry if registry is not None else self._orm
        if resolved is None:
            raise ValueError("no ORM registry: pass one to Privacy(registry) or to the call")
        return resolved

    def _publish(self) -> None:
        """Push the personal names to the log-site layer.

        Called after every declaration rather than lazily, because a log site
        is declared at import and a name published after that site was built
        would not change it. Declaring classifications before the modules that
        log is therefore the rule, and it is stated in the guide.
        """
        from ._logsite import declare_personal

        declare_personal(self._registry.personal_names())


_EXPORTS = {
    "Classification": "_privacy.registry",
    "Classified": "_privacy.declare",
    "ColumnAction": "_privacy.model",
    "CycleFinding": "_privacy.model",
    "Disposal": "_privacy.model",
    "Edge": "_privacy.model",
    "Erase": "_privacy.model",
    "ErasureBlocked": "_privacy.execute",
    "ErasureIncomplete": "_privacy.execute",
    "ErasurePlan": "_privacy.model",
    "ExportPlan": "_privacy.model",
    "OrphanRisk": "_privacy.model",
    "Personal": "_privacy.declare",
    "PlanMoved": "_privacy.execute",
    "PreparedErasure": "_privacy.execute",
    "PrivacyDeclarationError": "_privacy.registry",
    "PrivacyRegistry": "_privacy.registry",
    "Pseudonymise": "_privacy.model",
    "Reach": "_privacy.model",
    "Retained": "_privacy.model",
    "Retention": "_privacy.registry",
    "Subject": "_privacy.declare",
    "SurvivingReference": "_privacy.model",
    "TableAction": "_privacy.model",
    "Unreachable": "_privacy.model",
    "as_dict": "_privacy.model",
    "classified": "_privacy.declare",
    "declare_model": "_privacy.declare",
    "declare_registry": "_privacy.declare",
    "build_export_plan": "_privacy.planner",
    "build_plan": "_privacy.planner",
    "CATALOG_EDGES": "_privacy.graph",
    "build_graph": "_privacy.graph",
    "catalog_edge_rows": "_privacy.graph",
    "missing_edges": "_privacy.graph",
    "describe_retention": "_privacy.retention",
    "record_erasure": "_privacy.execute",
    "render_export_text": "_privacy.render",
    "render_json": "_privacy.render",
    "render_text": "_privacy.render",
    "schema_sql": "_privacy.retention",
    "retention_passes": "_privacy.retention",
}

_MODULE_EXPORTS = {
    "_privacy.registry": (
        "Classification",
        "PrivacyDeclarationError",
        "PrivacyRegistry",
        "Retention",
    ),
    "_privacy.declare": (
        "Classified",
        "Personal",
        "Subject",
        "classified",
        "declare_model",
        "declare_registry",
    ),
    "_privacy.model": (
        "ColumnAction",
        "CycleFinding",
        "Disposal",
        "Edge",
        "Erase",
        "ErasurePlan",
        "ExportPlan",
        "OrphanRisk",
        "Pseudonymise",
        "Reach",
        "Retained",
        "SurvivingReference",
        "TableAction",
        "Unreachable",
        "as_dict",
    ),
    "_privacy.execute": (
        "ErasureBlocked",
        "ErasureIncomplete",
        "PlanMoved",
        "PreparedErasure",
        "record_erasure",
    ),
    "_privacy.planner": ("build_export_plan", "build_plan"),
    "_privacy.graph": (
        "CATALOG_EDGES",
        "build_graph",
        "catalog_edge_rows",
        "missing_edges",
    ),
    "_privacy.render": ("render_export_text", "render_json", "render_text"),
    "_privacy.retention": ("describe_retention", "retention_passes", "schema_sql"),
}


def _privacy_module(module: str) -> Any:
    from importlib import import_module

    return import_module(f".{module}", __package__)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    loaded = _privacy_module(module)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__, *_EXPORTS})


def _annotation_loader(
    annotate: Callable[[Any], dict[str, Any]],
) -> Callable[[Any], dict[str, Any]]:
    def load(format: Any) -> dict[str, Any]:
        namespace = globals()
        for module in ("_privacy.model", "_privacy.registry", "_privacy.execute"):
            loaded = _privacy_module(module)
            for export in _MODULE_EXPORTS[module]:
                namespace[export] = getattr(loaded, export)
        return annotate(format)

    return load


for _method_name in ("plan", "access", "render", "retention_passes", "prepare", "erase"):
    _method = getattr(Privacy, _method_name)
    _method.__annotate__ = _annotation_loader(
        cast(Callable[[Any], dict[str, Any]], _method.__annotate__)
    )
