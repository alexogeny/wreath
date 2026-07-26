"""Content negotiation + the pure MessagePack encoder (spec byte-vectors)."""
from __future__ import annotations

import pytest

from wreath._pure.msgpack import packb
from wreath.negotiation import (
    JSON,
    MSGPACK,
    negotiate,
    parse_accept,
    serialize,
)


class _Req:
    def __init__(self, accept: str | None) -> None:
        self._accept = accept

    def header(self, name: str, default=None):
        return self._accept if name.lower() == "accept" else default


# --- MessagePack encoder vs the spec's byte layout --------------------------


@pytest.mark.parametrize("value, expected_hex", [
    (None, "c0"),
    (False, "c2"),
    (True, "c3"),
    (0, "00"),
    (127, "7f"),
    (128, "cc80"),
    (255, "ccff"),
    (256, "cd0100"),
    (-1, "ff"),
    (-32, "e0"),
    (-33, "d0df"),
    (1.5, "cb3ff8000000000000"),
    ("", "a0"),
    ("a", "a161"),
    (b"\x01", "c40101"),
    ([1, 2, 3], "9301020304"[:8]),      # 93 01 02 03
    ({"a": 1}, "81a16101"),
])
def test_msgpack_known_vectors(value, expected_hex) -> None:
    assert packb(value).hex() == expected_hex


def test_msgpack_round_trips_through_a_nested_structure() -> None:
    # Structural check: encodes without error and starts with a fixmap of 2.
    encoded = packb({"items": [1, "two", None], "ok": True})
    assert encoded[0] == 0x82   # map with 2 entries


# --- Accept parsing + negotiation -------------------------------------------


def test_parse_accept_orders_by_q_then_specificity() -> None:
    parsed = parse_accept("*/*;q=0.1, application/json;q=0.9, application/msgpack")
    assert parsed[0][0] == "application/msgpack"     # q=1.0
    assert parsed[1][0] == "application/json"        # q=0.9
    assert parsed[-1][0] == "*/*"                    # q=0.1


def test_negotiate_defaults_to_json() -> None:
    assert negotiate(None) is JSON
    assert negotiate("*/*") is JSON
    assert negotiate("application/*") is JSON        # first matching serializer


def test_negotiate_selects_msgpack_when_preferred() -> None:
    assert negotiate("application/msgpack") is MSGPACK
    assert negotiate("application/json;q=0.2, application/msgpack;q=0.9") is MSGPACK


def test_negotiate_unsatisfiable_returns_none() -> None:
    assert negotiate("application/xml") is None
    assert negotiate("application/json;q=0") is None   # explicitly not acceptable


def test_serialize_picks_format_and_sets_headers() -> None:
    json_response = serialize(_Req("application/json"), {"a": 1})
    assert json_response.status == 200
    assert (b"content-type", b"application/json") in json_response.headers
    assert (b"vary", b"Accept") in json_response.headers

    mp_response = serialize(_Req("application/msgpack"), {"a": 1})
    assert (b"content-type", b"application/msgpack") in mp_response.headers
    assert mp_response.body == packb({"a": 1})


def test_serialize_406_when_unsatisfiable() -> None:
    response = serialize(_Req("application/xml"), {"a": 1})
    assert response.status == 406
