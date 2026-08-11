"""Header hygiene for a proxy. The part that is a checklist, not a design.

A reverse proxy is a *recipient* on one connection and a *sender* on another,
and RFC 9110 is explicit that some fields describe the connection rather than
the message. Forwarding one of those is how a proxy leaks a client's
`Connection: close` into a pooled upstream, or lets a client name a field that
the next hop then strips from someone else's request.
"""

from __future__ import annotations

from wreath._native import _edge

#: RFC 9110 §7.6.1. Never forwarded in either direction.
HOP_BY_HOP: frozenset[bytes] = frozenset({
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
})

#: Fields this proxy writes itself. A client that sends one does not get to
#: decide what the next hop believes: `x-forwarded-for` is an audit trail and an
#: authorization input in many deployments, so an inbound value is *replaced*,
#: never appended to. Appending is the classic spoof -- the attacker writes the
#: first element and every parser that reads "the client" reads theirs.
OWNED: frozenset[bytes] = frozenset({
    b"forwarded",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-forwarded-proto",
    b"via",
})


#: Everything dropped on every message, unioned once at import. The
#: per-request union used to be `HOP_BY_HOP | _connection_named(...) | OWNED`,
#: which built three sets and two unions for a request that almost never names
#: a field in `Connection`.
_ALWAYS_DROP: frozenset[bytes] = HOP_BY_HOP | OWNED


def _connection_named(headers: tuple[tuple[bytes, bytes], ...]) -> frozenset[bytes]:
    """Every field name listed in a `Connection` header.

    `Connection: close, x-custom` makes `x-custom` hop-by-hop for this message.
    Missing this is a real forwarding bug and not a theoretical one: it is how a
    field meant for one hop reaches the origin.
    """
    named: set[bytes] = set()
    for name, value in headers:
        if name != b"connection":
            continue
        for token in value.split(b","):
            token = token.strip().lower()
            if token and token not in (b"close", b"keep-alive"):
                named.add(token)
    return frozenset(named)


def forwardable(
    headers: tuple[tuple[bytes, bytes], ...],
) -> list[tuple[bytes, bytes]]:
    """`headers` minus everything that belongs to the inbound connection.

    Order and duplicates are preserved for what survives, because a proxy that
    reorders or coalesces headers is changing a message it was asked to relay.
    """
    # `Connection` is scanned for first and the union skipped when it names
    # nothing, because that is the shape of nearly every request: one pass over
    # the headers instead of a set build and two unions on top of it.
    drop = _ALWAYS_DROP
    for name, _value in headers:
        if name == b"connection":
            named = _connection_named(headers)
            if named:
                drop = _ALWAYS_DROP | named
            break
    return [(name, value) for name, value in headers if name not in drop]


def via_token(version: str, name: str) -> bytes:
    """This proxy's `Via` element: the protocol it received, then who it is."""
    return f"{version} {name}".encode("latin-1")


def append_via(
    forwarded: list[tuple[bytes, bytes]],
    inbound: tuple[tuple[bytes, bytes], ...],
    token: bytes,
) -> None:
    """Append our element to any `Via` chain the client already carried.

    `Via` *is* appended where `x-forwarded-for` is replaced, and the difference
    is deliberate: `Via` is a loop-detection and topology record whose whole
    value is the chain, and it is not an authorization input anywhere.
    """
    existing = [value for name, value in inbound if name == b"via"]
    chain = b", ".join([*existing, token]) if existing else token
    forwarded.append((b"via", chain))


def append_forwarded(
    forwarded: list[tuple[bytes, bytes]],
    *,
    client: str | None,
    host: bytes | None,
    scheme: str,
) -> None:
    """Write the forwarding record, both the RFC 7239 form and the common one.

    Both, because RFC 7239 `Forwarded` is the standard and `X-Forwarded-*` is
    what almost everything actually reads -- including `wreath`'s own
    `ProxyPolicy`, which is the thing behind this proxy. Emitting
    only the standard one would mean wreath could not sit behind itself.
    """
    if client:
        forwarded.append((b"x-forwarded-for", client.encode("latin-1")))
    forwarded.append((b"x-forwarded-proto", scheme.encode("latin-1")))
    if host:
        forwarded.append((b"x-forwarded-host", host))
    parts = [f'proto={scheme}']
    if client:
        parts.insert(0, f'for="{client}"')
    if host:
        parts.append(f'host="{host.decode("latin-1")}"')
    forwarded.append((b"forwarded", "; ".join(parts).encode("latin-1")))


#: Names the request path drops in addition to `_ALWAYS_DROP`. `host` and
#: `content-length` are the client's to write -- see `ReverseProxy._outbound`.
_REQUEST_DROP: frozenset[bytes] = _ALWAYS_DROP | {b"host", b"content-length"}


#: The outbound request headers for one forwarded request, in a single pass.
#:
#: `request_headers(inbound, *, client, scheme, via) -> list[tuple[bytes, bytes]]`
#:
#: Native, and with nothing behind it. The Python this replaces was already
#: fused into one pass -- the readable version walked the inbound headers five
#: times and an ablation put that at roughly a third of the proxy's whole
#: request cost -- and it was still the largest single line item left, at 44.4
#: of 117 CPU-microseconds per forwarded request.
#:
#: `via` and `scheme` are passed pre-encoded because they are constant for the
#: life of the proxy. What it must produce is pinned by
#: `tests/test_edge_forwarding_contract.py`, written from a differential run
#: against haproxy and nginx, and asserted directly in
#: `tests/test_edge_native_headers.py`.
request_headers = _edge.request_headers
