```hero
eyebrow: The canonical example
title: One application. No glue.
lede: A camera-trap network for four wildlife reserves — a real schema, a read
  API over 140,000 observations, and row-level access control that hides a
  rhino's location from the people who should not have it. Every part ships in
  wreath.
action: Run it in five minutes -> quickstart.md
action: Read the code -> https://github.com/alexogeny/wreath/tree/main/example
```

Four reserves on three continents run camera traps. A trap fires, writes a JPEG
and a timestamp to an SD card, and sits there until somebody walks out to it —
which might be a week later, or a month. Volunteers review what came back.
Researchers count species. Rangers need to know where a poached animal was
photographed, and **nobody else does**.

That is the whole domain, and it is enough to need most of a framework.

## The one to try first

Station 25 in Nullarbor is marked sensitive: there are rhinos on it. Ask for it
as a volunteer:

```json
{"id":25,"reserve_id":3,"name":"Nullarbor 01","habitat":"riverine forest",
 "sensitive":true,"cameras":[...]}
```

Now ask as a ranger:

```json
{"id":25,"reserve_id":3,"name":"Nullarbor 01","habitat":"riverine forest",
 "sensitive":true,"latitude":0.6,"longitude":40.1,"cameras":[...]}
```

Two things are happening, and neither is a filter bolted onto a response.

**The coordinate key is absent, not null.** `"latitude": null` is a different
claim — it says this station has no coordinates, which is false, and a client
plotting it would put a rhino at the intersection of the equator and the prime
meridian. Absent means *not for you*, and a client can tell the difference.

**The rule is written once.** A Cedar policy decides who may locate a sensitive
station. The same policy answers the console's question — *what may I do?* — so
the button a volunteer cannot press is greyed out by the declaration that would
refuse them, not by a second list somebody has to keep in step. When the rule
changes, one file changes.

Quoted from `camera_trap/policies.py`, unedited:

```
// An ordinary station's location is not a secret. A waterhole is on the map.
permit(principal, action == Action::"Station::locate", resource)
  when { resource.sensitive == false };

// A sensitive station -- a midden, a nest tree -- is a ranger's to know.
permit(principal in Role::"ranger", action == Action::"Station::locate", resource)
  when { resource.sensitive == true };
```

Cedar's `forbid` overrides every `permit` unconditionally, so one standing rule
suspends an account outright without editing any of the permits above it. A
suspended ranger is refused by the same evaluator that would otherwise admit
them.

## What you would otherwise have assembled

This is not a competitive table. It is an inventory of integration work that
does not need doing, because the parts were built to hold each other.

| The example does | You would otherwise reach for | It is |
| --- | --- | --- |
| Nested routers building `/reserves/{slug}/stations/{id}/sightings` | a router library | `Router(prefix=...)`, included |
| Typed, validated query parameters with bounds | a validation library | `Annotated[int, Query(maximum=...)]` |
| Models, migrations, and a startup schema check | an ORM plus a migration tool | one registry, one artifact |
| Cursor pagination with a sort allow-list | hand-written keyset SQL | `wreath.pagination` |
| A cached species list that a write invalidates | a cache plus an invalidation hook | `@cached`, and the ORM tells it |
| Authorization, and *reporting* authorization to a UI | a policy engine plus a permissions endpoint | one Cedar policy set |
| Sessions, and an identity read from them | a session library plus an auth shim | `SessionPolicy` + a backend |

No Redis. No Celery. No Alembic. No `requirements.txt` at all: wreath has no
mandatory runtime dependencies, so the example's dependency list is wreath and
PostgreSQL.

## The problems it was built to have

An example that only does easy things proves nothing. This schema was designed
around the cases that break naive code:

**Four timezones, one of them fractional.** `Africa/Nairobi` has no daylight
saving, `Europe/Lisbon` does, `America/Belize` does not, and
`Australia/Adelaide` sits at **+09:30**. A "sightings on the 3rd" query has to
mean the 3rd *where the camera is*, and code that reads a local date as a UTC
midnight is wrong by nine and a half hours on one of these four — which looks
almost right, and so survives review.

**Data that arrives long after it happened.** A card collected on the 20th
carries images captured on the 1st. The `captured_at` and the `ingested_at` are
different columns because they are different facts, and every count over "last
week" has to decide which one it means. The schema tour
[queries both](walkthrough.md), and a later chapter builds the charts that have
to survive it.

**A free-text column that should never have been one.** `review_state` holds
whatever the console posted: `confirmed`, `Confirmed`, `ok`, `needs-review`,
`?`. Eighteen months and 140,000 rows later, nobody can count how many sightings
are confirmed. That is a v1 flaw on purpose — the later chapter that recodes it
is the honest way software actually evolves, and the interesting part is what
the migration system *refuses* while the column is mid-conversion.

## What is here now

Stages one to three of eight, and each is worth reading on its own:

- **[The schema, in psql](walkthrough.md)** — nine tables and 141,398
  deterministic rows, toured with real query output. Partial indexes, the two
  timestamps, and the free-text column.
- **[The read API](read-api.md)** — nine routes. Nested routers, binding that
  turns a query string into typed arguments before a handler runs, declared
  queries, stable paging, and a cache the ORM clears without being asked.
- **[Run it](quickstart.md)** — a container, a schema, the seed, the server, and
  a first request, in about two minutes.

Still to come: uploads and background ingest, the analysis views and their
charts, the deferred migration that cleans up `review_state`, and an operations
appendix.

## What it is not

It is not a tutorial. If you have not written a wreath handler yet, start with
[getting started](../getting-started/index.md) — this is the second thing you
read, when you understand the pieces and want to see one that is real.

It is also not a kitchen sink. A feature that does not fit a camera-trap network
naturally is left out rather than wedged in, because an example that uses
everything teaches nothing about what to use.
