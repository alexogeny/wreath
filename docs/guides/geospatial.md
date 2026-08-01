# Places and distances

Almost every application eventually grows a column that means *where*. A fleet
has vans, a courier has drops, a field-service team has jobs and engineers, a
directory has offices, a research station has collars on animals. And almost
every one of those teams reaches the same afternoon: they install a geo package
for one function, or they paste a haversine implementation off the internet, and
then they discover — usually in production, usually from a customer — that the
proximity query reads the whole table, or that everything near the date line has
quietly vanished.

`wreath.geospatial` is the same decision wreath already made about time. Just as
[`wreath.temporal`](../reference/temporal.md) refuses a naive datetime rather
than assuming UTC, this module refuses an ambiguous coordinate rather than
picking an order for you.

## The refusal that matters

```python
from wreath.geospatial import Coordinate

depot = Coordinate(lat=-27.4698, lon=153.0251)   # fine
depot = Coordinate(-27.4698, 153.0251)           # TypeError
```

That second line is the whole argument. GeoJSON writes coordinates as
`[lon, lat]`. Every mapping UI, every phone, and every human writes "lat, lon".
Both orders are completely plausible to a reader, which means a positional pair
is a coin flip that nobody notices in review — and when it lands the wrong way
up, the symptom is not an exception. It is a van in the Indian Ocean.

So there is no positional form. Name the arguments and the ambiguity cannot
exist. Wreath owns both sides of its own wire and its own storage, so this is
the only place the question ever has to be asked.

The bounds are inclusive, because the poles and the antimeridian are real
places: `Coordinate(lat=90, lon=0)` and `Coordinate(lat=0, lon=180)` are both
fine. It is only beyond them that a value cannot mean anything, and those are
refused with a message naming the field.

## Distance

```python
from wreath.geospatial import distance

metres = distance(depot, site)
```

Great-circle metres, symmetric, on a sphere of mean radius 6 371 008.8 m.

**Read that sentence carefully if you are billing by it.** A sphere is not the
Earth. Against a proper ellipsoidal calculation (Vincenty, Karney) these
distances differ by up to about **0.5%** — worst near the poles, best around 45
degrees of latitude. That is comfortably good enough to sort a list of nearby
sites, to route a van, or to decide whether a collar moved. It is not good
enough to put on an invoice without saying which model produced the number, and
wreath would rather tell you that than let you find out from a customer.

The implementation is the haversine formula rather than the spherical law of
cosines, for the usual reason: the law of cosines takes `acos` of a value within
an ulp of 1 for short distances and loses every significant figure it had. Short
distances — consecutive GPS fixes seconds apart — are the common case here.

## Finding what is near

A proximity search has two halves, and using only one of them is the mistake.

```python
from wreath.geospatial import bounding_boxes

boxes = bounding_boxes(depot, 5_000.0)
```

`bounding_boxes` gives you the degree-aligned rectangles that contain every
point within the radius. A rectangle is what an index can answer. The exact
great-circle test still has to run over the rows the boxes return, because a box
is a *superset* of a circle — but the box is what stops the query reading every
row in the table.

Writing only the exact test gives a correct answer and a sequential scan. Wreath
already refuses that shape for vector search, where `where()` turns away a bare
distance comparison, and the reasoning is identical here.

### Two edges that are usually broken

**The antimeridian.** A circle centred at 179.95°E extends past 180° and comes
back at −180°. There is no single rectangle for that, because no comparison
operator understands a wrapped edge — so `bounding_boxes` returns **two**, both
with longitudes inside `[-180, 180]`:

```pycon
>>> len(bounding_boxes(Coordinate(lat=0.0, lon=179.95), 20_000.0))
2
```

Code that emits one rectangle with `lon_min = 179.77` and `lon_max = 180.13`
returns nothing at all for everything just over the line. The bug reads as "no
vehicles near Fiji", which is not obviously a longitude problem.

**The poles.** A circle that reaches a pole is bounded by no finite span of
longitude, because every meridian passes through it. There, `bounding_boxes`
widens to the whole range rather than raising:

```pycon
>>> box, = bounding_boxes(Coordinate(lat=89.99, lon=0.0), 50_000.0)
>>> box.lon_min, box.lon_max
(-180.0, 180.0)
```

Both of these are answered rather than refused, deliberately. A fleet operating
across the date line and a research station near a pole are ordinary situations,
and a library that raised on them would simply be unusable in those places.

