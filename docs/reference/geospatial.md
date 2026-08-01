# `wreath.geospatial`

Coordinates, great-circle distances, and the bounding boxes that let a database
answer a proximity question with an index rather than a full scan. Reach for it
whenever a row has a place on it — a vehicle, a delivery, a site, a sighting —
and something needs to know how far apart two of them are, or which of them are
near here.

Like [`wreath.temporal`](temporal.md), it refuses an ambiguous value at the
boundary rather than guessing: a `Coordinate` is built with keywords, and a bare
pair of numbers is turned away.

The column types live next door in [`wreath.orm.types`](orm.md): `Point` for
tier 1, which needs no extension, and `Geography` for tier 2, which needs
`CREATE EXTENSION postgis` and says so at startup when it is missing.

`Coordinate` is what a row *holds*; `BoundingBox` and `Polygon` are what a query
*asks about*. A box is a rectangle in degrees and is what `within()` renders for
a tier-1 column; a `Polygon` is a region with a shape and goes to `covered_by()`
on a tier-2 one. Both are built from `Coordinate`s, so neither can reintroduce
the ordering ambiguity the constructor exists to refuse.

Reference: [Places and distances](../guides/geospatial.md) for the reasoning and
the worked examples.

::: wreath.geospatial
