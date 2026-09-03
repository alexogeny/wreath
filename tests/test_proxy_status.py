from __future__ import annotations

from collections.abc import Iterable
from typing import cast

import pytest

from wreath.proxy_status import ProxyStatus


def test_proxy_status_serializes_one_structured_field_member() -> None:
    status = ProxyStatus(
        "edge.example",
        error="connection_timeout",
        next_hop="origin.example:8443",
        next_protocol="h2",
        received_status=504,
        details='upstream "closed" \\ early',
    )

    assert status.to_header() == (
        b"edge.example;error=connection_timeout;next-hop=origin.example:8443;"
        b'next-protocol=h2;received-status=504;details="upstream \\"closed\\" '
        b'\\\\ early"'
    )


def test_proxy_status_uses_string_and_byte_sequence_forms_when_required() -> None:
    status = ProxyStatus("edge node", next_hop="origin node", next_protocol=b"\x00h3")
    assert status.to_header() == (b'"edge node";next-hop="origin node";next-protocol=:AGgz:')


def test_proxy_status_prefers_a_token_for_ascii_alpn_bytes() -> None:
    assert ProxyStatus("edge", next_protocol=b"h2").to_header() == (b"edge;next-protocol=h2")


def test_proxy_status_allows_an_empty_optional_detail() -> None:
    assert ProxyStatus("edge", details="").to_header() == b'edge;details=""'


def test_proxy_status_serializes_rfc_9532_next_hop_alias_examples() -> None:
    status = ProxyStatus(
        "proxy.example.net",
        next_hop="2001:db8::1",
        next_hop_aliases=("tracker.example.com", "service1.example.com"),
    )
    assert status.to_header() == (
        b'proxy.example.net;next-hop="2001:db8::1";'
        b'next-hop-aliases="tracker.example.com,service1.example.com"'
    )

    escaped = ProxyStatus(
        "proxy.example.net",
        next_hop_aliases=(
            "comma,name.example.com",
            r"dot\.label.example.com",
            r"backslash\\name.example.com",
        ),
    )
    assert escaped.to_header() == (
        b'proxy.example.net;next-hop-aliases="comma%2Cname.example.com,'
        b'dot%5C.label.example.com,backslash%5C%5Cname.example.com"'
    )


def test_proxy_status_can_report_that_dns_returned_no_aliases() -> None:
    assert ProxyStatus("edge", next_hop_aliases=()).to_header() == (b'edge;next-hop-aliases=""')


def test_proxy_status_normalizes_a_trailing_root_label() -> None:
    assert ProxyStatus("edge", next_hop_aliases=("origin.example.",)).to_header() == (
        b'edge;next-hop-aliases="origin.example"'
    )


@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        ("one.example", "iterable of DNS names"),
        (("",), "must not be empty"),
        ((object(),), "entries must be str"),
        ((r"bad\q.example",), "escape"),
        (("bad\\",), "escape"),
        (("left..right",), "empty DNS label"),
        (("a" * 64 + ".example",), "label"),
        ((".".join(("a" * 63,) * 4),), "255-octet DNS limit"),
        (("snowman.\N{SNOWMAN}",), "IDNA"),
    ],
)
def test_proxy_status_refuses_invalid_next_hop_aliases(aliases: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        ProxyStatus("edge", next_hop_aliases=cast(Iterable[str] | None, aliases))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"error": "not an error"}, "error must be an RFC 9651 Token"),
        ({"next_protocol": "http 1.1"}, "next_protocol must be an RFC 9651 Token"),
        ({"received_status": True}, "received_status must be int"),
        ({"received_status": 99}, "received_status must be an HTTP status code"),
        ({"details": "line\nbreak"}, "details must contain only printable ASCII"),
    ],
)
def test_proxy_status_refuses_invalid_parameters(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        ProxyStatus("edge.example", **kwargs)


@pytest.mark.parametrize("proxy", ["", "snowman \N{SNOWMAN}", "line\rbreak"])
def test_proxy_status_refuses_invalid_proxy_identifiers(proxy: str) -> None:
    with pytest.raises(ValueError, match="proxy must be a non-empty RFC 9651 String or Token"):
        ProxyStatus(proxy)
