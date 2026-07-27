# Dates and times

Time is the type that has to mean the same thing in six places at once: the
column it is stored in, the parameter it arrives as, the JSON it leaves as, the
OpenAPI schema, the generated TypeScript client, and the GraphQL scalar.

Most applications re-decide it in each of them. The ORM hands back a `datetime`,
something wraps it, a handler calls `.isoformat()`, a template builds "3 hours
ago" by hand, and the frontend re-parses an ISO string to render it a seventh
way. None of that is wrong on its own, and the drift between them is invisible
until a client reads a naive timestamp as if it were UTC and a trek appears to
start ten hours early.

`wreath.temporal` is one answer to the question, wired into every surface.

## User story: porting a codebase that lives on `arrow`

> *Every module in the app I'm porting starts with `import arrow`. Wreath's core
> takes no dependencies, so that import has to go — but I am not hand-rolling a
> date library either.*

```python
from wreath.temporal import Instant, now, parse, relative

started = parse("2026-07-26T09:30:00Z")     # aware, or it raises
sydney = started.to("Australia/Sydney")     # the same moment, another clock
relative(started)                           # "3 hours ago"
```

The familiar moves, without the dependency: `now()`, `parse()`, `.to(zone)`,
ISO-8601 durations (`parse_duration("PT3H")`), and a relative formatter.

## An `Instant` is always zone-aware

This is the one opinion the module holds, and it is the reason it exists.

```python
Instant.parse("2026-07-26T09:30:00")
# TemporalError: an Instant must carry a UTC offset
```

There is no correct default for a timestamp without an offset — UTC is merely
the most popular wrong one, and picking it silently is how a herd's departure
time drifts by ten hours between two services. Where a naive value genuinely
needs a zone, say which:

```python
Instant.of(scraped_value, assume="Australia/Sydney")
```

Because `Instant` subclasses `datetime`, everything else keeps working: it is
stored by a `TimestampTz` column, compares against other datetimes, and adding
a `timedelta` gives you another `Instant`.

## User story: one declaration, six surfaces

> *I added a `started_at` to the trek model. Now I need to make the API return
> it properly, describe it in OpenAPI, get a real type in the TypeScript client,
> and expose it in GraphQL. That is four more places to remember.*

It is zero more places. Declare the column and the rest follows:

```python
class Trek(Model, table="treks"):
    id: Mapped[int] = column(Int64, primary_key=True)
    started_at: Mapped[datetime] = column(TimestampTz)
```

| Surface | What you get |
| --- | --- |
| ORM | `TimestampTz`, and comparisons that stay in the database |
| Inbound | `?since=2026-07-26T09:30:00Z` coerces to an `Instant`; naive is a 422 |
| REST JSON | `"2026-07-26T09:30:00+00:00"` — no `.isoformat()` in the handler |
| OpenAPI | `{"type": "string", "format": "date-time"}` |
| typegen | `IsoDateTime`, not `any` |
| GraphQL | a declared `DateTime` scalar, not `String` |

### The JSON half is the one you notice

```python
@app.get("/treks/{trek_id}")
async def trek(request, trek_id: int) -> dict:
    trek = await session.fetch_one(Trek.select().where(Trek.id == trek_id))
    return {"id": trek.id, "started_at": trek.started_at}   # just the value
```

Dates, times, datetimes, and durations render as ISO-8601 on the way out. A
handler that never writes `.isoformat()` cannot write it inconsistently.

This is a retry, not a tax: the encoder is tried as-is first, and only a
`TypeError` triggers the pass that rewrites temporal values. A response with no
timestamps in it pays for one `try` that never raises, which CPython charges
nothing for.

### The inbound half is the one that catches bugs

```python
@app.get("/treks")
async def treks(request, since: Instant | None = None) -> dict:
    ...
```

`?since=2026-07-26T09:30:00Z` arrives as an aware `Instant`.
`?since=2026-07-26T09:30:00` is a validation error naming the missing offset,
rather than a value quietly interpreted as UTC. Annotating `datetime` works too,
since that is what ported handlers say.

## User story: "3 hours ago", once

> *Three different templates render a relative time and none of them agree about
> "1 minutes". And when we add a second language, I have to find all of them.*

