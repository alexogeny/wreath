"""Trusted forwarding-header handling for deployments behind a proxy.

Behind a TLS-terminating proxy the connection Wreath actually accepts is plaintext
HTTP from the proxy, so the scope says `scheme == "http"` and the peer address
is the proxy's. Middleware downstream believes it, and two things fail quietly:
`SecurityHeadersMiddleware` gates HSTS on an HTTPS scheme and emits nothing, and
`CSRFMiddleware` builds its expected origin as `http://host` while the browser
sends `https://host` and rejects every unsafe request.

This middleware restores the truth, but only from proxies that were explicitly
configured:

```python
app.add_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]), priority=-30)
```
Forwarding headers are trivially forged by any client, so nothing is trusted by
default and the allow-list has no wildcard: `trusted` is required and must name
the proxy networks. Run this before anything that reads scheme, host, or client
-- a negative priority puts it first.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .._native import _core
from ..request import Request

if _core is not None and hasattr(_core, "TrustedNetworks"):
    TrustedNetworks: Any = _core.TrustedNetworks
else:  # pragma: no cover - exercised by the WREATH_PURE test matrix
    from .._pure.proxy import TrustedNetworks

_SCHEMES = frozenset({"http", "https"})


class ProxyHeadersMiddleware:
    """Apply X-Forwarded-* headers, but only from configured proxy networks.

    Mount this **only** when a proxy you control sits in front of the
    application and overwrites these headers on the way in. Every one of them is
    client-supplied and trivially forged; the peer address is the only thing
    that cannot be, which is why the allow-list is on the peer and not on the
    header. There is no wildcard: `trusted` is required and empty is refused.

    Three corrections, all skipped entirely when the immediate peer is outside
    `trusted`. `X-Forwarded-For` sets `Request.client` to the rightmost hop that
    is not itself a trusted proxy, or to the leftmost when every hop is one, and
    always with a `None` port -- the port on the connection belongs to the proxy
    and no forwarding header carries the client's. A chain containing one hop
    that does not parse leaves `Request.client` alone entirely, because a
    malformed chain puts the boundary between forged and vouched-for hops out of
    reach. `X-Forwarded-Proto` sets `Request.scheme`, taking the first entry of a
    chain and accepting only `http` or `https`. `X-Forwarded-Host` overwrites the
    `Host` *header* rather than a scope entry, because `TrustedHostMiddleware`
    and `CSRFMiddleware` read the header.

    Mount it before anything that reads scheme, host, or client -- a negative
    priority puts it first. Without it, `SecurityHeadersMiddleware` sees an
    `http` scheme and emits no HSTS, and `CSRFMiddleware` builds an expected
    origin of `http://host` while the browser sends `https://host` and refuses
    every unsafe request. Both failures are silent.

    Args:
        trusted: Proxy addresses or CIDR networks whose forwarding headers count.
        trust_proto: Apply `X-Forwarded-Proto` to the request scheme.
        trust_host: Apply `X-Forwarded-Host` to the request `Host` header.

    Raises:
        ValueError: `trusted` is empty.
        ValueError: An entry is not a valid address or strict CIDR network.
        TypeError: An entry is not a string.
    """

    global_scope = True
    __slots__ = ("_networks", "_trust_host", "_trust_proto")

    def __init__(
        self,
        *,
        trusted: Iterable[str],
        trust_proto: bool = True,
        trust_host: bool = True,
    ) -> None:
        networks = tuple(trusted)
        if not networks:
            raise ValueError(
                "trusted proxy networks must not be empty; forwarding headers are "
                "client-supplied and are only meaningful from a known proxy"
            )
        # Compiled once: the per-request path never parses a CIDR.
        self._networks = TrustedNetworks(networks)
        self._trust_proto = trust_proto
        self._trust_host = trust_host

    def _peer_trusted(self, request: Request) -> bool:
        client = request.client
        if not client:
            return False
        return bool(self._networks.contains(str(client[0])))

    async def before(self, request: Request) -> None:
        """Rewrite client, scheme, and Host from a trusted peer's forwarding headers.

        Always returns None; this hook never short-circuits. A request whose
        immediate peer is not in `trusted` passes through with nothing changed,
        which is also what happens when the peer address is unavailable.
        """
        if not self._peer_trusted(request):
            return None
        # One index rather than three scans of the header list. The index is
        # request-scoped and shared, so CSRF and the auth backend reuse it
        # instead of rescanning; measured at ~3x cheaper than repeated
        # per-name lookups once a request performs more than a couple.
        headers = request._index_headers()

        forwarded_for = headers.get(b"x-forwarded-for")
        if forwarded_for is not None:
            client = self._networks.forwarded_client(forwarded_for)
            if client is not None:
                # The port belongs to the proxy hop, not the client, and no
                # forwarding header carries the original one. Written through
                # the request so a native context never materializes its ASGI
                # scope just to carry the override.
                request._set_client((client, None))

        if self._trust_proto:
            proto = headers.get(b"x-forwarded-proto")
            if proto is not None:
                # A proxy chain may append: the first entry is the client's.
                value = proto.split(b",", 1)[0].strip().decode("latin-1").lower()
                if value in _SCHEMES:
                    request._set_scheme(value)

        if self._trust_host:
            host = headers.get(b"x-forwarded-host")
            if host is not None:
                value = host.split(b",", 1)[0].strip()
                # TrustedHostMiddleware and CSRFMiddleware read the Host header
                # rather than the scope, so the override has to land there.
                if value:
                    request._set_header(b"host", value)
        return None


__all__ = ["ProxyHeadersMiddleware"]
