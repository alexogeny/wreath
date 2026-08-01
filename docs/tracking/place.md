# Place and policy

Two questions about *where*, and one about who may be told.

## The refusal that costs nothing and prevents a van in the ocean

```python
depot = Coordinate(lat=-1.9705, lon=36.1042)   # fine
depot = Coordinate(-1.9705, 36.1042)           # TypeError
```

[`wreath.geospatial`](../guides/geospatial.md) has no positional form, because
GeoJSON writes `[lon, lat]`, every mapping UI and every human writes "lat, lon",
and a bare pair is a coin flip nobody notices in review.

It is worth being precise about what that buys, because this example has a test
that could not be written. A caller who swaps this conservancy's two numbers
asks about `lat=36.10, lon=-1.97` — and both are perfectly legal coordinates.
That is a place in Turkey, and nothing anywhere can tell it was a mistake. An
out-of-range value is catchable and the handler answers 400 for it; **only
naming the arguments catches the swap**, and it catches it at the moment
somebody writes the line rather than the moment a customer notices.

## A proximity search has two halves

"Which collars came within 5 km of the waterhole" is answerable two ways, and
both return the same rows. One of them reads the whole table.

```python
boxes = bounding_boxes(centre, metres)
rows = await session.fetch(
    Fix.select().where(
        or_(*(and_(Fix.latitude >= box.lat_min, Fix.latitude <= box.lat_max,
                   Fix.longitude >= box.lon_min, Fix.longitude <= box.lon_max)
              for box in boxes))
    ).limit(limit + 1)
)

inside = []
for row in rows:
    metres_away = distance(centre, Coordinate(lat=row.latitude, lon=row.longitude))
    if metres_away <= metres:
        inside.append((metres_away, row))
```

`bounding_boxes` gives the degree-aligned rectangles containing the circle, and
a rectangle is what a btree can answer — that is what stops the query reading
every row. The rectangle is a *superset*, so the exact great-circle test still
runs over what comes back, in Python.

Writing only the exact test gives a correct answer and a sequential scan. So the
example asserts the plan, from the SQL the ORM actually compiles for its own
query rather than a hand-written reconstruction of it:

```
Bitmap Heap Scan on fixes t0
  Recheck Cond: ((latitude >= ...) AND (latitude <= ...) AND ...)
  ->  Bitmap Index Scan on wreath_5637df8dc01a1017
        Index Cond: ((latitude >= ...) AND (latitude <= ...) AND ...)
```

The discriminator is `Index Cond`, not the scan node's name. A bounding box that
reached the index only as a `Filter` would be reading the whole index, which is
the sequential scan wearing a different plan.

Skipping the second half is worse than it sounds, and the tests pin it: over the
seeded data a 3 km circle keeps roughly three-quarters of what its rectangle
returned. A circle is π/4 of its bounding square and the corners stick out by up
to 41% of the radius, so a `within` that skipped the exact test would be fast,
plausible, and quietly answering a different question — one whose answer is
about a third larger and shaped like a box.

That is not hypothetical either. The first version of `within` here computed
every distance, sorted by it, and returned the whole rectangle. It looked
right — every row in it *was* nearby — and the test that caught it is the one
asserting the two counts differ.

**Two edges are handled by the library rather than by this example, and they
are why the code is an `or_` of boxes rather than one `BETWEEN`.** A circle
crossing the antimeridian has no single rectangle, so `bounding_boxes` returns
two; a circle reaching a pole is bounded by no finite span of longitude, so it
widens to the whole range. This conservancy is nowhere near either, and the
branch never fires here — which is exactly the shape of code that is wrong in
production the first week somebody deploys it in Fiji.

### A bound on a proximity search cannot be a `LIMIT`

This one was a real bug in this example before it was a paragraph.

The rectangle is unordered. There is no `ORDER BY distance` to write, because the
distance is computed in Python after the rows come back. So truncating the
rectangle returns an *arbitrary* subset of the box — and then the nearest members
of an arbitrary subset get reported as the nearest members of the circle. The
answer is wrong and looks exactly like a right one, because every fix in it
really is nearby.

`nearest` was written with a `LIMIT` sized to the number of fixes it wanted, and
it returned confidently wrong answers around a waterhole where the box held
hundreds of rows. The bound now refuses instead:

```
400 more than 20000 fixes lie in the rectangle around (-1.97, 36.10) at 50000 m;
    narrow the radius, because a truncated rectangle would answer with an
    arbitrary subset of it
```

## Nearest, without a nearest-neighbour index

pgvector answers a KNN query by walking an index in distance order. A btree on
two independent columns cannot, because "nearest" is not a range. So the honest
tier-1 answer is a bounded search: ask a small circle, and if it did not contain
enough, double it.

Doubling means the number of queries is logarithmic in how wrong the first guess
was — five widenings cover a hundredfold radius — and a ceiling stops a query
for a point in the ocean from walking out to a hemisphere. A caller who reaches
the ceiling gets what there was, which may be nothing. Returning fewer than
asked for is a true answer; raising would make an empty conservancy an error.

