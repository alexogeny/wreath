"""`wreath._native._edge.request_headers`: the outbound header transform, in C.

The first piece of `wreath.edge`'s request path to move out of Python. It is
first because it is the largest: an ablation put the header build at roughly a
third of the proxy's whole per-request cost, and the decomposition put
`wreath.edge`'s own work at 44.4 of 117 CPU-microseconds.

It is also the piece whose contract is already pinned. Every assertion here
about *what* the transform must produce traces to
`tests/test_edge_forwarding_contract.py`, which was written from a differential
run against haproxy and nginx while the Python implementation still existed to
check against. This file asserts the C agrees, one property at a time.

Per `AGENTS.md`, `wreath.edge` has no Python path by design -- so there is
nothing to compare against here, and these are direct assertions on behaviour.
"""

from __future__ import annotations

import importlib

import pytest

try:
    _edge = importlib.import_module("wreath._native._edge")
except ImportError:  # not built yet -- the expected state until the C lands
    _edge = None


class _Unbuilt:
    """Yielded before the extension exists; every use fails the test in-body.

    Deliberately not `importorskip`. The extension is *unbuilt*, not absent on
    this platform, and a skip there would report success for work nobody did --
    the third state `AGENTS.md` forbids. `tests/reactor/conftest.py` solved the
    same problem the same way while the reactor was being written. A real
    platform gate belongs here only once the extension ships and some platform
    genuinely cannot build it.
    """

    def __getattr__(self, name: str):
        raise AssertionError(
            "wreath._native._edge is not built -- implement "
            f"{name}() in src/wreath/_native/edge_headers.c; this spec line "
            "stays RED until then"
        )


_edge = _edge if _edge is not None else _Unbuilt()

VIA = b"1.1 wreath"
HTTP = b"http"


def names(pairs: list[tuple[bytes, bytes]]) -> list[bytes]:
    return [name for name, _ in pairs]


def value(pairs: list[tuple[bytes, bytes]], want: bytes) -> bytes | None:
    for name, val in pairs:
        if name == want:
            return val
    return None


def test_an_ordinary_header_survives_in_order() -> None:
    """What is not hop-by-hop, owned, or reframed passes through untouched.

    Order matters and is asserted: a proxy that reorders headers is altering a
    message it was asked to relay, and some origins parse positionally.
    """
    out = _edge.request_headers(
        [(b"x-a", b"1"), (b"x-b", b"2"), (b"x-c", b"3")],
        client="203.0.113.7", scheme=HTTP, via=VIA)
    survivors = [n for n in names(out) if n.startswith(b"x-") and n != b"x-forwarded-for"
                 and not n.startswith(b"x-forwarded")]
    assert survivors == [b"x-a", b"x-b", b"x-c"]


def test_a_duplicated_header_keeps_both_values() -> None:
    """Coalescing two fields into one changes the message. Both survive."""
    out = _edge.request_headers(
        [(b"x-a", b"1"), (b"x-a", b"2")], client=None, scheme=HTTP, via=VIA)
    assert [v for n, v in out if n == b"x-a"] == [b"1", b"2"]


@pytest.mark.parametrize("hop", [
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailer", b"transfer-encoding", b"upgrade",
])
def test_a_hop_by_hop_field_is_never_forwarded(hop: bytes) -> None:
    """RFC 9110 7.6.1, the fixed list. Parametrised so a gap names itself."""
    out = _edge.request_headers(
        [(hop, b"whatever"), (b"x-keep", b"1")], client=None, scheme=HTTP, via=VIA)
    assert hop not in names(out)
    assert b"x-keep" in names(out)


def test_a_field_named_by_connection_is_dropped_for_this_message() -> None:
    """`Connection: keep-alive, x-hop` makes `x-hop` hop-by-hop here.

    The case both oracles get wrong -- haproxy 3.4.3 and nginx 1.30.4 both
    forward `x-hop` -- so there is no implementation to copy and the RFC is the
    only authority. See `test_edge_forwarding_contract.py`.
    """
    out = _edge.request_headers(
        [(b"connection", b"keep-alive, x-hop"), (b"x-hop", b"secret"),
         (b"x-keep", b"1")],
        client=None, scheme=HTTP, via=VIA)
    assert b"x-hop" not in names(out)
    assert b"x-keep" in names(out)


