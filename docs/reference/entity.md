# `wreath.entity`

One name, one owner, one mailbox. `Ownership` is a leased, fenced claim on a
*name* — which worker holds `device:abc` right now — and `EntityRegistry.ask`
puts a question to whoever holds one, from a worker that does not know which one
that is.

It exists for the shape every stateful gateway has: a socket, a session or a
device that lives on exactly one worker, addressed by a request that arrives on
any of them. Without it that is three hand-written subsystems — a per-connection
channel on a broker, an ownership table with a heartbeat, and a correlation map
with a timeout. See [Entities and addressing](../guides/entities.md) for the
tour, and [Distributed locks](../guides/distributed-locks.md) for
`SingletonRunner`, which this deliberately does **not** replace: an advisory lock
releases the instant its connection drops, which is a better failure detector
than a lease and costs a held connection per name.

::: wreath.entity
