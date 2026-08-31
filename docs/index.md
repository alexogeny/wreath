---
description: Build contemporary systems that stay coherent under contention, retries and failure.
keywords: Python ASGI framework realtime jobs MCP SAML SCIM PostgreSQL WebSockets
boost: 2
---

```hero
eyebrow: Wreath 0.4.0 · Python 3.14 · ASGI
title: Build the systems that become somebody's operating surface.
lede: Realtime rooms, durable work, PostgreSQL, policy and a native server belong to one application—not a week of integration before the interesting work begins.
signal: realtime under contention
signal: durable work
signal: governed AI
signal: hard tenant boundaries
action: Meet the nine systems -> stories/index.md
action: Start with one route -> start/index.md
wide: true
```

```cards
label: Nine systems built under pressure
wide: true
card: Build the app they said was missing | Start with users and hard security boundaries, then inspect how much of the serious request path stays native. | stories/serious-api.md | users · security · measured
card: Balance a live energy depot | Coordinate microgrids, vehicles and chargers without overcommitting the power that remains. | stories/energy-depot.md | realtime · concurrency
card: Turn computers into an agent fleet | Assign long-running work across every device a person owns and survive a device disappearing. | stories/agent-fleet.md | devices · durable work
card: Let customers wire the world together | Turn signed events, schedules and user hooks into durable, inspectable automation runs. | stories/automation-backplane.md | webhooks · workflows · replay
card: Give an agent tools—safely | Put a governed MCP boundary around real operations instead of building a second, weaker backend. | stories/mcp-control-room.md | MCP · policy · progress
card: Land the enterprise | Carry a tenant from SAML and SCIM provisioning through isolation, support and deprovisioning. | stories/enterprise.md | tenancy · SSO · SCIM
card: Ask better questions of time | Analyse irregular series honestly across missing samples, late arrivals and daylight-saving boundaries. | stories/time-series-lab.md | temporal · series
card: Survive the noon drop | Keep scarce inventory coherent through flash traffic, retries and webhook redelivery. | stories/noon-drop.md | edge · idempotency
card: Assume the network will fail | Resume field sync and large uploads without exposing data outside the operator's assignment. | stories/field-operations.md | sync · objects · place
```

## One system. One obvious home.

A production service needs a router. It also needs validation, identity, data access,
background work, observability and somewhere to run. Wreath owns those parts and
keeps the framework usable on any conforming ASGI server.

```python title="app.py"
from wreath import Request, Wreath

app = Wreath()


@app.get("/hello/{name}")
async def hello(request: Request, name: str) -> dict:
    return {"hello": name}
```

```bash
uv add wreath
uv run wreath dev app:app
```

The first route is deliberately ordinary. The interesting part begins when the
requirements stop being ordinary: only one worker may own a device; a job must
survive a process; an enterprise identity provider removes a person; a model may
read freely but must involve a human before it deletes.

This site documents **Wreath 0.4.0**. Check the
[version, platform and upgrade contract](start/releases.md) before choosing an extra
or moving an existing application between releases.

## The contract is the product

Each story starts with a visible system, introduces contention or failure, and
then names the invariant that must continue to hold. That is the recurring Wreath
idea: express the boundary early, refuse half-supported shapes, and leave enough
evidence to explain the outcome afterward.

[Choose a story](stories/index.md) or [build the first application](start/index.md).
