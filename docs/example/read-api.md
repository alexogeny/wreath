# The read API

The [schema tour](walkthrough.md) explored the camera-trap example in `psql`.
This page is the same data through HTTP: nine routes that answer what a reserve
is, where its cameras hang, and what walked past them.

It is a small API on purpose. Every route exists to show one of the parts a read
path is made of — routers that compose a URL, binding that turns a query string
into typed arguments before a handler runs, declared queries that name a read
once, pagination that a client can page and sort inside an allow-list, and a
cache the ORM clears without being asked. There is no route here that
demonstrates nothing.

Every request and response below was run against the seeded database and the
output is what came back. The seed is deterministic, so yours will match.

## Running it

```bash
export CAMERA_TRAP_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"
psql "$CAMERA_TRAP_DSN" -c 'CREATE SCHEMA IF NOT EXISTS camera_trap'

export WREATH_MIGRATION_DSN="$CAMERA_TRAP_DSN"
PYTHONPATH=example wreath migrations apply camera_trap.app:app example/migrations/migration.bin
PYTHONPATH=example wreath run camera_trap.app:app --port 8000
```

Seeding is a separate step, and [the quickstart](quickstart.md) walks the whole
sequence — container, schema, artifact, seed, server — with the output each
command actually prints.

The observations are not public, so every request below carries a session. This
page signs in as a **ranger**, the role that is refused nothing, so that what you
see is the routing and the binding rather than the authorization:

```bash
curl -s -c jar.txt -X POST 'localhost:8000/session?email=ranger1@example.org'
```

That choice is load-bearing and this page says so twice: a volunteer's totals
are smaller, because the species a volunteer may see are a subset. The one place
below where the role changes the answer is called out where it happens.

## The URL is the domain's hierarchy

A reserve owns stations; a station owns sightings and the SD cards they came off.
The paths say so, and no handler spells one out. Two routers do it:

```python
reserves = Router(prefix="/reserves", tags=("reserves",))
stations = Router(prefix="/{slug}/stations", tags=("stations",))

@stations.get("/{station_id}/sightings")
async def list_sightings(...): ...

reserves.include_router(stations)
```

`/reserves/{slug}/stations/{station_id}/sightings` is assembled from those two
prefixes and the route's own path. Including a router takes a snapshot and folds
the prefixes, tags and dependencies into each route, so what you read in the file
is what runs — there is no sub-application dispatched at request time.

The whole surface:

| Route | What it answers |
| --- | --- |
| `GET /reserves` | the four reserves, each with its timezone |
| `GET /reserves/{slug}` | one reserve |
| `GET /reserves/{slug}/stations` | the stations in it |
| `GET /reserves/{slug}/stations/{station_id}` | one station and every camera it has held |
| `GET /reserves/{slug}/stations/{station_id}/sightings` | a page of what it recorded |
| `GET /reserves/{slug}/stations/{station_id}/deployments` | the last few cards |
| `GET /sightings/{sighting_id}` | one sighting, with what it points at |
| `GET /species` | the controlled vocabulary |
| `GET /species/{code}` | one species |

## The timezone is on the wire because it has to be

```bash
curl -s -b jar.txt localhost:8000/reserves
```

```json
{"items":[
 {"id":4,"slug":"chiquibul","name":"Chiquibul Forest","timezone":"America/Belize","area_hectares":17300},
 {"id":3,"slug":"nullarbor","name":"Nullarbor Station","timezone":"Australia/Adelaide","area_hectares":41200},
 {"id":1,"slug":"olkiramatian","name":"Olkiramatian Conservancy","timezone":"Africa/Nairobi","area_hectares":22400},
 {"id":2,"slug":"serra-da-estrela","name":"Serra da Estrela Reserve","timezone":"Europe/Lisbon","area_hectares":8900}
]}
```

Timestamps go out as instants. A client asking "what moved last night" needs to
know whose night, and that is the reserve's zone rather than the server's — so it
is a field, not an assumption.

## A station outlives its cameras

```bash
curl -s -b jar.txt localhost:8000/reserves/olkiramatian/stations/3
```

```json
{"id":3,"reserve_id":1,"name":"Olkiramatian 03","habitat":"acacia scrub","sensitive":false,
 "latitude":-0.778,"longitude":37.526,
 "cameras":[
  {"id":3,"serial":"CT-00003","model":"Cuddeback J3","firmware":"3.2.1","battery_pct":31,
   "deployed_at":"2025-01-03T10:00:00+00:00","retired_at":"2025-08-15T11:00:00+00:00"},
  {"id":51,"serial":"CT-00051","model":"Cuddeback J3","firmware":"4.0.0","battery_pct":82,
   "deployed_at":"2025-08-15T12:00:00+00:00","retired_at":null}
 ]}
```

