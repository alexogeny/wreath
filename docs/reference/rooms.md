# `wreath.rooms`

Named groups of WebSockets with cross-worker broadcast over the PostgreSQL
message bus.

`broadcast(grade=..., render=...)` delivers **one event at each subscriber's own
authorization outcome** — two watchers of the same incident seeing a position at
different precisions, because the authorizer graded them differently. The cheap
half runs per socket and the expensive half once per distinct grade, and the
grade is re-read on every broadcast so a revoked grant takes effect on the next
event rather than when the connection happens to reconnect. See
[Authentication and authorization](../guides/auth.md) for the ladder that
produces those grades.

::: wreath.rooms
