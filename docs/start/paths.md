---
description: Choose the Wreath learning path that matches the pressure in your application.
keywords: learning path guides architecture routing database realtime jobs auth MCP tenancy
---

```hero
eyebrow: Start · choose by pressure
title: Learn the part your application needs next.
lede: Wreath is broad. Your first useful path should be narrow: one real requirement, one owner for its state, and one failure you can reproduce.
signal: requests
signal: data
signal: realtime
signal: identity
action: Return to the quickstart -> index.md
action: Open the surface map -> ../reference/index.md
```

## I am serving an API

Begin with `wreath.app`, `wreath.router`, `wreath.binding`, `wreath.request` and
`wreath.response`. Add request policy only when you can name the boundary it enforces.
The [first application](index.md) gives you a complete vertical slice.

## I need data and analysis

Begin with `wreath.postgres`, the `wreath.orm` package, `wreath.temporal` and
`wreath.series`. Make transaction ownership and calendar semantics explicit before
adding presentation. The [time-series laboratory](../stories/time-series-lab.md) shows
why both decisions surface quickly.

## I need realtime or background work

Choose by lifetime:

| Work | Start with |
|---|---|
| present only while people are connected | `wreath.websocket`, `wreath.rooms` |
| output that should resume | `wreath.streams`, `wreath.progress` |
| work that must survive a process | `wreath.jobs` |
| several durable steps | `wreath.workflows` |
| one live owner for an addressed thing | `wreath.entity` |

The [agent fleet](../stories/agent-fleet.md) combines all five without confusing their
lifetimes.

## I need identity and enterprise tenancy

Begin with principal identity and tenant resolution, then add the protocol your
customer uses. `wreath.users`, `wreath.auth`, `wreath.authorization` and
`wreath.tenancy` own the application boundary. `wreath.saml`, `wreath.sso` and the
SCIM router connect it to an enterprise directory. Follow the
[enterprise lifecycle](../stories/enterprise.md).

## I need an AI-native boundary

Use `wreath.mcp` when a model should reach selected application operations. Keep the
same schema, OAuth resource, authorization decision, progress mechanism and recording
policy that the rest of the service uses. The
[MCP control room](../stories/mcp-control-room.md) starts with a read operation and
ends at a deliberate human step-up.

## I need the server and edge

The framework remains ASGI. Use `wreath.server` when you want Wreath's native HTTP
stack and `wreath.edge` when you need the native reverse-proxy path. Add traffic,
admission, deadline, cache and compression policy from `wreath.policy` as named
operational decisions. The [noon drop](../stories/noon-drop.md) shows those decisions
working together.
