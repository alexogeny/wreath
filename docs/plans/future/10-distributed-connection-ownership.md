# Distributed connection ownership plan

## Status

Future proposal; depends on supervised services, observability, reliable commands, and the WebSocket operational layer.

## Objective

Allow multiple Neo application replicas to accept persistent client connections while commands and presence queries find the current owner safely. Do not hide the distributed nature of ownership or claim perfect presence during partitions.

## Model

A logical peer has at most one current ownership lease for an application-defined connection key. The owner record includes process/instance ID, connection generation, fencing token, lease expiry based on database time, protocol metadata, and bounded routing information. The local process maintains the live socket object; PostgreSQL or a configured coordination system stores only ownership metadata.

Every accepted connection receives a monotonically newer generation/fence. Commands routed through an old owner or old connection generation are rejected. Duplicate live connections follow explicit application policy: reject new, replace old, or permit distinct session keys.

## Routing

Local delivery uses the process connection registry. Remote delivery writes durable command intent and sends a bounded wake-up notification to the recorded owner. Wake-ups may use broker, PostgreSQL notification, or polling; they are hints, not durability. The owner always re-reads persisted command state.

If no valid owner exists, the command remains pending until reconnection, expiry, or application policy chooses another transport. Routing never drops durable intent because a presence hint was stale.

## Failure semantics

- Owner heartbeat extends a lease only for the current fence.
- Process death allows takeover after expiry.
- Network partition can temporarily produce stale beliefs; fencing prevents stale state mutation.
- Reconnection creates a new generation and invalidates old acknowledgements.
- Graceful drain marks the instance draining, rejects or redirects new connections, and gives current peers a bounded close/reconnect interval.
- Presence is reported with observation time and confidence, not as an eternal boolean.

## C and pure split

Python owns socket objects, database transactions, takeover policy, command routing, and callbacks. C may own a compact local connection registry, generation checks, bounded per-connection queues, and lookup indexes. The pure registry has identical admission, replacement, and close behavior. Durable ownership remains external to both.

Native registry operations must account for free-threading and cannot call user code while holding internal locks.

## Phases

1. Single-process registry and generation semantics.
2. PostgreSQL lease/fence schema and takeover protocol.
3. Durable command routing with polling wake-ups.
4. Optional broker/notification wake-up adapter.
5. Graceful instance drain and deployment integration.
6. Partition, reconnect-storm, contention, and soak testing.

## Verification

Test simultaneous connects, rapid reconnect, stale acknowledgement, owner death, delayed heartbeat, database outage, lost/duplicate wake-up, process drain, queue overflow, and many replicas claiming one key. Use deterministic state-machine tests plus real multi-process integration tests.

## Completion criteria

No stale owner can finalize work for a newer connection generation, wake-up loss cannot lose a durable command, all local queues are bounded, and operators can identify owners, lease age, generation, backlog, and drain state.

## Risks

Connection ownership can become a home-grown consensus system. Leases and fencing intentionally provide a narrower contract; workloads requiring stronger consensus should use a dedicated coordinator.
