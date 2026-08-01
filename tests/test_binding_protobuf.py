"""Binding a `Content-Type: application/x-protobuf` request body.

Two decisions were deferred to this change, and neither is obvious. Both are
asserted here rather than only written down, because a documented decision that
no test holds is a decision the next refactor is free to reverse.

**1. A `@message` annotation is content-negotiated, not protobuf-only.** The
same handler binds a JSON body and a protobuf one, chosen by `Content-Type`.
This is the conservative reading and it is also the compatible one: a
`@message` class is an ordinary dataclass, so it *already* bound from JSON
before protobuf existed at the boundary, and making the annotation mean
"protobuf only" would have silently broken every handler that had one. It also
keeps the request half symmetric with the response half, where
`serialize(request, msg, serializers=(PROTOBUF, JSON))` has always negotiated;
and it is what the format's own users need -- OTLP/HTTP defines a protobuf
*and* a JSON encoding of the same messages behind one endpoint.

**2. The two content types are deliberately not equally strict.** JSON rejects
an unknown field; protobuf preserves an unknown field *number*. That is one
request shape with two strictnesses, decided by `Content-Type`, and it is a
choice rather than an oversight: an unexpected name in JSON is a typo, and an
unexpected number on a protobuf wire is a peer built against a newer
declaration. Tolerating the second is the mechanism protobuf exists to provide
-- the bytes survive the hop and `encode` puts them back -- and refusing it
would make every schema rollout a synchronised deploy.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from wreath import Wreath
from wreath.negotiation import PROTOBUF, PROTOBUF_MEDIA_TYPES
from wreath.protobuf import decode, encode, field, message, unknown_fields
from wreath.testing import TestClient


@message
class Sighting:
    """What this build knows about a sighting."""

    species: str = field(1)
    count: int = field(2)


@message
class SightingV2:
    """A peer built against a newer declaration: one more field number."""

    species: str = field(1)
    count: int = field(2)
    observer: str = field(3)


@dataclass
class PlainSighting:
    species: str
    count: int


def _app() -> Wreath:
    app = Wreath()

    @app.post("/sightings")
    async def record(request, sighting: Sighting) -> dict:
        # Re-encoded inside the handler so the assertion is about what a real
        # service would forward, not about an internal buffer's length.
        return {
            "species": sighting.species,
            "count": sighting.count,
            "kept": unknown_fields(sighting).hex(),
            "forwarded": decode(SightingV2, encode(sighting)).observer,
        }

    @app.post("/plain")
    async def plain(request, sighting: PlainSighting) -> dict:
        return {"species": sighting.species}

    return app


# --- decision 1: content-negotiated ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", sorted(PROTOBUF_MEDIA_TYPES))
async def test_a_protobuf_body_binds_to_the_declared_message(media_type: str) -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/sightings",
            content=encode(Sighting(species="tapir", count=3)),
            headers={"content-type": media_type},
        )
    assert response.status == 200
    assert response.json() == {
        "species": "tapir", "count": 3, "kept": "", "forwarded": ""
    }


@pytest.mark.asyncio
async def test_the_same_handler_still_binds_a_json_body() -> None:
    """The decision, asserted: the annotation does not mean protobuf-only.

    A `@message` is an ordinary dataclass and bound from JSON before this branch
    existed. If the annotation had been made to mean protobuf-only, this is the
    test that would have gone red -- which is the whole reason it is here.
    """
    async with TestClient(_app()) as client:
        response = await client.post(
            "/sightings", json={"species": "tapir", "count": 3}
        )
    assert response.status == 200
    assert response.json() == {
        "species": "tapir", "count": 3, "kept": "", "forwarded": ""
    }


@pytest.mark.asyncio
async def test_content_type_parameters_do_not_defeat_the_match() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/sightings",
            content=encode(Sighting(species="tapir", count=3)),
            headers={"content-type": " Application/X-Protobuf ; boundary=x"},
        )
    assert response.status == 200


# --- decision 2: two strictnesses, one request shape -------------------------


@pytest.mark.asyncio
async def test_an_unknown_field_number_is_preserved_not_refused() -> None:
    """Version skew, which is the case protobuf's numbering exists for."""
    async with TestClient(_app()) as client:
        response = await client.post(
            "/sightings",
            content=encode(SightingV2(species="tapir", count=3, observer="ada")),
            headers={"content-type": PROTOBUF.media_type},
        )
    assert response.status == 200
    body = response.json()
    assert body["species"] == "tapir"
    # Kept as raw bytes on the decoded message ...
    assert body["kept"] != ""
    # ... and put back on the way out, which is what makes keeping them useful.
    assert body["forwarded"] == "ada"


