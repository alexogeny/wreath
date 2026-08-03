"""A declared message is readable *and* writable, and the document says both.

Wreath has decoded `application/x-protobuf` request bodies since
`binding._decode_protobuf_body` landed. It could not encode one: a handler
returning a declared message served JSON whatever the client asked for, and the
generated OpenAPI advertised JSON on both halves. That asymmetry was not a
decision -- it was the half that had not been done.

The fix is deliberately *per route*, not a change to `DEFAULT_SERIALIZERS`. The
reason `negotiation` keeps `PROTOBUF` out of the global offers still stands:
JSON and MessagePack encode whatever a handler returns, protobuf can only encode
a declared message, and a handler returning a dict is the common case. What was
missing is the **return annotation** -- a route saying at startup that its body
is encodable. These tests pin that distinction, because the tempting fix (add
PROTOBUF to the defaults) breaks every dict-returning route in the tree.
"""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.negotiation import PROTOBUF
from wreath.openapi import generate_openapi
from wreath.protobuf import decode, encode, field, message
from wreath.testing import TestClient

PROTO = "application/x-protobuf"


@message
class Reading:
    seq: int = field(1)
    note: str = field(2)


def _app() -> Wreath:
    app = Wreath()

    @app.post("/echo")
    async def echo(request, body: Reading) -> Reading:
        return body

    @app.get("/plain")
    async def plain(request) -> dict:
        return {"seq": 1}

    return app


def _content_type(response) -> str | None:
    return response.header("content-type")


# --- the wire ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_message_is_read_from_the_protobuf_wire() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/echo",
            content=encode(Reading(seq=4, note="wire")),
            headers={"content-type": PROTO},
        )
    assert response.status == 200
    assert response.json() == {"seq": 4, "note": "wire"}


@pytest.mark.asyncio
async def test_a_declared_message_is_written_to_the_protobuf_wire() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/echo", json={"seq": 7, "note": "out"}, headers={"accept": PROTO}
        )
    assert response.status == 200
    assert _content_type(response) == PROTO
    assert decode(Reading, response.body) == Reading(seq=7, note="out")


@pytest.mark.asyncio
async def test_the_round_trip_is_protobuf_on_both_halves() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/echo",
            content=encode(Reading(seq=9, note="both")),
            headers={"content-type": PROTO, "accept": PROTO},
        )
    assert decode(Reading, response.body) == Reading(seq=9, note="both")


@pytest.mark.asyncio
async def test_json_is_still_what_a_client_without_a_preference_gets() -> None:
    # The property that makes this safe to land: adding an offer must not change
    # what an existing client already receives.
    async with TestClient(_app()) as client:
        default = await client.post("/echo", json={"seq": 1, "note": "a"})
        wildcard = await client.post(
            "/echo", json={"seq": 1, "note": "a"}, headers={"accept": "*/*"}
        )
    for response in (default, wildcard):
        assert _content_type(response) == "application/json"
        assert response.json() == {"seq": 1, "note": "a"}


@pytest.mark.asyncio
async def test_a_negotiated_response_varies_on_accept() -> None:
    # Or a shared cache serves one client's protobuf to another client's JSON
    # request -- the same reason `serialize` sets it.
    async with TestClient(_app()) as client:
        response = await client.post(
            "/echo", json={"seq": 1, "note": "a"}, headers={"accept": PROTO}
        )
    assert response.header("vary") == "Accept"


@pytest.mark.asyncio
async def test_a_route_that_returns_a_dict_is_untouched() -> None:
    # The whole reason this is per-route. A global PROTOBUF offer would make
    # this request a runtime error, because protobuf cannot encode a dict.
    async with TestClient(_app()) as client:
        response = await client.get("/plain", headers={"accept": PROTO})
    assert response.status == 200
    assert _content_type(response) == "application/json"


@pytest.mark.asyncio
async def test_an_explicit_json_preference_still_wins() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/echo",
            json={"seq": 2, "note": "b"},
            headers={"accept": "application/json;q=1.0, application/x-protobuf;q=0.5"},
        )
    assert _content_type(response) == "application/json"


# --- the document --------------------------------------------------------------------


def test_the_document_advertises_both_media_types() -> None:
    # A document that names only JSON understates what the endpoint accepts and
    # produces, and a generated client believes the document.
    document = generate_openapi(_app(), title="t", version="1")
    operation = document["paths"]["/echo"]["post"]
    assert set(operation["requestBody"]["content"]) == {"application/json", PROTO}
    assert set(operation["responses"]["200"]["content"]) == {"application/json", PROTO}


def test_both_media_types_carry_the_same_schema() -> None:
    document = generate_openapi(_app(), title="t", version="1")
    content = document["paths"]["/echo"]["post"]["requestBody"]["content"]
    assert content["application/json"]["schema"] == content[PROTO]["schema"]


def test_a_dict_route_advertises_only_json() -> None:
    document = generate_openapi(_app(), title="t", version="1")
    operation = document["paths"]["/plain"]["get"]
    assert set(operation["responses"]["200"]["content"]) == {"application/json"}


def test_the_document_and_the_wire_name_one_media_type() -> None:
    # Spelled from `negotiation.PROTOBUF` in both places rather than beside it,
    # so the two cannot drift into naming different types.
    document = generate_openapi(_app(), title="t", version="1")
    advertised = set(document["paths"]["/echo"]["post"]["requestBody"]["content"])
    assert PROTOBUF.media_type in advertised
