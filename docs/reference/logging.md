# `wreath.logging`

Structured log records that travel on the same ring the Flight Recorder already
owns, so a line you write in a handler arrives correlated to its trace without
the request path ever building a context object to say so. Reach for it whenever
you would otherwise reach for `logging.getLogger(...)`, and read
[the logging guide](../guides/logging.md) first if you want the reasoning behind
the two tiers.

::: wreath.logging
