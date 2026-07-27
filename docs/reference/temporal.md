# `wreath.temporal`

Instants, durations, and zones — dependency-free, over stdlib `datetime` and
`zoneinfo`. Reach for it instead of `arrow` or `pendulum`: an `Instant` is a
`datetime` subclass that cannot exist without a UTC offset, so the naive value
that a client reads as UTC is refused at the boundary rather than discovered in
production.

Declaring `Instant` settles the ORM column, the inbound coercion, the JSON on
the way out, the OpenAPI `format`, the generated TypeScript, and the GraphQL
scalar at once — see [Dates and times](../guides/dates-and-times.md) for the
tour.

::: wreath.temporal