Two devices, one place. That list is here because the handler asked for it:

```python
await session.load(station, Station.cameras)
```

`Station.cameras` is declared `load="raise"`, so reading it without loading it
raises rather than quietly emitting a query. Every other handler in the file
leaves the relationship alone and pays nothing for it. This is what stops a list
endpoint from growing an N+1 by accident: the accident is an exception, not a
slow page.

### Where a rhino is, is not published

```bash
curl -s -b jar.txt localhost:8000/reserves/nullarbor/stations/25
```

```json
{"id":25,"reserve_id":3,"name":"Nullarbor 01","habitat":"riverine forest","sensitive":true,
 "latitude":0.6,"longitude":40.1,
 "cameras":[{"id":25,"serial":"CT-00025","model":"Bushnell Core DS","firmware":"3.2.1",
             "battery_pct":58,"deployed_at":"2025-01-03T10:00:00+00:00","retired_at":null}]}
```

**This is the one request on the page where the signed-in role changes the
answer.** Sign in as a volunteer and ask for the same station:

```bash
curl -s -c volunteer.txt -X POST 'localhost:8000/session?email=volunteer1@example.org'
curl -s -b volunteer.txt localhost:8000/reserves/nullarbor/stations/25
```

```json
{"id":25,"reserve_id":3,"name":"Nullarbor 01","habitat":"riverine forest","sensitive":true,
 "cameras":[{"id":25,"serial":"CT-00025","model":"Bushnell Core DS","firmware":"3.2.1",
             "battery_pct":58,"deployed_at":"2025-01-03T10:00:00+00:00","retired_at":null}]}
```

No `latitude`, no `longitude`. A station marked `sensitive` is a rhino midden or
a raptor nest, and publishing where it is assists poachers.

Two details in that difference are deliberate. The `sensitive` flag **stays on
the wire for both**, so a client can render *withheld* rather than *unknown*.
And the coordinate keys are *absent* rather than `null`, because `"latitude":
null` is a different claim — it says this station has no coordinates, which is
false, and a map would plot it off the west coast of Africa.

The rule itself is a Cedar policy, not a branch in this handler:

```
permit(principal, action == Action::"Station::locate", resource)
  when { resource.sensitive == false };

permit(principal in Role::"ranger", action == Action::"Station::locate", resource)
  when { resource.sensitive == true };
```

## The reserve segment is enforced, not decorative

```bash
curl -s -b jar.txt localhost:8000/reserves/olkiramatian/stations/27
```

```json
{"type":"about:blank","title":"Not Found","status":404,
 "detail":"no station 27 in reserve 'olkiramatian'"}
```

Station 27 is Nullarbor's. Every handler that names a station resolves the
reserve first and then the station *within it* — one extra query, and the reason
the URL hierarchy means something. Without it, the reserve-scoped authorization
rules would have nothing to hold on to.

## A date is a local date

This is the request the example was built around.

```bash
curl -s -b jar.txt 'localhost:8000/reserves/nullarbor/stations/27/sightings?since=2026-01-01&days=30&size=2'
```

```json
{"station_id":27,
 "since":"2026-01-01T00:00:00+10:30",
 "until":"2026-01-31T00:00:00+10:30",
 "items":[
  {"id":62285,"station_id":27,"camera_id":27,"species_id":38,"deployment_id":321,
   "captured_at":"2026-01-30T06:29:53+00:00","uploaded_at":"2026-02-24T10:30:00+00:00",
   "confidence":68,"review_state":"","image_key":"images/27/0062285.jpg",
   "thumbnail_key":"thumbs/27/0062285.jpg","tags":{"batch":13},"notes":null},
  {"id":4638,"station_id":27,"camera_id":27,"species_id":13,"deployment_id":321,
   "captured_at":"2026-01-29T21:22:26+00:00","uploaded_at":"2026-02-24T19:30:00+00:00",
   "confidence":43,"review_state":"confirmed","image_key":"images/27/0004638.jpg",
   "thumbnail_key":null,"tags":{"batch":1},"notes":null}],
 "total":150,"page":1,"size":2,"pages":75,"has_next":true,"has_prev":false}
```

