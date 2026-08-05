"""Wreath at the edge: a reverse proxy and an in-memory load balancer.

One process in front of your origins, doing what nginx plus a load balancer
usually do -- and doing it with the same server, the same client, the same
observability and the same configuration language as the applications behind
it. The point is not that wreath *can* proxy; it is that a small deployment
should not need a second stack, a second config format and a second set of logs
to put something on a port.

```python
from wreath import Wreath, Request
from wreath.edge import ReverseProxy, Upstream, UpstreamPool
from wreath.http_client import HTTPClient

pool = UpstreamPool([Upstream("http://10.0.0.4:8000"), Upstream("http://10.0.0.5:8000")])
clients = {u.url: HTTPClient(u.url, base_url=u.url) for u in pool.upstreams}
proxy = ReverseProxy(pool, clients)

edge = Wreath()

@edge.get("/{path:path}")
async def relay(request: Request, path: str):
    return await proxy(request)
```

**Selection is memory, not PostgreSQL.** Upstream choice, health and in-flight
counts are read and written on the request path, so nothing here makes a
database call to decide where a request goes. Postgres is where *desired* state
belongs -- which upstreams should exist -- and that half is not built yet.

## What this does not do yet

Said plainly, because a proxy that is quiet about its gaps is the dangerous
kind:

* **Request bodies are buffered** under `DEFAULT_MAX_BODY` and refused above it.
  The response half streams; the request half needs the client's write path to
  accept an async iterator.
* **No WebSocket or other upgrade.** `Upgrade` is hop-by-hop and dropped, so an
  upgrade request reaches the origin as an ordinary one.
* **No TLS termination for more than one name.** `wreath.server`'s `TLSConfig`
  is a single certificate, so SNI selection across several hostnames is not
  available here yet.
* **One process.** Round-robin and peak-EWMA stay correct behind `SO_REUSEPORT`
  because each worker's view is independently valid; *global*
  least-connections would need shared state and does not have it.

See `docs/guides/edge.md` for the reasoning and
`docs/reference/edge.md` for the generated surface.
"""

from __future__ import annotations

from .headers import HOP_BY_HOP, forwardable
from .proxy import DEFAULT_ATTEMPTS, DEFAULT_MAX_BODY, IDEMPOTENT, ReverseProxy
from .serve import DEFAULT_CONNECTIONS, EdgeHandle, serve
from .upstream import Ejection, Upstream, UpstreamPool

__all__ = [
    "DEFAULT_ATTEMPTS",
    "DEFAULT_CONNECTIONS",
    "DEFAULT_MAX_BODY",
    "HOP_BY_HOP",
    "IDEMPOTENT",
    "EdgeHandle",
    "Ejection",
    "ReverseProxy",
    "Upstream",
    "UpstreamPool",
    "forwardable",
    "serve",
]
