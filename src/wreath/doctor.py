"""Diagnose the bugs a green test suite cannot see.

Today that means the N+1 query: fifty fast statements where one belonged. It is
the most common performance defect in an ORM-backed API and the least visible,
because every individual part of it is correct. Finding it needs the route and
the queries in the same field of view, and in most stacks nothing holds both --
the ORM does not know what it is serving, and the server does not know what the
ORM did.

Wreath owns both layers, so it can say the useful sentence:

```python
GET /llamas issued 51 statements; 50 of them hydrated Trek
```
Two ways to hear it. In development, install the guard and the request fails at
the query that crossed the line, with a traceback pointing at the loop:

```python
app.add_middleware(NPlusOneGuard(limit=10))
```
In production, the Flight Recorder already records what each request did, so
`wreath doctor n-plus-one <socket>` reads it back out without reproducing
anything -- and each finding carries the `request_id` that `wreath replay`
needs to turn it into a regression test.
"""

from __future__ import annotations

import logging as _stdlib_logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ._nplusone import (
    Finding,
    NPlusOneDetected,
    Origin,
    QueryLedger,
    Repetition,
    _raise,
    find_n_plus_one,
    query_ledger,
    watch,
)

__all__ = [
    "Finding",
    "check_extension_types",
    "check_logging_streams",
    "NPlusOneDetected",
    "NPlusOneGuard",
    "Origin",
    "Preflight",
    "PreflightFinding",
    "Repetition",
    "TraceLookup",
    "TracedRequest",
    "TracedWork",
    "diagnose_n_plus_one",
    "find_n_plus_one",
    "find_requests_with_trace",
    "find_work_with_trace",
    "preflight",
    "route_manifest",
    "render_route_manifest",
    "render_preflight",
]

_STATE_TOKEN = "_wreath_nplusone_token"


async def diagnose_n_plus_one(
    client: Any, *, threshold: int = 10, limit: int = 256
) -> list[Finding]:
    """Scan a running server's recorded traces through its Inspector.

    `client` is a connected `InspectorClient`. Reads
    the recent timeline plus the route and model name tables, and returns
    findings worst first -- so a production N+1 is diagnosed from outside the
    process, without reproducing the request that caused it.

    Requires the server to be recording in Detailed mode or better: phases are
    what carry the model, and an unsampled request has none. A server whose
    metadata predates its ORM simply reports numeric model IDs.
    """
    timeline = await client.timeline(limit=limit)
    routes = (await client.metadata("routes")).get("rows", ())
    models = (await client.metadata("models")).get("rows", ())
    return find_n_plus_one(
        timeline.get("traces", ()), threshold=threshold, routes=routes, models=models
    )


@dataclass(frozen=True, slots=True)
class TracedWork:
    """One durable unit of work carrying a trace id."""

    #: `job` | `message` | `workflow` | `pass`.
    kind: str
    #: How that subsystem names this unit: a job id, a message id, an instance
    #: key, a pass name.
    identifier: str
    #: What it is: the task, the channel, the workflow, the pass name.
    label: str
    #: The row's own state word. `passes` calls its column `phase`, and this is
    #: that value rather than a translation of it -- an operator reading this
    #: report goes on to `wreath passes status`, where the same word appears.
    state: str
    tenant: str
    #: The row's `last_error`, or `None` when it has none. Workflow instances
    #: record their failure per step rather than on the instance, so they carry
    #: nothing here.
    detail: str | None
    #: The whole stored `traceparent`, so the span id survives to the report.
    traceparent: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identifier": self.identifier,
            "label": self.label,
            "state": self.state,
            "tenant": self.tenant,
            "detail": self.detail,
            "traceparent": self.traceparent,
        }


@dataclass(frozen=True, slots=True)
class TracedRequest:
    """One recorded request carrying a trace id, out of the Flight Recorder."""

    request_id: int
    route_id: int
    status: int
    duration_us: int
    is_failure: bool
    error_class: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "route_id": self.route_id,
            "status": self.status,
            "duration_us": self.duration_us,
            "is_failure": self.is_failure,
            "error_class": self.error_class,
        }


@dataclass(frozen=True, slots=True)
class TraceLookup:
    """Everything one trace id was found on, and everywhere it could not be looked for.

    `omitted` is the load-bearing half. A forensic answer that silently leaves a
    source out is worse than no answer: the reader concludes "nothing else
    carries this trace" from a search that never ran. Every source this could
    not read -- a table that is not there, a schema still on version 1, a
    recorder nobody pointed it at -- is named here in the same sentence as the
    findings.
    """

    trace_id: str
    work: tuple[TracedWork, ...] = ()
    requests: tuple[TracedRequest, ...] = ()
    omitted: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "work": [item.as_dict() for item in self.work],
            "requests": [item.as_dict() for item in self.requests],
            "omitted": list(self.omitted),
        }