def test_connection_close_and_keep_alive_are_not_treated_as_field_names() -> None:
    """`close` and `keep-alive` are connection options, not fields to drop.

    Asserted case-insensitively. The Python original compared the *unlowered*
    token against `close`/`keep-alive` while lowering it before use, so
    `Connection: CLOSE` added a phantom `close` to the drop set -- harmless,
    since no field is named `close`, but inconsistent with `_connection_named`
    in the same module. The C resolves it the way that function already did.
    """
    out = _edge.request_headers(
        [(b"connection", b"CLOSE, Keep-Alive"), (b"close", b"1"),
         (b"keep-alive-ish", b"2")],
        client=None, scheme=HTTP, via=VIA)
    assert b"close" in names(out)
    assert b"keep-alive-ish" in names(out)


def test_a_client_supplied_forwarded_for_is_replaced() -> None:
    """The spoof this exists to stop: the client does not write the audit trail."""
    out = _edge.request_headers(
        [(b"x-forwarded-for", b"203.0.113.9")],
        client="198.51.100.4", scheme=HTTP, via=VIA)
    assert value(out, b"x-forwarded-for") == b"198.51.100.4"
    assert [v for n, v in out if n == b"x-forwarded-for"] == [b"198.51.100.4"]


def test_host_and_content_length_are_dropped_for_the_sender_to_rewrite() -> None:
    """Both belong to the outbound message, and the codec refuses a stale one."""
    out = _edge.request_headers(
        [(b"host", b"front.example"), (b"content-length", b"5")],
        client=None, scheme=HTTP, via=VIA)
    assert b"host" not in names(out)
    assert b"content-length" not in names(out)


def test_the_inbound_host_becomes_x_forwarded_host_and_the_forwarded_record() -> None:
    out = _edge.request_headers(
        [(b"host", b"front.example")], client="203.0.113.7", scheme=HTTP, via=VIA)
    assert value(out, b"x-forwarded-host") == b"front.example"
    assert value(out, b"forwarded") == b'for="203.0.113.7"; proto=http; host="front.example"'


def test_the_forwarded_record_omits_for_when_the_peer_is_unknown() -> None:
    out = _edge.request_headers([], client=None, scheme=HTTP, via=VIA)
    assert value(out, b"forwarded") == b"proto=http"
    assert b"x-forwarded-for" not in names(out)


def test_via_is_appended_to_the_chain_rather_than_replacing_it() -> None:
    """`Via` is topology and loop detection; the chain is the whole value.

    The deliberate asymmetry with `x-forwarded-for` above, which is replaced.
    """
    out = _edge.request_headers(
        [(b"via", b"1.1 alpha"), (b"via", b"1.1 beta")],
        client=None, scheme=HTTP, via=VIA)
    assert value(out, b"via") == b"1.1 alpha, 1.1 beta, 1.1 wreath"


def test_via_is_written_even_with_no_inbound_chain() -> None:
    out = _edge.request_headers([], client=None, scheme=HTTP, via=VIA)
    assert value(out, b"via") == VIA


def test_a_non_bytes_header_is_refused_rather_than_coerced() -> None:
    """A `str` name would silently never match a drop rule and be forwarded.

    Refusing is the only safe answer: this function's entire job is deciding
    what not to send, and a name it cannot compare is a name it cannot filter.
    """
    with pytest.raises(TypeError):
        _edge.request_headers([("x-a", b"1")], client=None, scheme=HTTP, via=VIA)
    with pytest.raises(TypeError):
        _edge.request_headers([(b"x-a", "1")], client=None, scheme=HTTP, via=VIA)


def test_a_malformed_pair_is_refused() -> None:
    with pytest.raises((TypeError, ValueError)):
        _edge.request_headers([(b"x-a",)], client=None, scheme=HTTP, via=VIA)
