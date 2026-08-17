# The audit trail

Sooner or later somebody asks a question about the past. Who changed this price?
Who removed that account? Who exported the customer list, and when? An audit
trail is how an application answers, and the reason most of them cannot is
always the same: the trail was something the code had to *remember* to write.

The remembering fails in a predictable order. It goes into the handler first,
because that is where the feature was built. It does not go into the background
job that reconciles the same table overnight, because that was written by
somebody else three months later. It does not go into the admin action added in
a hurry. Each gap is invisible — the trail still has rows in it, they are simply
not all of the rows — and the gap is only ever discovered by the one question it
cannot answer.

Wreath can do better than remembering, because it owns the ORM. The session
already knows which rows it wrote and which fields changed, and it knows it
inside the transaction that wrote them. So the trail is not a thing you call. It
is a property of the write.

## Declaring what is audited

Audited-ness is a fact about a model, so it is declared on the model, next to the
columns it talks about:

```python
from wreath.audit_log import audited
from wreath.orm import Model, column
from wreath.orm.types import Int64, Text


class Photo(Model, table="photos"):
    id: int = column(Int64, primary_key=True)
    caption: str = column(Text)
    exif_gps: str = column(Text, nullable=True)

    _audit = audited(redact={"exif_gps"})
```

The attribute name is documentation — the metaclass collects the declaration by
type, the same way it collects `unique()` and `index()`. What it does with it is
the useful part: the redacted column names are checked when the class is
created, so a redaction naming a column that was renamed two migrations ago is
an error you see at import rather than a redaction that quietly stopped covering
anything.

A redacted column still appears in the record. Its value is replaced by
`REDACTED`, because *"this changed and you may not see to what"* and *"this did
not change"* are different facts, and an auditor usually wants the first one.

## Binding an actor

A record with nobody's name on it answers none of the questions a trail is kept
for. So wreath asks for an actor, and refuses the write without one:

```python
from wreath.audit_log import actor

async def rename(request, session, photo_id: int):
    with actor(f"user:{request.identity.sub}"):
        photo = await session.get(Photo, photo_id)
        photo.caption = "a heron, at last"
        await session.flush()
```

A background job is an actor. So is a migration, and so is a test:

```python
with actor("job:nightly-rollup"):
    ...
```

The refusal happens *before* the statement reaches the database, which matters:
a write that has already happened cannot be undone by a failed append. If you
see `Unattributed`, the write did not go through.

Actors nest. An inner block wins for its own duration and the outer one resumes
afterwards, which is what makes "this request, except this bit, which is the
system" expressible.

## Wiring it up

The trail is a [`wreath.log`](../reference/log.md) with a particular shape, and a
session that has been given one:

```python
from wreath.audit_log import AuditTrail, declaration
from wreath.log import PostgresLog

trail = AuditTrail(PostgresLog(database, declaration()))
session = Session(registry, "write", audit=trail)
```

The DDL comes from one declaration through the ordinary schema machinery. Step
1 creates the table; step 2 installs the row guard and the statement-level
`TRUNCATE` guard, so upgrading an existing version-1 trail also closes it:

```python
from wreath.audit_log import declaration

declaration().schema_claim("audit_log")
```

## Three properties worth knowing about

**The record is atomic with the write.** It is appended on the session's own
connection, inside the session's own transaction, so a write that rolls back
leaves no record and a record that exists describes a write that happened. A
trail assembled afterwards — by a listener, by a queue, by a nightly job reading
the WAL — has a window in it, and the window is exactly where the interesting
failures live.

**The trail is append-only in the database.** `wreath` emits a `REVOKE` and a
row and statement triggers, so the application's own role cannot update, delete,
or truncate history. A database superuser can deliberately disable triggers;
that administrative capability must be controlled outside the application.

**Erasure is possible, and explicit.** An audit trail holds personal data, and a
subject may ask to be forgotten — a trail that could not answer that would force
you to choose between two obligations. `AuditTrail.forget` deletes one subject's
records, and it works by setting a transaction-scoped flag the trigger looks
for. Nothing else can delete a record: not a handler, not a migration, not a
ordinary database role. And because the flag is `SET LOCAL`, the permission ends with
the transaction rather than travelling with a pooled connection into whatever
runs next.

Retention is `KEEP_FOREVER` and that is deliberate. How long an audit trail
lives is a compliance decision, not a disk-space one, so nothing ages a record
out by accident and removing one is always an act that names whose record it is.

**Retention and erasure are two doors, and only one of them opens.**
[`wreath.log`](../reference/log.md)'s `retention_pass` is how a log that *does*
declare a window has it executed — a paced, counted `wreath.passes` walk — and
it refuses a `KEEP_FOREVER` log outright, which an audit trail always is. It
could have been taught to set `wreath.audit_erasure` and delete through the
trigger; it is not, and that is the design rather than an omission. That setting
is the whole of what stands between an audit record and a background job, and a
scheduled walk carrying it would hand the permission to delete evidence to a
process nobody is watching, every five minutes, forever. So `AuditTrail.forget`
stays the only door: one subject, named, for exactly one transaction.

**A flush's records are appended together.** The session builds each record where
the facts are correct — after the statement, before the dirty mask is cleared —
and appends them as one batch before the flush returns. That is still inside the
transaction the writes are in (`flush` opens one when you have not), so the
atomicity above is unchanged; what it removes is a round trip per audited
instance. A flush of a hundred audited rows was measured at 75.1 µs per record
appended one at a time and 11.5 µs per record batched — 6.5× — on a local
PostgreSQL with an A/A noise floor of 7.4 %. A flush of one row is a wash: the
batched path costs about 0.3 µs more interpreter work, which is under half a
percent of the round trip it still has to make, and below what the live
measurement can resolve.

Reference: [`wreath.audit_log`](../reference/audit_log.md), and
[`wreath.log`](../reference/log.md) underneath it.