```python
from wreath.temporal import relative

relative(trek.started_at, locale=request.locale)   # "3 hours ago"
```

It reads the way a person speaks — `just now`, `1 minute ago`, `3 hours ago`,
`yesterday`, `in 2 hours` — and gets the singular right, which is the detail
every hand-rolled version misses on the first pass.

The `locale` argument is the point. English ships today, and an unknown locale
renders English rather than failing a page. But because the formatting lives in
one function that every surface already goes through, adding a language later is
a table entry — not a search for every call site that renders a date. `request.locale`
reads the caller's `Accept-Language`, so passing it through is the whole
integration.

!!! note "What is not translated yet"

    Only English is shipped. The seam is built for the rest: adding a language
    means adding a locale table with a CLDR plural rule, and nothing outside
    `wreath.temporal` changes when that happens. That is the argument for
    centralising the formatter *before* i18n rather than after.

## Durations

The stdlib has no ISO-8601 duration parser, which is why configuration files
full of `PT30S` end up with a hand-written one in every project:

```python
parse_duration("P1DT2H30M")        # timedelta(days=1, hours=2, minutes=30)
format_duration(timedelta(hours=2))  # "PT2H"
```

Years and months are rejected on purpose: `P1M` is 28 to 31 days depending on
when you ask, so a `timedelta` cannot hold one honestly, and quietly choosing 30
is a bug in someone's scheduler six months from now. `format_duration` never
emits one either, so the two are inverses over every `timedelta` — including
sub-second ones, which render as a fixed-point fraction:

```python
format_duration(timedelta(microseconds=1))   # "PT0.000001S"
```

That matters more than it looks. A formatter is only useful if its own parser
can read what it wrote, and anything that reaches for scientific notation on a
small number — or rounds a large one — quietly breaks that.

## What round-trips, and the one thing that does not

Every conversion here is checked as a *property* rather than by example, because
the duration bug above survived a passing test for exactly as long as that test
used a value with whole minutes and no fraction. The sweeps live in
`tests/test_temporal.py` behind the `fuzz` marker:

| Round trip | Domain swept |
| --- | --- |
| `format_iso` → `Instant.parse` | 754 aware moments: both sides of a DST change in ten zones, both passes of an ambiguous hour, `+05:45` and `+12:45`, every microsecond shape, years 1 to 9999 |
| an `Instant` through JSON and back | the same 754, through `jsonable` and the real encoder |
| `format_duration` → `parse_duration` | 33,033 durations, including `timedelta.min`, `.max` and `.resolution` |
| `format_iso` → `date.fromisoformat` | the calendar extremes and every leap-year boundary |

All four are exact.

## The repeated hour, and why a bucket cannot contain all of it

When a zone puts its clocks back, one local hour happens twice, so a single
local time names two instants. A bucket start can only *be* one of them, and
moments at the other one therefore sit outside their own bucket. No resolution
avoids that; there is only a choice of which pass to strand.

`Bucket.floor` resolves to the **later** of the two candidates, because that is
what PostgreSQL's `timestamp AT TIME ZONE zone` does — measured, not inferred:
864 samples across nine zones and both transition directions agree, including a
zone with a half-hour DST step, one whose tzdata entry uses negative DST, and
four whose transition falls at local midnight so a *day* boundary lands in the
skipped hour.

Agreeing is the whole point. A bucket boundary computed in Python and one
generated by `generate_series` have to be the same instant; when they differ, a
settled row files itself under a bucket the spine never emits and the value
disappears from every later read.

The visible consequence is that a moment in the *first* pass gets a bucket that
starts after it:

```python
tz = zone("Pacific/Auckland")
first_pass = Instant(2025, 4, 6, 2, 30, 0, 0, tz, fold=0)
Minute.floor(first_pass, tz)        # an hour *after* first_pass
```

**PostgreSQL does exactly the same thing with the same inputs.** Both engines
were run over 9,828 comparisons — every bucket unit, nine zones, densely across
each transition — and they agree on every one, including which values fall
outside their own bucket. So this is the shared answer rather than a wrinkle in
this module.

Reference: [`wreath.temporal`](../reference/temporal.md).