def test_a_preserved_field_survives_a_re_encode() -> None:
    """Preservation is only worth anything if the bytes come back out.

    This is what makes the asymmetry defensible rather than merely lenient: a
    service that reads a message, changes one field and forwards it does not
    silently strip what a newer peer sent through it.
    """
    wire = encode(SightingV2(species="tapir", count=3, observer="ada"))
    older = decode(Sighting, wire)
    assert decode(SightingV2, encode(older)).observer == "ada"


@pytest.mark.asyncio
async def test_an_unknown_field_name_in_json_is_still_refused() -> None:
    """The other half of the pair, on the same handler and the same shape.

    Asserted beside its protobuf twin on purpose: the two strictnesses are only
    a decision if both are pinned. One of them drifting to match the other is
    exactly what this test exists to notice.
    """
    async with TestClient(_app()) as client:
        response = await client.post(
            "/sightings", json={"species": "tapir", "count": 3, "observer": "ada"}
        )
    assert response.status == 422


# --- refusals, each with its own message -------------------------------------


@pytest.mark.asyncio
async def test_undecodable_protobuf_bytes_are_refused_as_protobuf() -> None:
    """Not "invalid JSON body": the refusal has to name the format that failed,
    or a caller reads it as having sent the wrong body entirely."""
    async with TestClient(_app()) as client:
        response = await client.post(
            "/sightings",
            content=b"\x08",  # a field tag with its varint missing
            headers={"content-type": PROTOBUF.media_type},
        )
    assert response.status == 400
    assert "protobuf" in response.json()["detail"]
    assert "JSON" not in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_protobuf_body_against_a_plain_dataclass_is_refused_by_name() -> None:
    """A distinct third message, for a distinct third cause.

    The handler's body is a dataclass with no field numbers, so there is nothing
    for the codec to read the wire against. Refusing here names the declaration;
    falling through to the JSON path would have blamed the caller's bytes for
    the server's annotation.
    """
    async with TestClient(_app()) as client:
        response = await client.post(
            "/plain",
            content=b"\x0a\x05tapir",
            headers={"content-type": PROTOBUF.media_type},
        )
    detail = response.json()["detail"]
    assert response.status == 400
    assert "@message" in detail
    assert "PlainSighting" in detail


@pytest.mark.asyncio
async def test_the_three_refusals_are_three_different_sentences() -> None:
    """A refusal test that asserts only a field name proves nothing.

    Every one of these mentions the body, so a test keyed on that word would
    pass whichever branch fired -- including a fallthrough that never reached
    the protobuf path at all. The three causes have three remedies, so they must
    read differently.
    """
    async with TestClient(_app()) as client:
        bad_json = await client.post(
            "/sightings", content=b"{oh no", headers={"content-type": "application/json"}
        )
        bad_protobuf = await client.post(
            "/sightings", content=b"\x08", headers={"content-type": PROTOBUF.media_type}
        )
        undeclared = await client.post(
            "/plain", content=b"\x08", headers={"content-type": PROTOBUF.media_type}
        )
    details = [
        bad_json.json()["detail"],
        bad_protobuf.json()["detail"],
        undeclared.json()["detail"],
    ]
    assert len(set(details)) == 3, details
    # Each is a refusal rather than a collapse. Without this the mutation pass
    # found that deleting the JSON `raise` still left three distinct strings --
    # the handler was called without its argument and answered 500, which is a
    # different sentence and not a refusal at all.
    assert [r.status for r in (bad_json, bad_protobuf, undeclared)] == [400, 400, 400]
    assert details[0].startswith("invalid JSON body")
