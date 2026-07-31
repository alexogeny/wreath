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

## What this deliberately is not

Everything above needs **no PostgreSQL extension**. That is the point of it: the
questions ordinary applications actually ask — how far apart, what is within N
metres, what is nearest, how far did this thing travel — are answerable on a
stock database.

It does not do polygons, containment, projections other than WGS84, geocoding,
routing, or map matching. Those need a real spatial engine, and wreath's answer
there is PostGIS as an opt-in client half, in the same shape it already supports
pgvector: wreath implements the client side, `CREATE EXTENSION postgis` is
required and always will be. See the roadmap for where that stands.

Reference: [`wreath.geospatial`](../reference/geospatial.md).
