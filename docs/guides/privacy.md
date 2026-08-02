# Erasure, retention and subject access

A right-to-erasure request arrives with a clock attached. GDPR Article 17 gives
you a month; CCPA and CPRA give forty-five days, extensible once. What the
European Data Protection Board found when it looked, in its February 2026
coordinated enforcement report, was that responses are still routinely late and
routinely *incomplete* — and incomplete is the interesting half, because almost
nobody refuses an erasure on purpose. They miss a table.

Missing a table is a graph problem. Somewhere in your schema is a row that
belongs to the subject, reached through two foreign keys and a join nobody
remembered at four o'clock on a Friday. Finding it is not a matter of care; it
is a matter of walking the graph.

Wreath is already holding the graph. `wreath.migrations` derives every foreign
key with its referential action and its deferrability, because that is exactly
what diffing a schema requires. The vendors who sell "graph discovery and
cascade-safe deletes" are selling a hand-maintained copy of a thing your models
already declare — and the copy goes stale on the next migration, quietly, in
the direction of missing rows.

So this module reads the graph you have, and tells you what an erasure would
do.

## Declaring what is personal

There is no inference here. No column-name heuristic, no "looks like an email"
rule, nothing that guesses. A heuristic that catches `email` and misses
`contact_string` produces a plan that is confident and wrong, and a plan that
looks authoritative is worse than no plan at all.

You say it, once:

```python
from wreath.passes import Declared
from wreath.privacy import Erase, Privacy, Pseudonymise

privacy = Privacy(registry)                    # your compiled ORM registry

privacy.subject(Person, key="id", delete=True)
privacy.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
privacy.classify(Comment, personal={"body": Erase.REDACT})
privacy.classify(
    Ledger,
    personal={"payer_name": Erase.REDACT},
    exempt=Declared(
        "retained seven years under tax law; the rows are financial records "
        "before they are personal data"
    ),
)
```

`subject()` names the model that *is* a data subject. `classify()` names a
model's personal columns — and, where the model carries the subject's identity
itself, the column that does it. Everything else is reached by walking foreign
keys, so `Comment` needs no `subject=`: the planner finds it through `Photo`.

### Or say it on the column

The same declaration can live on the model, next to the column it is about:

```python
from typing import Annotated

from wreath.privacy import Erase, Personal, Subject, classified


class Person(Model, table="people"):
    id: Mapped[Annotated[int, Subject(root=True, delete=True)]] = column(
        Int64, primary_key=True
    )
    email: Mapped[str] = column(Text)


class Photo(Model, table="photos"):
    id: Mapped[int] = column(Int64, primary_key=True)
    owner_id: Mapped[Annotated[int | None, Subject()]] = column(
        Int64, references=Person.id, on_delete="set null", nullable=True
    )
    caption: Mapped[Annotated[str, Personal(erase=Erase.REDACT)]] = column(Text)


class Ledger(Model, table="ledger"):
    ...
    _privacy = classified(exempt="retained seven years under tax law")
```

`Privacy(registry)` reads these when it is constructed, and they produce
*exactly* the registrations the calls above do — same `Classification`, same
plan digest, which the test suite asserts. `Subject(root=True)` is the person;
a bare `Subject()` is the column that says whose row this is. Whether a table's
rows are deleted outright or exempt from erasure is a fact about the table
rather than about one column, so it is a `classified(...)` facet in the class
body.

Neither surface is the legacy one. A model that some other package declares
cannot be annotated by the application deploying it, and that application is
the controller — so `classify()` is the one that always works, and the
annotation is the one that cannot drift from a renamed column.

Still no inference. Nothing reads a column's name or its type; every marker was
written by a person.

## Reading the plan

```console
$ wreath privacy plan myapp.policy:privacy --subject 4711
```

The output is ordered the way the erasure runs — children before the parents
they reference, because a `RESTRICT` foreign key refuses the parent's delete
and a `SET NULL` one orphans the child. Each table says how it was reached, so
the first question a reviewer asks ("why is *this* in my erasure?") is answered
on the line above the answer.

Then come the five findings, and they are the reason this command exists.

**Unreachable classified data.** A model with personal columns that no
foreign-key path connects to the subject. The erasure would run, report
success, and leave the rows exactly where they were. This blocks.

**Orphan risks.** A `SET NULL` or `SET DEFAULT` edge pointing at a row you are
deleting. Delete the parent and the child survives, still holding the subject's
data, with the only column that pointed at the subject now null — unreachable
forever, and made unreachable *by the erasure itself*. The plan orders the
child first and says so.

**Surviving references.** A `NO ACTION` or `RESTRICT` edge pointing at a row
you are deleting, *from a row you are keeping*. Ordering answers those two
codes only when the child rows go too; a child that is merely anonymised — or
one nobody classified at all — keeps its foreign key, so PostgreSQL refuses the
parent's delete. It refuses it after the children have already been redacted,
which is the worst place for an erasure to stop, and it is invisible to a plan
that only looks at the tables somebody declared. This blocks.

**Foreign-key cycles.** A loop admits no ordering of plain deletes. Either the
constraints are deferrable and one transaction carries it, or somebody breaks
the loop by hand. Guessing is how an erasure half-runs, so this blocks too
unless every edge in the loop is deferrable.

**Retained rows.** Data that survives under a written exemption. Not a defect —
an audit trail that erased the record of its own erasure would be a compliance
failure pointing the other way — but you are entitled to see precisely which
personal data an erasure leaves behind, and why, in the reason somebody wrote
down.