@dataclass(frozen=True, slots=True)
class _Source:
    """One durable table this lookup reads, and how to name what it finds.

    Kept as data rather than four near-identical coroutines: every source asks
    the same question of a different table, and all that varies is which column
    is the identity, which is the label, and which holds the state.
    """

    kind: str
    #: The plural an operator reads in the `omitted` list. Spelled out rather
    #: than `kind + "s"`, which produced "passs".
    plural: str
    relation: str
    #: The column that identifies a row: a job id, a message id, a pass name.
    key: str
    #: The column that says what the row *is*: the task, the channel, the pass.
    label: str
    #: The column holding its state. `passes` calls it `phase`.
    state: str
    #: The column holding whatever went wrong. Every source has one; there is no
    #: `None` case, and a mutant sweep confirmed the branch that allowed for one
    #: could never be reached.
    detail: str = "last_error"


_SOURCES: tuple[_Source, ...] = (
    _Source("job", "jobs", "jobs", "id", "task", "state"),
    _Source("message", "durable messages", "messages", "id", "channel", "state"),
    _Source("pass", "passes", "passes", "name", "name", "phase"),
)


async def _relation_columns(connection: Any, schema: str, relation: str) -> set[str]:
    """Which columns a relation has, or an empty set when it is not there.

    One catalog read answers both questions this lookup needs -- does the table
    exist, and is it at the version that has `trace_context` -- and answering
    them together is what lets the caller say *why* a source was omitted
    instead of reporting an empty result for two different reasons.
    """
    rows = await connection.fetch(
        # `attname::text` because `name` comes back as *bytes*, not `str`: the
        # set would then contain `b'trace_context'`, every membership test would
        # answer no, and the lookup would report "this database is still on the
        # version before propagation" against a database that is not. It said
        # exactly that until a live test caught it.
        "SELECT a.attname::text AS attname FROM pg_attribute a "
        "JOIN pg_class k ON k.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = k.relnamespace "
        # `::text` on the parameters for the mirror-image reason: `nspname` and
        # `relname` are `name`, which the driver cannot *encode*.
        # `wreath-sql-lint` SQL002.
        "WHERE n.nspname = $1::text AND k.relname = $2::text "
        "AND a.attnum > 0 AND NOT a.attisdropped",
        schema,
        relation,
    )
    return {row["attname"] for row in rows or ()}


async def find_work_with_trace(
    connection: Any,
    trace_id: str,
    *,
    schema: str = "wreath",
    workflow_schema: str = "wreath_system",
    workflow_table: str = "workflow_steps",
) -> TraceLookup:
    """Every durable row carrying `trace_id`, plus what could not be searched.

    The forensic half of cross-seam causality: given a trace id off a log line,
    a dashboard or a failed job, say which jobs, durable messages, workflow
    instances and chunked passes that request caused. All four carry the
    enqueuing request's `traceparent` on their own durable row, which is what
    makes this one read per table rather than a join nobody can write.

    Matched with `split_part(trace_context, '-', 2)` rather than a `LIKE`: the
    stored value is a whole `traceparent`, the trace id is its second field, and
    an exact comparison has no wildcard to escape and no way to match a span id
    that happens to share a prefix.

    **What this does not answer.** It reads the database, so it finds durable
    work and nothing else. The *request* lives in the Flight Recorder's ring --
    `find_requests_with_trace` reads that, over the Inspector socket, and only
    as far back as the ring goes. Ephemeral bus messages carry no context at
    all and never appear here; that is a deliberate deferral rather than a gap
    in this function (see `wreath.messaging.MessageBus.publish`).
    """
    work: list[TracedWork] = []
    omitted: list[str] = []
    for source in _SOURCES:
        columns = await _relation_columns(connection, schema, source.relation)
        if not columns:
            omitted.append(
                f'{source.plural}: "{schema}".{source.relation} does not exist on this database'
            )
            continue
        if "trace_context" not in columns:
            omitted.append(
                f'{source.plural}: "{schema}".{source.relation} has no '
                "trace_context column, so this database is still on the schema "
                "version before propagation; apply the pending step and future "
                "work will be found"
            )
            continue
        # `dict.fromkeys` rather than a set: `passes` names the same column as
        # both its key and its label, and the projection has to stay ordered and
        # de-duplicated.
        wanted = dict.fromkeys(
            (
                source.key,
                source.label,
                source.state,
                "tenant",
                "trace_context",
                source.detail,
            )
        )
        rows = await connection.fetch(
            f'SELECT {", ".join(wanted)} FROM "{schema}".{source.relation} '
            "WHERE split_part(trace_context, '-', 2) = $1 "
            f"ORDER BY {source.key}",
            trace_id,
        )
        for row in rows:
            work.append(
                TracedWork(
                    kind=source.kind,
                    identifier=str(row[source.key]),
                    label=str(row[source.label]),
                    state=str(row[source.state]),
                    tenant=str(row["tenant"]),
                    detail=row[source.detail],
                    traceparent=str(row["trace_context"]),
                )
            )
    instances = f"{workflow_table}_instances"
    columns = await _relation_columns(connection, workflow_schema, instances)
    if not columns:
        omitted.append(
            f'workflows: "{workflow_schema}".{instances} does not exist on this database'
        )
    elif "trace_context" not in columns:
        omitted.append(f'workflows: "{workflow_schema}".{instances} has no trace_context column')
    else:
        rows = await connection.fetch(
            f"SELECT key, workflow, state, tenant, trace_context "
            f'FROM "{workflow_schema}".{instances} '
            "WHERE split_part(trace_context, '-', 2) = $1 ORDER BY key",
            trace_id,
        )
        for row in rows:
            work.append(
                TracedWork(
                    kind="workflow",
                    identifier=str(row["key"]),
                    label=str(row["workflow"]),
                    state=str(row["state"]),
                    tenant=str(row["tenant"]),
                    detail=None,
                    traceparent=str(row["trace_context"]),
                )
            )
    omitted.append(
        "ephemeral bus messages: they carry no trace context, because doing so "
        "would mean a versioned envelope around a live wire format"
    )
    return TraceLookup(trace_id=trace_id, work=tuple(work), omitted=tuple(omitted))


