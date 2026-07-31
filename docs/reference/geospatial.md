# `wreath.geospatial`

Coordinates, great-circle distances, and the bounding boxes that let a database
answer a proximity question with an index rather than a full scan. Reach for it
whenever a row has a place on it — a vehicle, a delivery, a site, a sighting —
and something needs to know how far apart two of them are, or which of them are
near here.

Like [`wreath.temporal`](temporal.md), it refuses an ambiguous value at the
boundary rather than guessing: a `Coordinate` is built with keywords, and a bare
pair of numbers is turned away.

Reference: [Places and distances](../guides/geospatial.md) for the reasoning and
the worked examples.

::: wreath.geospatial
