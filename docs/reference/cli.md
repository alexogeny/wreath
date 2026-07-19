# `wreath` CLI

The `wreath` command runs applications (`wreath run`, `wreath dev`), generates
typed clients (`wreath typegen`), queries a running server's telemetry
(`wreath inspect`, a client of the [Inspector](inspector.md) socket), and
replays recordings through the owned pipeline (`wreath replay`). Use
`wreath --help` for the full flag list; run options map onto
[`ServerConfig`](server.md).

`wreath replay transport MODULE:APP recording.wtr1` feeds a recorded connection
into the owned HTTP/1 driver; `--inject schedule.wfs1` applies a fault schedule
first. `wreath replay plan MODULE:APP --path /users --method GET` runs a canonical
request through routing, binding, validation, and serialization. Unlike `inspect`
and `capture`, `replay` loads the application because it drives the app's own code
in-process — over fake transports only. See
[Replay and fault injection](replay.md) and the cookbook recipe
[Fuzz your own routes](../cookbook/recipes/fuzz-your-routes.md).

::: wreath.cli