## Paths through time

A `Trajectory` is where this module and `wreath.temporal` meet: an ordered
sequence of `(Instant, Coordinate)` fixes, and the measures that fall out of it.

```python
from wreath.geospatial import Trajectory

path = Trajectory([(fix.recorded_at, fix.position) for fix in fixes])
path.distance   # metres travelled, summed leg by leg
path.duration   # seconds from first fix to last
path.speed      # mean metres per second, or None
```

Distance is the sum of the legs and never the straight line from first to last —
an animal that returns to where it started still travelled a long way, and a van
that did a loop is still owed its fuel. `speed` is `None` rather than `0.0` when
the path spans no time, because a division that cannot be performed has no
answer and zero would read as "stationary", which is a different claim.

Every fix must carry a timezone-aware timestamp. A naive one makes `duration`
wrong across a DST boundary and `speed` wrong with it, so it is refused at
construction in the same spirit as everything else here.

## In the database

A `Coordinate` column is `Point`, which is core PostgreSQL's `point` — OID 600,
in the catalog rather than allocated by `CREATE EXTENSION`. That is what makes
the no-extension claim true rather than aspirational, because core also ships
the GiST `point_ops` operator class the index needs.

```python
from wreath.geospatial import Coordinate
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Point, Text

class Station(Model, table="stations"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    at: Mapped[Coordinate] = column(Point, index="gist")
```

### Proximity

```python
Station.select().where(Station.at.within(here, 5_000))          # metres
Station.select().order_by(Station.at.nearest(here)).limit(10)
```

`within()` renders **two** things, ANDed, and both are load-bearing:

```sql
"t0"."at" <@ box(point($1, $2), point($3, $4))
AND (2 * 6371008.8 * asin(sqrt(...))) <= $8
```

The box is the half a GiST index can answer, and it is the only reason the query
does not read the whole table. The haversine is the half that makes the answer
*right*: a box is a superset of the circle, and its corners reach about 1.41×
further than its edges, so the box alone returns rows that are not within the
radius. Dropping either one gives you a query that is fast and wrong, or correct
and unusable.

A circle crossing the antimeridian renders two boxes, ORed. `<@` has no notion of
a wrapped edge, and searching only one side is the classic date-line bug.

`nearest()` is a **distance, not a predicate** — the same treatment the pgvector
distances get. `where()` refuses it by name, and ordering by it **requires a
limit**: an unbounded proximity search sorts the whole table and no index can
answer it.

### What this costs to have

Two implementations of the haversine already existed (Python and C); the SQL
above is a third, and the sphere radius is written once in the renderer and
pinned against the module constant by a live test rather than retyped.

`point` also needed a binary parameter encoder in **both** driver twins, because
the prepared path binds parameters in binary and neither twin has a shared
fallback for an unenumerated OID. Reading needs no such thing: the driver hands
back an OID it does not know as raw bytes, and `Point.from_wire` reads them.
PostGIS's `geography` hits the same wall for the same reason and has the same
pair of encoders; its wire form is EWKB hex, which is the one spelling both the
text and the binary parameter paths accept.

## One declaration, every surface

A `Coordinate` in a model settles what happens at each boundary it crosses,
the way an `Instant` does for time:

| Surface | What it becomes |
| --- | --- |
| ORM column | PostgreSQL `point`, with its GiST index |
| Binding | `{"lat": …, "lon": …}`, range-checked |
| REST JSON | `{"lat": …, "lon": …}` — the same object, on the way out |
| OpenAPI | an object with `format: coordinate` and bounded `lat`/`lon` |
| TypeScript | `{ lat: number; lon: number }` |
| Python client | the real `wreath.geospatial.Coordinate` |
| `.proto` | `message Coordinate { double lat = 1; double lon = 2; }` |

**Every one of those is an object with named components, never a pair.** That
is the same refusal the constructor makes, carried to each surface that could
otherwise reintroduce it: GeoJSON orders `[lon, lat]`, people say "lat, lon",
and a two-element array is ambiguous exactly where being wrong is most
expensive. A generated client cannot transpose what was never positional.

Note the inversion worth knowing about: the database stores `point` as
`(x=lon, y=lat)`, the PostGIS convention. The wire shape is keyword `lat`/`lon`
regardless. Storage order and wire order are different questions, and the
declaration is what keeps them from being confused.