Read the offsets. `since=2026-01-01` became **+10:30**, and the same parameter
one reserve over, and six months later, becomes something else again:

| Request | `since` resolves to |
| --- | --- |
| `nullarbor`, `since=2026-01-01` | `2026-01-01T00:00:00+10:30` |
| `nullarbor`, `since=2026-06-01` | `2026-06-01T00:00:00+09:30` |
| `olkiramatian`, `since=2026-06-01` | `2026-06-01T00:00:00+03:00` |

Adelaide is +09:30 in winter and +10:30 in summer, and Nairobi never moves. A
camera records the wall clock it reads, so the window has to be built on that
clock:

```python
tz = zone(reserve.timezone)
start = from_wall_clock(datetime.datetime.combine(since, midnight), tz)
end = from_wall_clock(
    datetime.datetime.combine(since + datetime.timedelta(days=days), midnight), tz
)
```

The end is the local midnight `days` later, not the start plus `days * 24h`.
Across a daylight-saving change those are different instants, and the version
that adds hours drops or double-counts everything in the missing one.

`captured_at` and `uploaded_at` are both in the payload and are twenty-five days
apart on the first row. The first is when the animal walked past; the second is
when the card reached a laptop. A client that treats either as "when this
happened" is wrong
about one of them, which is exactly the property the analysis stage has to
survive.

## Bad input is refused before a query exists

```bash
curl -s -b jar.txt 'localhost:8000/reserves/nullarbor/stations/27/sightings?since=2026-01-01&days=4000'
```

```json
{"type":"about:blank","title":"Unprocessable Content","status":422,
 "detail":"Request validation failed",
 "errors":[{"loc":["query","days"],"msg":"value must be <= 90","type":"maximum"}]}
```

That bound is one annotation:

```python
days: Annotated[int, Query(minimum=1, maximum=SETTINGS.max_window_days)] = 7
```

The handler is never entered. The ceiling comes from
`CAMERA_TRAP_MAX_WINDOW_DAYS` and is fixed when the module is imported, which is
what start-up configuration means — an operator sets it, a request cannot.

`since` deliberately has **no** default. "Everything ever recorded at this
station" is a scan of 140,000 rows that any caller could ask for, and a default
of "the last week" would be a moving window that made this page untrue a week
after it was written.

Sorting is an allow-list, and a name outside it is a 422 rather than a 500:

```bash
curl -s -b jar.txt 'localhost:8000/reserves/nullarbor/stations/27/sightings?since=2026-01-01&days=30&sort=notes'
```

```json
{"type":"about:blank","title":"Unprocessable Content","status":422,
 "detail":"cannot sort by notes; sortable columns are captured_at, confidence, id"}
```

There is no path from a query string to an arbitrary column.

## Paging over 140,000 rows

`page`, `size` and `sort` bind straight from the query string, and their bounds
and defaults come from `wreath.pagination` so the two cannot drift:

```python
page: Annotated[int, Query(minimum=1, maximum=MAX_PAGE)] = 1,
size: Annotated[int, Query(minimum=1, maximum=MAX_SIZE)] = 20,
sort: str = "",
```

`paginate` then runs the page and its total together:

```bash
BASE='localhost:8000/reserves/nullarbor/stations/27/sightings?since=2026-01-01&days=30'
curl -s -b jar.txt "$BASE&size=2&page=2"            # ids 93454, 11076
curl -s -b jar.txt "$BASE&size=2&sort=-confidence"  # ids 48994, 93454 — both confidence 99
```

The default sort is `-captured_at,-id`. The primary key is in there as a
tiebreaker for a reason: two sightings captured in the same second can otherwise
swap places between page 1 and page 2, so one row is served twice and another
never at all.

`page` is capped at `MAX_PAGE` (10,000). `LIMIT/OFFSET` makes the database walk
and discard every row before the offset, so an unbounded page number is a full
scan a caller can ask for at will; past that depth the right answer is a keyset
filter, which is what this endpoint's date window already is.

## The reads have names

```python
class SightingsByStation(Queries[Sighting]):
    in_window = query(
        Sighting.station_id == Param("station"),
        Sighting.captured_at >= Param("since"),
        Sighting.captured_at < Param("until"),
    )

class RecentDeployments(Queries[Deployment]):
    for_station = (
        query(Deployment.station_id == Param("station"))
        .order_by(Deployment.collected_at.desc())
        .limit(10)
    )
```

