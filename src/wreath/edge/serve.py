"""Put the native proxy on a port. The only Python a forwarded request meets.

Everything here runs once, while the proxy is being configured: URLs are parsed,
the upstream table is compiled, the connections to each origin are opened, and a
listening socket is bound. After that a request is handled entirely inside
`wreath._native._edge` -- parsed, transformed, written upstream and relayed back
-- without a scope, a `Request`, a coroutine or a Task.

    from wreath.edge import Upstream, UpstreamPool, serve

    pool = UpstreamPool([Upstream("http://10.0.0.4:8000")])
    handle = await serve(pool, host="0.0.0.0", port=8080)

**There is no `app` parameter, and its absence is the design.** An ASGI app is
the seam Python returns through: give the proxy something to call and a scope
has to be built to call it with, and the 117 CPU-microseconds per forwarded
request that this exists to remove come straight back. Having nothing to call is
what makes "no Python on the request path" structural rather than aspirational.

The upstream connections are established here rather than per request because
`loop.create_connection` is a coroutine, and reaching for one mid-request drags
asyncio's Task and Future machinery back onto the path -- 6.3us for the Task
alone, before any of the orchestration around it.

## What this does not do yet

* **No retry onto a second upstream** -- see below. TLS is supported in both
  directions: `ssl=` terminates it from the client, and an `https://` upstream
  gets a native outbound handshake, paid once at configuration time along with
  the rest of the pre-warm.
* **No retry onto a second upstream.** A failed attempt is a 502; the pool's
  passive health still ejects the origin, so the next request goes elsewhere.
* **No upgrade.** `Upgrade` is hop-by-hop and dropped, so a WebSocket handshake
  reaches the origin as an ordinary request.
* **Request bodies are buffered** under `max_body` and refused above it, which
  is the same bound `ReverseProxy` documents.
* **HTTP/1.1 on both sides.**

`ReverseProxy` remains for the cases those bullets rule out. It is the ASGI
proxy -- slower by construction, and able to sit inside an application.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from .._native import _edge
from .headers import via_token
from .proxy import DEFAULT_MAX_BODY, DEFAULT_VIA_NAME
from .upstream import UpstreamPool

#: Connections opened to each origin before the listener is bound.
#:
#: This is the proxy's concurrency per upstream: a request that arrives while
#: every connection is busy is queued in C until one frees. Eight is chosen to be
#: unremarkable -- enough that a single slow origin response does not stall the
#: next seven requests, few enough that a pool of origins does not open hundreds
#: of sockets before serving anything.
DEFAULT_CONNECTIONS = 8
DEFAULT_MAX_WAITING = 1024
DEFAULT_QUEUE_TIMEOUT = 30.0

#: How long a lost upstream connection waits before it is replaced. Reconnection
#: is the one place `serve()` schedules a task, and it is off the request path by
#: construction: it happens when a connection *dies*, never when one is used.
_REOPEN_DELAY = 0.25


def _endpoint(url: str) -> tuple[str, int, bytes, bool]:
    """Split an upstream URL into what the connect and the `Host` header need."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            f"wreath.edge.serve() speaks http:// or https:// to upstreams, not "
            f"{parts.scheme!r} ({url!r})."
        )
    if not parts.hostname:
        raise ValueError(f"upstream URL has no host: {url!r}")
    if (
        parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"upstream must be an origin URL such as 'https://example.com:443', "
            f"without credentials, a path, query, or fragment; got {url!r}"
        )
    secure = parts.scheme == "https"
    port = parts.port or (443 if secure else 80)
    authority = parts.netloc.encode("latin-1")
    return parts.hostname, port, authority, secure


class EdgeHandle:
    """A running native proxy: the listener, and the table behind it.

    Returned by `serve()`. Holds no per-request state -- everything a request
    touches lives in the C table -- so this is the configuration-time object and
    nothing more.
    """

    __slots__ = (
        "_closed",
        "_connections",
        "_endpoints",
        "_reopen_tasks",
        "_server",
        "_table",
    )

    def __init__(
        self,
        server: asyncio.Server,
        table: Any,
        endpoints: list[tuple[str, int, bytes, bool]],
        connections: int,
        reopen_tasks: set[asyncio.Task[None]],
    ) -> None:
        self._server = server
        self._table = table
        self._endpoints = endpoints
        self._connections = connections
        self._reopen_tasks = reopen_tasks
        self._closed = False

    @property
    def sockets(self) -> tuple[Any, ...]:
        """The listening sockets, for a caller that bound port 0."""
        return tuple(self._server.sockets or ())

    @property
    def closed(self) -> bool:
        """Whether `aclose()` has run. Read by the reconnect loop."""
        return self._closed

    def upstream_connections(self) -> int:
        """Upstream connections currently established."""
        return self._table.connections()

    def stats(self) -> dict[str, int]:
        """Live counters. `dict[str, int]`, per the naming rules."""
        return self._table.stats()

    async def aclose(self) -> None:
        """Stop accepting, then close every client and upstream connection."""
        if self._closed:
            return
        self._closed = True
        # The table first, and closing before the listener: it sets the flag
        # that stops a dying connection from scheduling its own replacement,
        # which would otherwise race the shutdown it is being torn down by.
        self._table.close()
        for task in self._reopen_tasks:
            task.cancel()
        if self._reopen_tasks:
            await asyncio.gather(*self._reopen_tasks, return_exceptions=True)
            self._reopen_tasks.clear()
        self._server.close()
        await self._server.wait_closed()