```python
@dataclass
class Station:
    id: int
    at: Coordinate

# {"id": 1, "at": {"lat": -33.8, "lon": 151.2}}  ->  Station(at=Coordinate(...))
# [-33.8, 151.2]                                 ->  refused
```

A handler may return a `Coordinate` directly, or anything containing one:

```python
@app.get("/depot")
async def depot(request) -> Coordinate:
    return Coordinate(lat=-27.4698, lon=153.0251)

# {"lat": -27.4698, "lon": 153.0251}
```

That works because `Coordinate` defines `__jsonable__`, which is how a type
tells the encoder it knows how to become JSON. The hook is **opt-in on
purpose** — a blanket "serialize any object with fields" rule would put every
field of every model a handler happened to return on the wire, including the
ones a sensitive-field guard exists to keep off it. So a type has to say so,
and this one says an object rather than a pair.

It is also the *only* spelling. `wreath.crud` serialises a coordinate column
through the same hook rather than building its own dict, because two
independent spellings of one wire contract is how they drift apart.

## What this deliberately is not

Everything above needs **no PostgreSQL extension**. That is the point of it: the
questions ordinary applications actually ask — how far apart, what is within N
metres, what is nearest, how far did this thing travel — are answerable on a
stock database.

It does not do polygons, containment, projections other than WGS84, geocoding,
routing, or map matching. Those need a real spatial engine, and wreath's answer
there is PostGIS as an opt-in client half — the next section.

Reference: [`wreath.geospatial`](../reference/geospatial.md).

## Tier 2: PostGIS, opt-in

When tier 1 is not enough — you need a projection, a true KNN ordering, or
containment against a polygon — declare a `Geography` column instead:

```python
from wreath.orm.types import Geography

class Station(Model, table="stations"):
    id: Mapped[int] = column(Int64, primary_key=True)
    at: Mapped[Coordinate] = column(Geography(), index="gist")
```

The Python value is the same `Coordinate`, so handlers do not change when a
model moves between the tiers. The column is `geography(Point,4326)`; only the
point form is declarable, because wreath has no value type for a polygon and a
column with nothing to hold is a hole rather than a feature.

**`CREATE EXTENSION postgis` is required and always will be.** Wreath ships the
client half only. The contract is word-for-word `pgvector`'s, because it is the
same mechanism: the type's OID is assigned by `CREATE EXTENSION`, so it is read
once at startup and a database without the extension fails **there**, naming
it, rather than at the first query with an unrecognised OID.

```
Station.at declares the 'geography' type, which the 'main' database does not
provide: the 'postgis' extension is not installed on the search path ...
Run CREATE EXTENSION IF NOT EXISTS postgis in that database
```

Two things deliberately do *not* move with that OID: the plan-cache shape token
and the model fingerprint. Both are derived from the type's name. An OID in
either would give one query two cache entries against two databases, or report
every model as drifted when the extension is reinstalled.

`detect`, `generate`, `apply` and `down` round-trip the column and its GiST
index like any other, and a matching index is not rediscovered as drift.

### One name, two renderings

`within()` and `nearest()` gain a **second rendering, not a second name**. The
compiler dispatches on the column's own type, so a model that moves from `Point`
to `Geography` keeps its queries:

```python
Beacon.select().where(Beacon.at.within(here, 20_000))
# tier 1:  at <@ box(point($1,$2), point($3,$4)) AND <haversine> <= $5
# tier 2:  ST_DWithin(at, $1, $2)

Beacon.select().order_by(Beacon.at.nearest(here)).limit(5)
# tier 1:  ORDER BY <haversine> ASC LIMIT $n
# tier 2:  ORDER BY (at <-> $1) ASC LIMIT $n
```

The second one is the reason to be here. `<->` on a `geography` is a **true
nearest-neighbour search**: PostgreSQL plans it as
`Index Scan ... Order By: (at <-> …)`, walking the GiST index in distance order
rather than filtering a bounding box and sorting what survives. It answers
**metres**, like tier 1, so the two are comparable — they differ by the ~0.5%
between a sphere and the WGS84 spheroid.

`ST_DWithin` plans as a `&&` index condition with the exact test filtered over
what it returns — *literally the shape tier 1 hand-builds*, which is the
strongest argument that the two belong behind one name.

### Containment against a region

The question tier 1 structurally cannot answer:

```python
from wreath.geospatial import Coordinate, Polygon

catchment = Polygon([
    Coordinate(lat=-34.0, lon=150.0),
    Coordinate(lat=-34.0, lon=152.0),
    Coordinate(lat=-33.0, lon=152.0),
    Coordinate(lat=-33.0, lon=151.0),
    Coordinate(lat=-32.0, lon=151.0),
    Coordinate(lat=-32.0, lon=150.0),
])

Beacon.select().where(Beacon.at.covered_by(catchment))
# ST_Covers(ST_GeogFromText($1), at)
```

A `Polygon` is built from `Coordinate`s and **nothing else** — not `(lon, lat)`
pairs and not a WKT string, both of which are this module's founding refusal
wearing a different hat. WKT writes longitude first and every mapping UI writes
"lat, lon", so a hand-typed `POLYGON((...))` that got the pair backwards is a
valid document describing the wrong hemisphere, and nothing would ever raise.
The ring is closed for you; wreath writes the WKT.

`ST_Covers` rather than `ST_Contains`, and not as a preference:
**`ST_Contains(geography, geography)` does not exist**, so a query reaching for
it fails at the database. `ST_Covers` also answers "yes" for a point exactly on
the boundary, which is the reading a catchment wants — an address on the line is
in the district.

The region travels out as **text**, lifted by `ST_GeogFromText`. There is no
`geography(Polygon)` column type, no OID to resolve for it, and nothing new on
the wire: containment is a predicate and KNN is an ordering, and both return the
model's own columns.

### The refusals tier 2 keeps

| Written | Refused because |
| --- | --- |
| `covered_by()` on a `point` column | tier 1 has no polygon operator an index answers |
| `~at.covered_by(region)` | "everywhere else" is every row the index excluded |
| `order_by(at.nearest(here))` with no `limit` | an unbounded proximity search sorts the whole table |
| `where(at.nearest(here))` | a distance is a number; compare it or order by it |

The limit rule is a **token** test, not an operator test: geography KNN renders
the same `<->` that pgvector does, and an unbounded
`ORDER BY embedding <-> $1` is allowed and stays allowed. The two are different
tokens inside the compiler precisely so this refusal can tell them apart.

### What tier 2 does not do yet

`geography(Polygon)` **columns**: storing a polygon is a different problem from
querying against one, and it is the case that needs a decoder for a type the
driver does not enumerate. Projections other than 4326. `ST_Intersects`,
`ST_Buffer`, and the rest of the PostGIS surface. Two operations, done properly,
with honest refusals around them — stated here rather than discovered at the
first call.

## Tiling a region

A `Grid` is a lattice of approximately-square cells over an extent — the
spatial analogue of a bucket width, and what a heatmap is drawn on.

```python
from wreath.geospatial import BoundingBox, grid

reserve = BoundingBox(lat_min=-30.0, lat_max=-29.0, lon_min=150.0, lon_max=151.0)
lattice = grid(reserve, metres=10_000)

lattice.rows, lattice.columns   # known before any query runs
lattice.count                   # rows * columns
lattice.cell(0, 0)              # a BoundingBox
lattice.centre(0, 0)            # a Coordinate
lattice.index_of(position)      # (row, column), or None if off the extent
```

Cells are indexed from the extent's south-west corner, and the lattice always
reaches *past* the far edge rather than dropping a partial cell — narrowing the
region the reader asked for is the one thing a dense axis must not do.

Because `rows` and `columns` are arithmetic on the extent, the number of cells
a declaration will produce is a **declaration-time fact**. That is what lets
[`Cells`](calculated-views.md) enforce a ceiling where a reviewer reads it,
rather than after the database has already done the work.

### Approximately square, and the refusal that keeps it honest

Latitude steps are constant. Longitude steps are computed once, at the extent's
middle latitude, because the metres in a degree of longitude shrink towards the
poles. Across a modest extent that is a fraction of a per cent, and
`lattice.distortion` reports where a given one sits.

Across a *tall* extent it is the difference between a 10 km cell at one edge
and a 5 km cell at the other, with one legend covering both — so `grid` refuses
past 10% variation and names the measured figure. High latitude is fine; it is
variation *across* the extent that is the problem, and a library that refused
Tromsø would simply be unusable there.

An extent crossing the antimeridian is also refused, because a lattice
generated over a wrapped longitude range runs backwards. `bounding_boxes`
already returns two boxes for that case, so the caller has the tool to grid
each half.
