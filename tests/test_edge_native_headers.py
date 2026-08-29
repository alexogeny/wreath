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
    out = _edge.request_headers(
        [(b"x-a", b"1"), (b"x-b", b"2"), (b"x-c", b"3")], client="203.0.113.7", scheme=HTTP, via=VIA
    )
    survivors = [
        n
        for n in names(out)
        if n.startswith(b"x-") and n != b"x-forwarded-for" and not n.startswith(b"x-forwarded")
    ]
    assert survivors == [b"x-a", b"x-b", b"x-c"]


def test_a_duplicated_header_keeps_both_values() -> None:
    out = _edge.request_headers([(b"x-a", b"1"), (b"x-a", b"2")], client=None, scheme=HTTP, via=VIA)
    assert [v for n, v in out if n == b"x-a"] == [b"1", b"2"]


@pytest.mark.parametrize(
    "hop",
    [
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    ],
)
def test_a_hop_by_hop_field_is_never_forwarded(hop: bytes) -> None:
    out = _edge.request_headers(
        [(hop, b"whatever"), (b"x-keep", b"1")], client=None, scheme=HTTP, via=VIA
    )
    assert hop not in names(out)
    assert b"x-keep" in names(out)


def test_a_field_named_by_connection_is_dropped_for_this_message() -> None:
    out = _edge.request_headers(
        [(b"connection", b"keep-alive, x-hop"), (b"x-hop", b"secret"), (b"x-keep", b"1")],
        client=None,
        scheme=HTTP,
        via=VIA,
    )
    assert b"x-hop" not in names(out)
    assert b"x-keep" in names(out)


def test_connection_close_and_keep_alive_are_not_treated_as_field_names() -> None:
    out = _edge.request_headers(
        [(b"connection", b"CLOSE, Keep-Alive"), (b"close", b"1"), (b"keep-alive-ish", b"2")],
        client=None,
        scheme=HTTP,
        via=VIA,
    )
    assert b"close" in names(out)
    assert b"keep-alive-ish" in names(out)


def test_a_client_supplied_forwarded_for_is_replaced() -> None:
    out = _edge.request_headers(
        [(b"x-forwarded-for", b"203.0.113.9")], client="198.51.100.4", scheme=HTTP, via=VIA
    )
    assert value(out, b"x-forwarded-for") == b"198.51.100.4"
    assert [v for n, v in out if n == b"x-forwarded-for"] == [b"198.51.100.4"]


def test_host_and_content_length_are_dropped_for_the_sender_to_rewrite() -> None:
    out = _edge.request_headers(
        [(b"host", b"front.example"), (b"content-length", b"5")], client=None, scheme=HTTP, via=VIA
    )
    assert b"host" not in names(out)
    assert b"content-length" not in names(out)


def test_the_inbound_host_becomes_x_forwarded_host_and_the_forwarded_record() -> None:
    out = _edge.request_headers(
        [(b"host", b"front.example")], client="203.0.113.7", scheme=HTTP, via=VIA
    )
    assert value(out, b"x-forwarded-host") == b"front.example"
    assert value(out, b"forwarded") == b'for="203.0.113.7"; proto=http; host="front.example"'


def test_the_forwarded_record_omits_for_when_the_peer_is_unknown() -> None:
    out = _edge.request_headers([], client=None, scheme=HTTP, via=VIA)
    assert value(out, b"forwarded") == b"proto=http"
    assert b"x-forwarded-for" not in names(out)


def test_via_is_appended_to_the_chain_rather_than_replacing_it() -> None:
    out = _edge.request_headers(
        [(b"via", b"1.1 alpha"), (b"via", b"1.1 beta")], client=None, scheme=HTTP, via=VIA
    )
    assert value(out, b"via") == b"1.1 alpha, 1.1 beta, 1.1 wreath"


def test_via_is_written_even_with_no_inbound_chain() -> None:
    out = _edge.request_headers([], client=None, scheme=HTTP, via=VIA)
    assert value(out, b"via") == VIA


def test_a_non_bytes_header_is_refused_rather_than_coerced() -> None:
    with pytest.raises(TypeError):
        _edge.request_headers([("x-a", b"1")], client=None, scheme=HTTP, via=VIA)
    with pytest.raises(TypeError):
        _edge.request_headers([(b"x-a", "1")], client=None, scheme=HTTP, via=VIA)


def test_a_malformed_pair_is_refused() -> None:
    with pytest.raises((TypeError, ValueError)):
        _edge.request_headers([(b"x-a",)], client=None, scheme=HTTP, via=VIA)