The shape is fixed when the class is defined and only the values vary, so each
declaration compiles once through the ORM's existing plan cache however many
stations ask it. `bind(...)` hands back an ordinary `Select`, which is what
`paginate` takes:

```python
query = SightingsByStation.in_window.bind(station=station.id, since=start, until=end)
```

Two decisions in that file are worth copying, and both are about restraint.

**No declaration sorts.** `order_by` appends, so a declaration that ordered would
make the caller's `?sort=` a tiebreaker rather than the sort — silently, and
visibly wrong only on page two. Ordering belongs to the handler.

**Only the mandatory filters are declared.** `min_confidence` is optional, and a
parameter in the declared shape would have to be supplied by every caller
including the ones that do not want it. The handler adds it:

```python
if min_confidence:
    query = query.where(Sighting.confidence >= min_confidence)
```

That costs a second compiled shape, which is the honest price of a filter that is
genuinely sometimes absent.

## One sighting, and what it points at

A sighting is a row of foreign keys. On its own it says species 38 walked past
station 27 in front of camera 27.

```bash
curl -s -b jar.txt localhost:8000/sightings/62285
```

```json
{"id":62285,"station_id":27,"camera_id":27,"species_id":38,"deployment_id":321,
 "captured_at":"2026-01-30T06:29:53+00:00","uploaded_at":"2026-02-24T10:30:00+00:00",
 "confidence":68,"review_state":"","image_key":"images/27/0062285.jpg",
 "thumbnail_key":"thumbs/27/0062285.jpg","tags":{"batch":13},"notes":null,
 "station":{"id":27,"reserve_id":3,"name":"Nullarbor 03","habitat":"acacia scrub",
            "sensitive":false,"latitude":0.622,"longitude":40.126},
 "camera":{"id":27,"serial":"CT-00027","model":"Cuddeback J3","firmware":"3.2.1",
           "battery_pct":43,"deployed_at":"2025-01-03T10:00:00+00:00","retired_at":null},
 "species":{"id":38,"code":"CROC","common_name":"Nile crocodile",
            "scientific_name":"Crocodylus niloticus","protection":"open","nocturnal":false}}
```

```python
Sighting.select().where(Sighting.id == sighting_id).include(
    Sighting.station.joined(),
    Sighting.camera.joined(),
    Sighting.species.joined(),
)
```

Three to-one relationships, so a join costs less than three extra statements;
`selectin` would be the right call for a to-many, where a join multiplies the
parent row by its children. The **list** endpoint deliberately includes none of
them and returns ids instead — twenty rows are twenty ids, not sixty joins — and
because the relationships raise rather than lazy-load, that decision is visible
in the query rather than hidden in a loop.

## The one endpoint worth caching

```python
@species.get("/")
@cached(ttl=SETTINGS.species_cache_ttl, invalidate_on=[Species])
async def list_species(request, session): ...
```

Forty rows that change a few times a year, read by every client on every screen
that renders an animal's name. It qualifies on three counts: the answer is the
same for everyone, it is small and bounded so no caller can fill the store, and
wreath knows when it goes stale.

That last one is the part a bolt-on cache cannot do. `invalidate_on=[Species]`
puts this cache on the ORM's own write announcement — a committed write to the
species table clears it, from a handler, a job, an admin console, anywhere. The
example's tests assert exactly that: they change a species' common name through a
write session and then read the endpoint again, and the new name is there. No
invalidator is called, because none has to be.

The TTL is what remains: a backstop for a change wreath cannot see, such as a row
edited by hand in `psql`. It fires on commit and never before, so a write that
rolls back invalidates nothing.

`GET /species/{code}` is *not* cached. Forty separate entries for the forty rows
the list already holds in one, each evicting something that was earning its
place.

## What is not here yet

- **Nobody is authenticated.** The sensitive-station rule is flat rather than
  per-caller, and there is no login. The authorization stage adds Cedar policies
  and the field-level rules that make "withheld from volunteers" mean something.
- **No writes.** This is the read half; review, upload and ingest arrive with
  object storage.
- **No charts.** `Series`, sealing and corrections are the analysis stage, and
  they are what the window and the local clock here exist to feed.

The code is `example/camera_trap/`, and every route on this page is exercised by
`tests/example/test_read_api.py` — including the two claims a reader would
otherwise have to take on trust: that a station cannot be reached through another
reserve's URL, and that a write to the species table clears the cache.