**The radii are computed before the first query runs**, and that is worth a
sentence because the first version of this was a `while` loop with the ceiling
and the have-enough test in one condition. That is correct code and a bad shape:
every way of getting the condition slightly wrong is a *hang* on a request path
— a handler that never returns, holding a connection, on input a caller chose —
rather than a wrong answer. Mutation testing found it by not being able to
decide: two mutants of that loop came back as timeouts, because a harness cannot
tell a non-terminating handler from a slow one.

The replacement is a `range` and not another `while`, which is the same lesson
one layer down: a `while` whose condition one edit can make permanently true is
the same hazard wearing arithmetic instead of I/O, and it would build a list
until the process died. The count comes from `ceil(log2(max_m / start_m))`, so
the loop's length is decided before it starts — and the search became testable
with no database at all, which is how it now has tests that *kill* those
mutations rather than time out on them.

The nearest *landmark* to a fix is a different question and gets a different
answer: six rows, compared in Python. A bounding-box query there would cost a
plan, a round trip and a paragraph explaining an index on six rows, to save
comparing six numbers.

## A path is the sum of its legs

```python
path = Trajectory([(fix.recorded_at, fix.position) for fix in fixes])
path.distance   # metres travelled, summed leg by leg
path.duration   # seconds from first fix to last
path.speed      # mean metres per second, or None
```

Never the straight line from first to last: an animal that walks a circuit back
to the waterhole it started at still walked all day, and a displacement figure
would report nearly zero. `speed` is `None` rather than `0.0` when the path
spans no time, because a division that cannot be performed has no answer and
zero would read as "stationary", which is a different claim and the one a
welfare dashboard would draw as a dead animal.

**The ordering is `recorded_at` and not arrival**, and in this domain those
genuinely differ. A collar that spent three days under canopy uploads afterwards,
so the fixes in the middle of a track were inserted last. A trajectory built in
insertion order doubles back through three days of history and reports several
times the true distance.

## Precision as an outcome

The [index](index.md) has the grid and the policy that produces it. Two things
about *where the answer is applied* belong here instead, because both are about
place rather than about policy.

### The distance to a landmark is very nearly a position

This is the sharpest trap in the example, and every half of it looks harmless.

A waterhole's coordinates are published — they are on the visitor map. So "1.2 km
from Ndovu Waterhole" is not a fact *about* a position: it puts the animal on a
circle of known centre and known radius. Give a reader two of those and the
circles intersect at two points, one of which is usually in the air.

Which means a degraded coordinate with a precise landmark distance beside it is
not degraded at all. The 10 km cell centre is decoration on an answer accurate
to a metre. In review, the coordinate was coarsened by policy and the distance is
just a convenience for the map legend.

The fix is not "round the distance too" — a distance rounded to 10 km is
useless, and one rounded to 1 km still narrows a 10 km cell by two orders of
magnitude. The rule is that the key exists **only** for a reader who was already
given the exact position, and a test asserts it for every other principal.

### Distance travelled is not a location

The same reasoning, in the other direction. `distance_m` on a track is the same
number for all four principals, deliberately. Knowing that a leopard covered
6 km yesterday locates it to within the whole conservancy, which is where it
already was — and it is the one number a welfare question turns on, so degrading
it would destroy the useful half of the response to protect nothing.

Getting this backwards is easy: a rule that coarsens "everything spatial" would
take the distance too, and the resulting dashboard would be useless to the
people it was built for while remaining exactly as useful to a poacher.

## The ceiling, and what is on the other side of it

**Tier-1 geospatial has no spatial join, and this example is capped by that.**

Everything above runs on stock PostgreSQL with no extension, which is the point
of it: rectangles and haversine answer the questions ordinary applications
actually ask. What they cannot answer, at any amount of application code:

- *Which animals crossed the northern boundary this month?* Polygon containment
  against a line, over a time series. There is no rectangle for a boundary.
- *How much time did this animal spend inside the conservancy?* The same, plus
  interpolation between fixes.
- *Infer the migration corridor from these forty tracks.* A spatial join of
  tracks against each other, then a density surface.
- *Which fixes fall inside the exclusion zone the county drew last week?* An
  arbitrary polygon, supplied at runtime.

Every one of those needs PostGIS, and wreath's answer there is the shape it
already uses for pgvector: wreath implements the client half, and
`CREATE EXTENSION postgis` is required and always will be, because the privilege
usually is not the application's.

**As of this page, that client half is not shipped.** The
[geospatial guide](../guides/geospatial.md#what-this-deliberately-is-not) states
the intent and the [roadmap](../reference/roadmap.md) is where its status lives;
this page will not describe an API that does not exist yet, because an example
that documents an unshipped declaration is worse than one that names the
ceiling.

What is worth saying now is **how little of this example would change**, because
that is the real claim a tier-2 opt-in has to make. The models declare two
`Float8` columns and a composite index; a PostGIS deployment would declare one
geography column instead and drop the index for a GiST one. `place.within` and
`place.nearest` would collapse into single queries. `Trajectory`, the
`Coordinate` refusal, every Cedar policy, `degrade`, the whole live map and
every one of the precision tests would be untouched — none of them is about how
the rows are stored.

That is the honest summary: PostGIS would replace about forty lines of
`tracking/place.py` and unlock four questions this example cannot ask. It would
change nothing about who is allowed to know the answers.

---

Back to [the argument](index.md), or on to
[ingest and realtime](ingest.md).
