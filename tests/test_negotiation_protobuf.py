from __future__ import annotations

from typing import cast

import pytest

from wreath.negotiation import (
    DEFAULT_SERIALIZERS,
    JSON,
    PROTOBUF,
    negotiate,
    serialize,
)
from wreath.protobuf import decode, encode, field, message
from wreath.request import Request


@message
class Reading:
    sensor: int = field(1)
    celsius: float = field(2)


class _Req:
    def __init__(self, accept: str | None) -> None:
        self._accept = accept

    def header(self, name: str, default=None):
        return self._accept if name.lower() == "accept" else default


def _request(accept: str | None) -> Request:
    return cast(Request, _Req(accept))


OFFERS = (PROTOBUF, JSON)


def test_protobuf_is_selected_by_accept() -> None:
    assert negotiate("application/x-protobuf", OFFERS) is PROTOBUF


def test_protobuf_respects_q_values() -> None:
    # JSON ranked higher wins even though protobuf is offered first.
    assert negotiate("application/x-protobuf;q=0.2, application/json;q=0.9", OFFERS) is JSON
    # And q=0 excludes it outright, per RFC 9110 §12.5.1.
    assert negotiate("application/x-protobuf;q=0, */*", OFFERS) is JSON


def test_serialize_emits_protobuf_bytes_and_headers() -> None:
    reading = Reading(sensor=7, celsius=1.5)
    response = serialize(_request("application/x-protobuf"), reading, serializers=OFFERS)
    assert response.status == 200
    assert (b"content-type", b"application/x-protobuf") in response.headers
    assert (b"vary", b"Accept") in response.headers
    # The body is the codec's own output, and it decodes back to the value.
    assert response.body == encode(Reading(sensor=7, celsius=1.5))
    assert decode(Reading, response.body) == Reading(sensor=7, celsius=1.5)


def test_protobuf_is_not_a_default_offer() -> None:
    assert PROTOBUF not in DEFAULT_SERIALIZERS
    assert negotiate("application/x-protobuf") is None


def test_encoding_an_undeclared_value_is_refused_by_name() -> None:
    with pytest.raises(TypeError) as excinfo:
        PROTOBUF.encode({"sensor": 7})
    text = str(excinfo.value)
    assert "application/x-protobuf" in text
    assert "@message" in text
    assert "dict" in text


def test_the_refusal_reaches_the_caller_rather_than_falling_back() -> None:
    with pytest.raises(TypeError):
        serialize(_request("application/x-protobuf"), {"sensor": 7}, serializers=OFFERS)