async def find_requests_with_trace(
    client: Any, trace_id: str, *, limit: int = 256
) -> tuple[TracedRequest, ...]:
    """Recorded requests carrying `trace_id`, read over the Inspector socket.

    The other end of the causal chain from `find_work_with_trace`: that says
    what a request caused, this says which request it was. Bounded by the
    recorder's ring, so a trace older than `limit` recent requests is simply not
    there any more -- which is a property of the ring, not of this lookup, and
    is why the two are separate functions with separate failure modes.

    Requires the server to be recording with correlation on; an unsampled
    request carries no trace id at all and cannot be found by one.
    """
    timeline = await client.timeline(limit=limit)
    found = []
    for trace in timeline.get("traces", ()):
        if trace.get("trace_id") != trace_id:
            continue
        found.append(
            # `.get` with a default rather than `x or 0`: the Inspector's own
            # `_trace_payload` always writes these keys, but this is a *protocol*
            # boundary and a peer on another build is the case worth surviving.
            # A dict default is not a branch, so there is nothing here that can
            # go untested.
            TracedRequest(
                request_id=int(trace.get("request_id", 0)),
                route_id=int(trace.get("route_id", 0)),
                status=int(trace.get("status", 0)),
                duration_us=int(trace.get("duration_us", 0)),
                is_failure=bool(trace.get("is_failure")),
                error_class=int(trace.get("error_class", 0)),
            )
        )
    return tuple(found)