async def serve(
    pool: UpstreamPool,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    via_name: str = DEFAULT_VIA_NAME,
    connections: int = DEFAULT_CONNECTIONS,
    max_body: int = DEFAULT_MAX_BODY,
    max_waiting: int = DEFAULT_MAX_WAITING,
    queue_timeout: float = DEFAULT_QUEUE_TIMEOUT,
    backlog: int = 2048,
    reuse_port: bool = False,
    ssl: Any = None,
    upstream_cafile: str | None = None,
    upstream_verify: bool = True,
) -> EdgeHandle:
    """Bind a native reverse proxy for `pool` and return its handle.

    Args:
        pool: The origins to forward to, and the policy for choosing between
            them. Only the upstream URLs, the policy and the ejection settings
            are read; they are compiled into a C table and the pool is not
            consulted again.
        host: Address to listen on.
        port: Port to listen on. `0` binds an ephemeral one; read it back from
            `handle.sockets`.
        via_name: The token this hop writes into `Via`.
        connections: Connections opened to each origin before the listener is
            bound. This is the per-upstream concurrency.
        max_body: Largest request body relayed; over it, 413.
        max_waiting: Requests allowed to wait for a connection per upstream.
        queue_timeout: Seconds a request may wait for an upstream connection.
        backlog: Listen backlog.
        reuse_port: Bind with `SO_REUSEPORT`, so several workers can share the
            port and each keep its own table.
        ssl: A server `ssl.SSLContext` to terminate TLS with. Pass
            `wreath.reactor.metal_tls_context(...)` on the metal loop and the
            crypto runs in C, keeping the native transport; an ordinary
            `SSLContext` still works and takes asyncio's path, which measured
            2.14x slower per request.
        upstream_cafile: PEM bundle of roots trusted for `https://` upstreams.
            None uses the system store.
        upstream_verify: Check each `https://` upstream's chain and host name.
            Off has to be typed out.

    Returns:
        An `EdgeHandle`. Close it with `await handle.aclose()`.

    Raises:
        ValueError: An upstream URL this cannot speak to -- see the module
            docstring for what is not supported yet. Raised here, at
            configuration time, rather than on the request that first selects it.
    """
    if connections < 1:
        raise ValueError("connections must be at least 1")
    if max_body < 0:
        raise ValueError("max_body must be non-negative")
    if max_waiting < 1:
        raise ValueError("max_waiting must be at least 1")
    if queue_timeout <= 0:
        raise ValueError("queue_timeout must be positive")
    if backlog < 1:
        raise ValueError("backlog must be at least 1")
    endpoints = [_endpoint(u.url) for u in pool.upstreams]
    loop = asyncio.get_running_loop()
    ejection = pool.ejection
    handle: EdgeHandle | None = None
    closing = False
    reopen_tasks: set[asyncio.Task[None]] = set()
    # One context for every https upstream, built while the proxy is being
    # configured. Native where the reactor can provide it, so the handshake and
    # every record afterwards run in C on the same transport the inbound side
    # uses.
    upstream_tls = None
    if any(secure for *_rest, secure in endpoints):
        from ..reactor import metal_tls_client_context

        upstream_tls = metal_tls_client_context(cafile=upstream_cafile, verify=upstream_verify)

    def handle_closed() -> bool:
        return closing or (handle is not None and handle.closed)

    async def reopen(index: int) -> None:
        """Replace one lost upstream connection, forever, until the proxy stops.

        A backoff rather than an immediate retry: an origin that has just gone
        away will refuse the next connection too, and eight sockets reconnecting
        in a tight loop is a proxy attacking its own upstream.
        """
        upstream_host, upstream_port, _authority, secure = endpoints[index]
        while not handle_closed():
            await asyncio.sleep(_REOPEN_DELAY)
            try:
                await loop.create_connection(
                    lambda: _edge.UpstreamConnection(table, index),
                    upstream_host,
                    upstream_port,
                    ssl=upstream_tls if secure else None,
                    server_hostname=upstream_host if secure else None,
                )
            except OSError:
                continue
            return

    def on_lost(index: int) -> None:
        """Called from C when an upstream connection dies.

        This is the only `create_task` anywhere near the proxy, and it is on the
        failure path by construction: a connection that is *used* never reaches
        here.
        """
        if handle_closed():
            return
        task = loop.create_task(reopen(index))
        reopen_tasks.add(task)
        task.add_done_callback(reopen_tasks.discard)

    table = _edge.UpstreamTable(
        [authority for _host, _port, authority, _secure in endpoints],
        via_token("1.1", via_name),
        b"https" if ssl is not None else b"http",
        policy=pool.policy,
        eject_failures=ejection.failures,
        eject_seconds=ejection.seconds,
        eject_cap=ejection.cap,
        max_body=max_body,
        on_lost=on_lost,
        max_waiting=max_waiting,
        queue_timeout=queue_timeout,
        loop=loop,
    )

    try:
        for index, (upstream_host, upstream_port, _authority, secure) in enumerate(endpoints):
            for _ in range(connections):
                await loop.create_connection(
                    lambda i=index: _edge.UpstreamConnection(table, i),
                    upstream_host,
                    upstream_port,
                    ssl=upstream_tls if secure else None,
                    server_hostname=upstream_host if secure else None,
                )

        server = await loop.create_server(
            lambda: _edge.EdgeProtocol(table),
            host,
            port,
            backlog=backlog,
            reuse_port=reuse_port,
            ssl=ssl,
            start_serving=True,
        )
    except BaseException:
        closing = True
        table.close()
        pending_reopens = tuple(reopen_tasks)
        for task in pending_reopens:
            task.cancel()
        if pending_reopens:
            await asyncio.gather(*pending_reopens, return_exceptions=True)
        reopen_tasks.clear()
        raise
    handle = EdgeHandle(server, table, endpoints, connections, reopen_tasks)
    return handle
