"""Header hygiene for a proxy. The part that is a checklist, not a design.

A reverse proxy is a *recipient* on one connection and a *sender* on another,
and RFC 9110 is explicit that some fields describe the connection rather than
the message. Forwarding one of those is how a proxy leaks a client's
`Connection: close` into a pooled upstream, or lets a client name a field that
the next hop then strips from someone else's request.
"""

from __future__ import annotations

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
    `ProxyHeadersMiddleware`, which is the thing behind this proxy. Emitting
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


def request_headers(
    inbound: list[tuple[bytes, bytes]],
    *,
    client: str | None,
    scheme: bytes,
    via: bytes,
) -> list[tuple[bytes, bytes]]:
    """The outbound request headers, built in a single pass.

    Fused deliberately. The readable version -- filter, then `next()` for the
    host, then a comprehension for the `Via` chain, then append -- walks the
    inbound headers five times and copies them once, and an ablation put that at
    roughly a third of the proxy's whole request cost. Emitting five headers
    should not cost more than talking to the upstream.

    `via` and `scheme` arrive pre-encoded because they are constant for the life
    of the proxy; building them per request was an f-string and an `encode` on
    the hot path for a value that never changes.

    Shares `_ALWAYS_DROP` and `_connection_named` with `forwardable`, which
    still serves the *response* direction. The two are separate because the
    directions genuinely differ -- a response has no `Host` to rewrite and no
    forwarding record to add -- not because the rule was written twice.
    """
    out: list[tuple[bytes, bytes]] = []
    host: bytes | None = None
    connection: bytes | None = None
    chain: list[bytes] = []
    for pair in inbound:
        name = pair[0]
        if name == b"host":
            host = pair[1]
        elif name == b"connection":
            connection = pair[1]
        elif name == b"via":
            chain.append(pair[1])
        elif name not in _REQUEST_DROP:
            out.append(pair)
    if connection is not None:
        named = {
            token.strip().lower()
            for token in connection.split(b",")
            if token.strip() not in (b"", b"close", b"keep-alive")
        }
        if named:
            out = [pair for pair in out if pair[0] not in named]

    if client is not None:
        encoded = client.encode("latin-1")
        out.append((b"x-forwarded-for", encoded))
        forwarded = b'for="' + encoded + b'"; proto=' + scheme
    else:
        forwarded = b"proto=" + scheme
    out.append((b"x-forwarded-proto", scheme))
    if host is not None:
        out.append((b"x-forwarded-host", host))
        forwarded += b'; host="' + host + b'"'
    out.append((b"forwarded", forwarded))
    out.append((b"via", b", ".join([*chain, via]) if chain else via))
    return out