class NPlusOneGuard:
    """Fail (or report) a request that queries one model over and over.

    `limit` is how many times a single model may be hydrated within one
    request before that is treated as a defect. Ten is a deliberate default:
    a handful of related lookups is ordinary, ten of the same model is a loop.

    By default the `limit`-th query raises `NPlusOneDetected` from
    inside the ORM call, which is the whole point -- the traceback names the
    loop. Pass `on_detect` to log the `Finding` instead and let the
    request finish, which is what you want in staging:

    ```python
    app.add_middleware(NPlusOneGuard(limit=25, on_detect=log.warning))
    ```
    Each model trips once per request, so a runaway loop yields one diagnosis
    rather than a thousand. Intended for development and staging: it costs one
    `ContextVar` read per ORM query, which is nothing against a round trip,
    but a guard that fails production requests is a worse outage than the N+1.
    """

    global_scope = True
    __slots__ = ("_limit", "_on_detect")

    def __init__(
        self, *, limit: int = 10, on_detect: Callable[[Finding], None] | None = None
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._on_detect = on_detect
        # Arms the ORM seam. Until a guard exists the seam does not so much as
        # read the ContextVar, so an app that never installs one pays nothing.
        watch()

    async def before(self, request: Any) -> None:
        # Deliberately *not* `_nplusone.watching`, though it is the same
        # ledger and the same `Origin`. The middleware protocol splits bind
        # and reset across two calls, so using the context manager would mean
        # holding a suspended generator on `request.state` -- and a suspended
        # generator is finalized when it becomes unreachable, which runs its
        # `finally` and unbinds the ledger the moment the request object is
        # collected rather than when the request ends. A plain token has no
        # finalizer and cannot do that. `watching` is for callers that have a
        # block; this is the caller that does not.
        ledger = QueryLedger(
            limit=self._limit,
            origin=Origin(kind="request", label=f"{request.method} {request.path}"),
            on_exceeded=self._on_detect or _raise,
        )
        request.state.__setattr__(_STATE_TOKEN, query_ledger.set(ledger))
        return None

    async def after(self, request: Any, response: Any) -> Any:
        token = request.state.get(_STATE_TOKEN)
        if token is not None:
            # Reset rather than set(None): a nested guard (or a test) must get
            # its own binding back, not a cleared one. A binding that escapes
            # anyway dies with the request's task.
            query_ledger.reset(token)
        return response


async def check_extension_types(registry: Any) -> list[str]:
    """Report extension types a registry needs that its database lacks.

    Startup already refuses to run against a database missing one -- see
    `wreath.orm.introspection.resolve_extension_types`, which raises with the
    extension named. This is the same reading without the refusal, for the case
    where you want to *ask* before deploying: a `Vector` column needs
    `CREATE EXTENSION vector`, some managed PostgreSQL tiers restrict who may
    run that, and finding out during a rollout is the expensive way.

    Args:
        registry: A started ORM registry.

    Returns:
        One human-readable line per missing extension type, worst first by name.
        Empty when every declared extension type resolved -- including when the
        registry declares none, in which case no query is issued at all.
    """
    from .orm.introspection import declared_extension_columns, probe_extension_types

    columns = declared_extension_columns(registry)
    if not columns:
        return []
    wanted = {column.pg_type.type_name: column.pg_type.extension for _, column in columns}
    users: dict[str, list[str]] = {}
    for spec, column in columns:
        users.setdefault(column.pg_type.type_name, []).append(
            f"{spec.model_type.__name__}.{column.python_name}"
        )
    connection = await registry.database.acquire("write")
    try:
        found = await probe_extension_types(connection, wanted)
    finally:
        await registry.database.release("write", connection)
    return [
        f"the {item.extension!r} extension is not installed on "
        f"{registry.database.name!r} (current schema "
        f"{item.current_schema or '?'!r}), so the {item.type_name!r} type used by "
        f"{', '.join(sorted(users[item.type_name]))} has no OID; run "
        f"CREATE EXTENSION IF NOT EXISTS {item.extension}"
        for item in found
        if not item.installed
    ]


def check_logging_streams(*, active: bool | None = None) -> list[str]:
    """Report stdlib loggers that will bypass wreath's log stream.

    `wreath.logging` deliberately does not install itself on the root logger:
    a framework that seizes global logging state fights `dictConfig`, surprises
    anyone with handlers of their own, and either double-emits or silently
    discards their configuration. The cost of that restraint is that a library
    logging to `logging.getLogger(...)` produces a second, disjoint stream --
    which an operator discovers at 3am while correlating by hand.

    So the restraint is paired with a check. This is the check: it names the
    loggers holding their own handlers while wreath logging is active, so the
    split is something tooling reports rather than something a human trips over.

    Args:
        active: Whether wreath logging is running. Defaults to asking the
            installed runtime; pass it explicitly to diagnose a configuration
            that is not the current process's.

    Returns:
        One human-readable line per logger that will not reach wreath's stream.
        Empty when there is nothing to say -- including when wreath logging is
        inactive, because then there is no second stream to be split from.
    """
    from . import logging as wreath_logging

    if active is None:
        active = wreath_logging.installed().sink is not None
    if not active:
        return []
    bridged = wreath_logging.bridged_loggers()
    if "root" in bridged or "" in bridged:
        return []  # the root bridge catches everything that propagates
    findings: list[str] = []
    manager = _stdlib_logging.Logger.manager
    for name, logger in sorted(manager.loggerDict.items()):
        if not isinstance(logger, _stdlib_logging.Logger) or not logger.handlers:
            continue
        if name in bridged:
            continue
        if (
            all(isinstance(handler, _stdlib_logging.NullHandler) for handler in logger.handlers)
            and not logger.propagate
        ):
            # A NullHandler on a non-propagating logger is a library silencing
            # itself, not a stream competing with wreath's.
            continue
        findings.append(
            f"logger {name!r} has {len(logger.handlers)} handler(s) of its own, so "
            f"its records will not reach wreath's log stream; bridge it with "
            f"wreath.logging.stdlib_bridge(logging.getLogger({name!r})) or accept "
            f"two streams deliberately"
        )
    return findings


def check_email_deliverability(
    sender: Any, *, timeout: float = 3.0, resolve: Callable[..., Any] | None = None
) -> list[str]:
    """Report the DNS records a configured mail sender needs and does not have.

    Sending mail is the one part of an application whose correctness lives
    somewhere the code cannot see. `SmtpEmailSender` can be configured
    perfectly, sign every message, and still have its mail rejected outright,
    because the sending domain's SPF, DKIM and DMARC records are in DNS and
    nobody published them. Since May 2026 that is a permanent 550 from Google,
    Yahoo and Microsoft rather than a spam-folder risk, and above 5,000 messages
    a day all three records are required rather than advisable.

    So this asks DNS what the code cannot: does the SPF record exist, is the
    selector this sender signs with actually published, and is there a DMARC
    policy. It is a diagnostic and never a gate -- an unreachable nameserver is
    reported as "could not tell", not as a failure, because a check that turns a
    slow resolver into a failed startup is a check people disable.

    Args:
        sender: A configured `SmtpEmailSender`.
        timeout: Seconds to wait for each lookup.
        resolve: The TXT resolver, for tests. Defaults to `wreath._dns.resolve_txt`.

    Returns:
        One human-readable line per finding, most serious first. Empty when SPF,
        DKIM and DMARC are all present and the DKIM key is published for the
        selector this sender signs with.
    """
    from ._dns import resolve_txt

    lookup = resolve or resolve_txt
    from_addr = getattr(sender, "from_addr", "") or ""
    _, _, envelope_domain = from_addr.rpartition("@")
    envelope_domain = envelope_domain.strip("<> ")
    if not envelope_domain:
        return [
            "the sender has no from address, so there is no domain whose SPF, DKIM "
            "and DMARC records could be checked"
        ]

    findings: list[str] = []
    signer = getattr(sender, "dkim", None)

    if signer is None:
        findings.append(
            f"{envelope_domain} sends unsigned mail: SmtpEmailSender has no dkim=, so "
            "every message is DKIM-unauthenticated. Above 5,000 messages a day to "
            "Gmail or Yahoo that is a permanent rejection, not a spam-folder risk; "
            "pass dkim=DkimSigner(...)"
        )
    else:
        # Alignment is checked before publication, because a published key for
        # the wrong domain passes DKIM and still fails DMARC -- and that failure
        # reads as "DKIM is broken" to everyone who looks at it.
        if signer.domain.lower() != envelope_domain.lower():
            findings.append(
                f"DKIM signs as d={signer.domain} but mail is sent From "
                f"{envelope_domain}: the signature will verify and DMARC will still "
                "fail, because DMARC requires the signing domain to align with the "
                "From domain"
            )
        record_name = f"{signer.selector}._domainkey.{signer.domain}"
        answer = lookup(record_name, timeout=timeout)
        if not answer.resolved:
            findings.append(f"could not read {record_name}: {answer.error}")
        else:
            published = [text for text in answer.records if "v=DKIM1" in text or "p=" in text]
            if not published:
                findings.append(
                    f"no DKIM public key is published at {record_name}, so every "
                    f"signature this sender makes fails verification. Publish a TXT "
                    f"record there containing the public half of the signing key"
                )
            elif all(_dkim_key_empty(text) for text in published):
                findings.append(
                    f"the DKIM record at {record_name} has an empty p=, which means "
                    "the key is revoked; verifiers treat every signature from it as a "
                    "failure"
                )
            elif signer.algorithm == "ed25519-sha256" and not any(
                "k=ed25519" in text for text in published
            ):
                findings.append(
                    f"this sender signs ed25519-sha256 but {record_name} does not say "
                    "k=ed25519, so verifiers will try to read the key as RSA and fail"
                )

    spf = lookup(envelope_domain, timeout=timeout)
    if not spf.resolved:
        findings.append(f"could not read the TXT records for {envelope_domain}: {spf.error}")
    else:
        records = [text for text in spf.records if text.lower().startswith("v=spf1")]
        if not records:
            findings.append(
                f"{envelope_domain} publishes no SPF record, so a receiver has no "
                "authorised-sender list to check this mail against"
            )
        elif len(records) > 1:
            findings.append(
                f"{envelope_domain} publishes {len(records)} SPF records; RFC 7208 "
                "requires exactly one, and a receiver seeing several returns permerror "
                "rather than picking one"
            )
        elif records[0].rstrip().endswith("+all"):
            findings.append(
                f"the SPF record for {envelope_domain} ends in +all, which authorises "
                "every host on the internet to send as this domain -- the same as "
                "publishing nothing, but harder to notice"
            )

    dmarc_name = f"_dmarc.{envelope_domain}"
    dmarc = lookup(dmarc_name, timeout=timeout)
    if not dmarc.resolved:
        findings.append(f"could not read {dmarc_name}: {dmarc.error}")
    else:
        policies = [text for text in dmarc.records if text.lower().startswith("v=dmarc1")]
        if not policies:
            findings.append(
                f"{envelope_domain} publishes no DMARC record at {dmarc_name}; SPF and "
                "DKIM alone do not satisfy the bulk-sender requirements, which ask for "
                "a policy of at least p=none"
            )
        elif _dmarc_policy(policies[0]) == "none":
            findings.append(
                f"the DMARC policy for {envelope_domain} is p=none, which meets the "
                "letter of the requirement and asks receivers to do nothing about "
                "forgery; p=quarantine is the 2026 baseline for a domain that sends "
                "real mail"
            )
    return findings


def _dkim_key_empty(record: str) -> bool:
    """Whether a DKIM TXT record carries a revoked (empty) `p=` tag."""
    for tag in record.split(";"):
        name, _, value = tag.partition("=")
        if name.strip() == "p":
            return not value.strip()
    return True


def _dmarc_policy(record: str) -> str | None:
    """The `p=` policy word from a DMARC record, lower-cased."""
    for tag in record.split(";"):
        name, _, value = tag.partition("=")
        if name.strip().lower() == "p":
            return value.strip().lower()
    return None


# The wiring omission is the failure mode wreath is most exposed to, because it
# ships fifty-seven subsystems and most of them refuse loudly *somewhere*: at
# startup, at configuration time, or in a plan a separate command prints. Loudly
# somewhere is not the same as loudly in one place, and the answers were spread
# across `wreath infra infer`, the hardening ruleset, and the route table.
# `preflight` asks all of them at once and prints one report. It aggregates and
# does not invent: every finding here is one another part of wreath already
# knows how to produce, which is what keeps it from becoming a fourth opinion
# that disagrees with the three.
# It also prints what it could **not** ask, which is the half that makes the
# rest safe to read. `wreath doctor trace` established the shape: a report that
# lists three findings and stops is read as "there are three", so every source
# that needs a database, a socket or a DNS answer is named with the command that
# does reach it.


#: How much a preflight finding costs you. Two values, deliberately: a scale
#: with a middle invites a middle, and the only question this report answers is
#: whether to deploy.
PreflightSeverity = str


@dataclass(frozen=True, slots=True)
class PreflightFinding:
    """One thing to resolve before this application is deployed."""

    #: Which of wreath's own checks produced it: `infra`, `hardening`, `routes`.
    source: str
    #: `blocking` or `advisory`.
    severity: PreflightSeverity
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class Preflight:
    """Everything preflight asked, and everything it could not."""

    application: str
    findings: tuple[PreflightFinding, ...] = ()
    #: Each entry names a check preflight cannot make and the command that can.
    unchecked: tuple[str, ...] = ()

    @property
    def blocking(self) -> tuple[PreflightFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "blocking")

    @property
    def advisory(self) -> tuple[PreflightFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "advisory")


#: What preflight cannot see from a built application, and what does see it.
#: Written out rather than derived, because each line is a judgment about why
#: the answer is somewhere else -- and a list that maintains itself would have
#: nowhere to put that.
_UNCHECKED: tuple[str, ...] = (
    "wreath's own tables exist in the target database -- `wreath schema check "
    "<target>` (needs a database)",
    "your models match the live schema -- `wreath migrations detect <target>` (needs a database)",
    "source-level security defects -- `wreath audit code` (a separate ruleset "
    "over your files, and the other half of what `hardening` runs at startup)",
    "N+1 queries under real traffic -- `wreath doctor n-plus-one <socket>` "
    "(needs a running server)",
    "mail will actually be delivered -- `wreath.doctor.check_email_deliverability` (needs DNS)",
    "whether a per-worker default is safe for your fleet -- preflight cannot see "
    "it. Sessions, idempotency, quotas and second-factor challenges all default "
    "to an in-process store, which is correct for one worker and wrong for four; "
    "each guide names the PostgreSQL-backed alternative",
)

#: A `GapKind` that means the deployment will start and then fail, rather than
#: start and be worth a second look.
_BLOCKING_GAPS = frozenset({"settings-key", "capacity"})

#: How many public routes a single finding lists before it stops naming them.
#: A finding that prints two hundred paths is one nobody reads to the end of.
_ROUTE_SAMPLE = 12

#: Why the route finding says *declared* and not *open*, in the finding itself.
#:
#: `access_level == 0` is the framework's own definition of "asks nothing of the
#: caller", and it is what the tape enforces -- but it is a statement about the
#: declaration, not about what the handler does. `crud`'s `Access.deny()` is the
#: case that proves it: the rule attaches nothing to the requirement and the
#: handler answers 403 regardless of identity, so a route that admits *nobody*
#: reads as level 0 here. Reporting that as "open" would be a preflight
#: confidently naming the safest route in the application as the risk.
_ROUTE_CAVEAT = (
    " -- this is the declaration and not the enforcement: a handler may still "
    "refuse from inside, as crud's Access.deny() does"
)


def preflight(
    app: Any,
    *,
    application: str = "",
    settings: Any = (),
    supplied: dict[str, str] | None = None,
    dotenv_keys: dict[str, str] | None = None,
) -> Preflight:
    """Ask every check wreath can answer from a built application.

    Three sources, none of them new:

    * **infra** -- `wreath.infra.infer`'s gaps. A settings key nothing supplies
      and a pool whose long-lived holders leave nothing for requests both block;
      a supplied key nothing reads, and something the stage cannot derive, are
      worth knowing and are not reasons to stop.
    * **hardening** -- `hardening.audit_configuration`, the tier read off the
      live object graph. Deliberately not the source tier: `wreath audit code`
      owns that, it reads files rather than objects, and two spellings of one
      gate is how they drift. It is named in `unchecked` instead.
    * **routes** -- how many endpoints declare nothing of the caller. A fact
      rather than a defect, so it is advisory however many there are: a login
      route and a health check are supposed to be there, and a check that fails
      on them is one that gets turned off in week one. It reports the
      *declaration* and says so -- see `_ROUTE_CAVEAT`.

    Opens nothing. The application is imported and read; no socket, no database,
    no DNS -- which is why the list of what that leaves out is part of the
    return value rather than a footnote.
    """
    from .hardening import audit_configuration
    from .infra import infer

    findings: list[PreflightFinding] = []

    plan = infer(
        app,
        application=application or "application",
        settings=settings,
        supplied=supplied or {},
        dotenv_keys=dotenv_keys or {},
    )
    for gap in plan.gaps:
        findings.append(
            PreflightFinding(
                source="infra",
                severity=("blocking" if str(gap.kind) in _BLOCKING_GAPS else "advisory"),
                subject=gap.subject,
                detail=gap.detail,
            )
        )

    for finding in audit_configuration(app):
        findings.append(
            PreflightFinding(
                source="hardening",
                # `Severity.ERROR` is the ruleset's own word for "this would be
                # refused under `hardening='block'`", so the translation is a
                # rename rather than a judgment made here.
                severity=("blocking" if finding.severity.value == "error" else "advisory"),
                subject=finding.rule_id,
                detail=f"{finding.surface}: {finding.message}",
            )
        )

    findings.extend(_tenancy_findings(app))

    public = _public_routes(app)
    if public:
        shown = ", ".join(public[:_ROUTE_SAMPLE])
        rest = len(public) - _ROUTE_SAMPLE
        findings.append(
            PreflightFinding(
                source="routes",
                severity="advisory",
                subject="routes with no declared requirement",
                detail=(
                    f"{len(public)} route(s) declare no authentication or "
                    f"authorization: {shown}"
                    + (f", and {rest} more" if rest > 0 else "")
                    + _ROUTE_CAVEAT
                ),
            )
        )

    return Preflight(
        application=application or "application",
        findings=tuple(findings),
        unchecked=_UNCHECKED,
    )


def _tenancy_findings(app: Any) -> list[PreflightFinding]:
    """A tenant-isolated registry with nothing to resolve a tenant from.

    The wiring omission `wreath.tenancy` exists to prevent, and the one that
    cannot be caught anywhere else: every individual piece is configured
    correctly, and the application starts and serves every request unbound. It
    blocks, because there is no reading of it that is fine.
    """
    from .tenancy import TENANCY_PREFLIGHT_SOURCE, TenancyMiddleware

    registries = getattr(app, "_orm_registries", None) or {}
    isolated = [
        name
        for name, registry in registries.items()
        if getattr(getattr(registry, "schema_mode", None), "kind", None) == "isolated"
    ]
    if not isolated:
        return []
    # `_global_middleware` holds `(priority, order, middleware)`, so the
    # middleware is the third element rather than the entry.
    installed = any(
        isinstance(entry[2], TenancyMiddleware) for entry in getattr(app, "_global_middleware", ())
    )
    if installed:
        return []
    return [
        PreflightFinding(
            source=TENANCY_PREFLIGHT_SOURCE,
            severity="blocking",
            subject=f"registry {name!r}",
            detail=(
                "this ORM registry is tenant-isolated and no TenancyMiddleware is "
                "installed, so nothing resolves a tenant for a request. Add "
                "app.add_global_middleware(TenancyMiddleware(Tenancy(directory=..., "
                "source=...)))"
            ),
        )
        for name in isolated
    ]


def _public_routes(app: Any) -> list[str]:
    """`METHOD /path` for every route that asks nothing of the caller.

    Through the same merge `wreath.openapi` and `wreath.signatures` use, and for
    the same reason: `RouteDefinition.requirement` holds only what the *router*
    contributed, so reading it alone reports every decorator-protected route as
    public -- which here would be a preflight that says an application is wide
    open when it is not.
    """
    image = getattr(app, "_application_image", None)
    if image is None:
        return []
    public: list[str] = []
    for route, requirement in zip(image.routes(), image.requirements(), strict=True):
        if requirement.access_level == 0:
            methods = "/".join(sorted(route.methods)) or "ANY"
            public.append(f"{methods} {route.path}")
    return public


def render_preflight(report: Preflight) -> str:
    """The report as text, worst first, with what was not checked at the end."""
    lines = [f"preflight: {report.application}", ""]
    blocking = report.blocking
    advisory = report.advisory
    if blocking:
        lines.append(f"blocking ({len(blocking)})")
        lines.extend(_render_finding(finding) for finding in blocking)
        lines.append("")
    if advisory:
        lines.append(f"advisory ({len(advisory)})")
        lines.extend(_render_finding(finding) for finding in advisory)
        lines.append("")
    if not blocking:
        lines.append("nothing blocking.")
        lines.append("")
    lines.append("not checked here -- each needs something preflight does not open:")
    lines.extend(f"  - {entry}" for entry in report.unchecked)
    return "\n".join(lines) + "\n"


def _render_finding(finding: PreflightFinding) -> str:
    return f"  {finding.source:<10} {finding.subject}\n             {finding.detail}"


def preflight_as_dict(report: Preflight) -> dict[str, Any]:
    """The report as plain JSON-compatible data."""
    return {
        "version": 1,
        "application": report.application,
        "blocking": len(report.blocking),
        "findings": [
            {
                "source": finding.source,
                "severity": finding.severity,
                "subject": finding.subject,
                "detail": finding.detail,
            }
            for finding in report.findings
        ],
        "unchecked": list(report.unchecked),
    }


def _qualified(value: Any) -> str:
    """A stable Python name, never a repr carrying a process-local address."""
    target = getattr(value, "__func__", value)
    module = getattr(target, "__module__", type(value).__module__)
    qualname = getattr(target, "__qualname__", type(value).__qualname__)
    return f"{module}.{qualname}"


def _type_ref(ref: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": ref.kind}
    if ref.name is not None:
        payload["name"] = ref.name
    if ref.arguments:
        payload["arguments"] = [_type_ref(argument) for argument in ref.arguments]
    if ref.literals:
        payload["literals"] = list(ref.literals)
    return payload


def _resource(value: Any) -> dict[str, Any]:
    if callable(value):
        return {"kind": "resolver", "name": _qualified(value)}
    try:
        from ._auth.cedar_engine import EntityUid

        if isinstance(value, EntityUid):
            return {"kind": "entity", "value": str(value)}
    except ImportError:
        pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return {"kind": "constant", "value": value}
    return {"kind": "constant", "type": _qualified(type(value))}


def _access(requirement: Any) -> str:
    if requirement.public:
        return "public"
    if requirement.identify:
        return "identified-public"
    if requirement.policies:
        return "authorized"
    if requirement.access_level == 2:
        return "administrator"
    if requirement.access_level == 1:
        return "authenticated"
    return "implicit-public"


def route_manifest(app: Any, *, application: str = "") -> dict[str, Any]:
    """Return a deterministic, JSON-compatible route and security contract.

    The manifest is deliberately source-shaped rather than runtime-shaped: it
    names original handlers, dependency factories, middleware, wire types, and
    the effective merged access requirement. No object repr enters the result,
    so two builds of unchanged source compare byte-for-byte through
    `render_route_manifest`.
    """
    from ._auth.permissions import declared_actions
    from ._auth.requirements import requirement_for
    from .typegen.inspect import build_api_model
    from .typegen.model import TypegenError

    compile_routes = getattr(app, "_compile_routes", None)
    if callable(compile_routes):
        compile_routes()
    image = getattr(app, "_application_image", None)
    routes = list(image.routes()) if image is not None else []
    operation_ids, diagnostics = image.operation_ids() if image is not None else ({}, ())
    if diagnostics:
        raise TypegenError(diagnostics)
    api = build_api_model(app, allow_unknown=True)
    operations = {(operation.method, operation.path): operation for operation in api.operations}
    app_middleware = tuple(
        item[2]
        for item in sorted(getattr(app, "_middleware", ()), key=lambda item: (item[0], item[1]))
    )
    global_middleware = tuple(
        item[2]
        for item in sorted(
            getattr(app, "_global_middleware", ()), key=lambda item: (item[0], item[1])
        )
    )
    qualified_app_middleware = [_qualified(item) for item in app_middleware]
    qualified_global_middleware = [_qualified(item) for item in global_middleware]
    entries: list[dict[str, Any]] = []
    requirements = image.requirements() if image is not None else ()
    for index, (route, requirement) in enumerate(zip(routes, requirements, strict=True)):
        security = {
            "access": _access(requirement),
            "declared": requirement.declares_access,
            "roles": [
                {"mode": check.mode, "values": sorted(check.values)}
                for check in requirement.role_checks
            ],
            "permissions": [
                {"mode": check.mode, "values": sorted(check.values)}
                for check in requirement.permission_checks
            ],
            "policies": [
                {"action": policy.action, "resource": _resource(policy.resource)}
                for policy in requirement.policies
            ],
            "second_factor_max_age": requirement.second_factor,
        }
        for method in sorted(route.methods):
            operation = operations.get((method, route.path))
            request = None
            response = None
            if operation is not None:
                request = {
                    "parameters": [
                        {
                            "name": parameter.wire_name,
                            "python_name": parameter.python_name,
                            "location": parameter.location,
                            "required": parameter.required,
                            "type": _type_ref(parameter.type),
                        }
                        for parameter in operation.parameters
                    ],
                    "body": (
                        _type_ref(operation.request_body)
                        if operation.request_body is not None
                        else None
                    ),
                    "body_media_type": operation.request_body_media_type,
                }
                response = _type_ref(operation.response_body)
            entries.append(
                {
                    "method": method,
                    "path": route.path,
                    "operation_id": operation_ids[(index, method)],
                    "name": route.name,
                    "handler": _qualified(route.endpoint),
                    "tags": list(route.tags),
                    "request": request,
                    "response": response,
                    "dependencies": [
                        {
                            "factory": _qualified(dependency.fn),
                            "scope": dependency.scope,
                            "cached": dependency.use_cache,
                        }
                        for dependency in route.dependencies
                    ],
                    "middleware": {
                        "global": qualified_global_middleware,
                        "application": qualified_app_middleware,
                        "route": [_qualified(item) for item in route.middleware],
                    },
                    "security": security,
                }
            )
    from .typegen.inspect import derive_operation_id

    for path, handler in getattr(app, "_ws_routes", ()):
        requirement = requirement_for(handler)
        entries.append(
            {
                "method": "WEBSOCKET",
                "path": path,
                "operation_id": derive_operation_id("WEBSOCKET", path),
                "name": None,
                "handler": _qualified(handler),
                "tags": [],
                "request": None,
                "response": None,
                "dependencies": [],
                "middleware": {
                    "global": qualified_global_middleware,
                    "application": [],
                    "route": [],
                },
                "security": {
                    "access": _access(requirement),
                    "declared": requirement.declares_access,
                    "roles": [
                        {"mode": check.mode, "values": sorted(check.values)}
                        for check in requirement.role_checks
                    ],
                    "permissions": [
                        {"mode": check.mode, "values": sorted(check.values)}
                        for check in requirement.permission_checks
                    ],
                    "policies": [
                        {
                            "action": policy.action,
                            "resource": _resource(policy.resource),
                        }
                        for policy in requirement.policies
                    ],
                    "second_factor_max_age": requirement.second_factor,
                },
            }
        )
    entries.sort(key=lambda entry: (entry["path"], entry["method"]))
    declared = {action for actions in declared_actions(app).values() for action in actions}
    vocabulary = getattr(app, "_authorization_vocabulary", None)
    return {
        "version": 1,
        "application": application or "application",
        "strict_access_declarations": bool(getattr(app, "_require_access_declarations", False)),
        "authorization": {
            "declared": sorted(declared),
            "vocabulary": list(vocabulary.actions) if vocabulary is not None else None,
            "unknown": list(vocabulary.unknown(declared)) if vocabulary is not None else [],
            "unused": list(vocabulary.unused(declared)) if vocabulary is not None else [],
        },
        "routes": entries,
    }


def render_route_manifest(manifest: dict[str, Any]) -> str:
    """Canonical JSON for version control and CI diffs."""
    import json

    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"