A plan with unreachable data, a surviving reference or a blocking cycle reports
`BLOCKED`, and
`wreath privacy plan` exits 1. Run it in CI and it will tell you the day
somebody adds a model with an email column and no path home.

## Running it

Erasure runs from the application that owns the database, not from the command
line:

```python
await privacy.erase(database, "4711", digest="6f1c9d2ab04e…")
```

The digest is the one from the plan you read. If anything has moved since — a
new classification, a new model, a table that has become unreachable — the
digest no longer matches and the call refuses. "Executes a plan that was
printed" is meant literally, and it is checked rather than trusted.

Each table becomes a [chunked pass](chunked-passes.md): keyset-walked, paced
against live traffic, with the cursor advanced inside the chunk transaction so
a crash resumes rather than restarting. At the size where an erasure takes long
enough to be interrupted, that is the difference between finishing and starting
again.

There is no `wreath privacy erase` subcommand, deliberately. The reading half
belongs in a shell; an irreversible delete belongs in the place that knows why
it is being issued.

### It records that it happened

When every pass has finished, `erase()` opens one transaction, reads the pass
ledger *inside it* to establish that each declared walk reached `done`, and
appends one row to `wreath.wreath_erasures`:

| subject | subject_model | plan_digest | tables_touched | rows_affected | at |
| --- | --- | --- | --- | --- | --- |
| `4711` | `Person` | `6f1c9d2a…` | 3 | 41 | 2026-08-03T09:14:02Z |

That row is the evidence the erasure was performed, and it is the only thing
that lets a restore from a backup taken before the erasure know there is
anything to replay. It holds the subject, the timestamp, the digest and the
counts — and deliberately nothing about *what* was erased, because a record of
the values would make the evidence store a re-identification store.

An erasure is not one transaction, so a receipt cannot share one with it: the
walk is chunked and resumable by design. What is guaranteed is the direction
that matters — **no record is written for an erasure that did not finish**. A
pass that stopped raises `ErasureIncomplete` and writes nothing. The other
window (finished, then crashed before the record) is recovered by running
`erase()` again: the walks are already complete, the ledger still says so, and
the record is deduplicated on `(subject, plan digest)`, so a redelivered job
never produces a second receipt.

The record is personal data — "user 4711 was erased on 3 August" is a fact
about an identified person — and it is retained anyway, for the two reasons
above. Its own window is yours to set:

```python
privacy = Privacy(registry, erasure_record_retain=days(35))   # your backup horizon
```

There is no default, because the honest one is "as long as your oldest backup"
and nothing here can know that. Left unset the records are kept, and
`wreath privacy retention` prints `erasure records: UNBOUNDED` rather than
falling silent. Enforce the window by calling `purge()` on
`privacy.erasure_records(database)` from a durable job.

Apply the table's DDL as a migration: `privacy.schema_sql("wreath")`, next to
the pass ledger's.

## Retention

```python
privacy.retain(SupportTicket, after=days(90), on="closed_at",
               reason="support policy")
```

Each window becomes a recurring pass whose frontier *is* the window —
everything the database clock has already passed — so the finish line does not
move while the walk runs, and workers with disagreeing wall clocks agree on
where a cycle stops. Deletions land in the pass ledger, which is what turns "we
delete support tickets after ninety days" from a sentence in a policy document
into a number somebody can be shown.

`privacy.retention()` lists every declared window *and* every classified table
that has none, marked `UNBOUNDED`. The absence is the finding; a silence would
read as "no problem here".

## One declaration, two effects

Classifying a column changes more than the erasure plan. A field with that name
at a `wreath.logging` call site is fingerprinted rather than written verbatim,
whatever its type — because "an integer is not a secret-bearing shape" stops
being true the moment the integer is *which person* the record is about.

That is the whole argument for declaring this in the framework rather than in a
compliance spreadsheet. A second list of sensitive column names is stale the
day after the next migration. This one sits next to the schema it describes.

## Two limits, stated plainly

**Backups are out of scope.** This walks live tables. A restore from a backup
taken before an erasure reinstates the data, and no amount of application code
changes that. What wreath does instead is *record* the erasure, so a restore
can replay it — small, checkable, and better than implying a guarantee nobody
in this category can keep.

**Anonymisation is not erasure unless it is irreversible.** A hash of an email
address is not irreversible: it is a stable identifier for the same person,
which is the definition of pseudonymous data. So there is no hash disposition,
and asking for one is refused by name:

```python
privacy.classify(Account, personal={"email": "hash"})
# PrivacyDeclarationError: Account.email: 'hash' is not erasure -- a hash is
# reversible by lookup and still identifies one subject. Erasure is
# irreversible: use Erase.NULL or Erase.REDACT. To keep the value joinable and
# say so, use Pseudonymise(Declared('why')), which records that the subject is
# still identifiable
```

If you genuinely need the value to stay joinable, say so and it will be
carried into the plan as what it is:

```python
privacy.classify(Ticket, personal={
    "requester": Pseudonymise(Declared("support continuity across merges")),
})
```

Every plan containing one prints that those columns are **not** erasure and
that the subject remains distinguishable in them. That sentence is the point.
A compliance report that calls pseudonymisation erasure is a false statement
about somebody's rights, and the cheapest place to stop making it is the line
where the decision is written.

Reference: [`wreath.privacy`](../reference/privacy.md).
