---
description: Nine production-shaped Wreath stories, each organised around a difficult invariant.
keywords: examples architecture tutorials realtime agents MCP enterprise analytics edge offline
boost: 1.7
---

```hero
eyebrow: Nine systems · nine kinds of pressure
title: Start with the moment the easy architecture stops working.
lede: These are not feature tours. Each story builds a system, breaks one comfortable assumption, and keeps a concrete invariant true.
signal: contention
signal: disconnection
signal: retries
signal: identity lifecycle
action: Begin with the serious API -> serious-api.md
action: Or start with MCP -> mcp-control-room.md
```

```cards
label: Hero stories
card: Build the app they said was missing | Users, policy, data, jobs and MCP in one measured native application. | serious-api.md | users · security · native
card: Balance a live energy depot | Hold a live physical system inside hard power and ownership limits. | energy-depot.md | rooms · leases · series
card: Turn computers into an agent fleet | Route agentic work across a person's devices without running it twice. | agent-fleet.md | entities · jobs · streams
card: Let customers wire the world together | Give every signed event one durable, tenant-bound and replayable automation run. | automation-backplane.md | webhooks · workflows · hooks
card: Give an agent tools—safely | Let a model operate real systems through the same policy boundary as a person. | mcp-control-room.md | MCP · OAuth · audit
card: Land the enterprise | Treat SAML, SCIM, tenancy and support as one lifecycle. | enterprise.md | SSO · isolation · quotas
card: Ask better questions of time | Make time zones, late data and downsampling part of the model. | time-series-lab.md | temporal · analysis
card: Survive the noon drop | Explain one outcome after a storm of equivalent attempts. | noon-drop.md | edge · webhooks
card: Assume the network will fail | Resume bounded field data rather than starting over or syncing everything. | field-operations.md | shapes · uploads
```

## Read them as architectures

Every story uses the same rhythm:

1. See the finished system under pressure.
2. Name what must never happen.
3. Give each piece of state an explicit owner.
4. Add the production boundary: policy, tenancy, limits or durability.
5. Inspect and replay the outcome.

You can read a story without building it. When one matches the thing in your
head, follow its build path into the conventional documentation.
