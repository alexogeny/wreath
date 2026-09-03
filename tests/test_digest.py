from __future__ import annotations

from typing import Any

import pytest

from wreath import Request, Wreath
from wreath.digest import Digest, DigestError, DigestPreferences
from wreath.response import Response
from wreath.testing import TestClient

RFC_BODY = b'{"hello": "world"}'
RFC_SHA256 = b"sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:"
RFC_SHA512 = (
    b"sha-512=:WZDPaVn/7XgHaAy8pmojAkGWoRx2UFChF41A2svX+TaPm+AbwAgBWnrIiYllu7"
    b"BNNyealdVLvRwEmTHWXvJwew==:"
)


def test_digest_matches_rfc_9530_appendix_d_vectors() -> None:
    assert Digest.compute(RFC_BODY, "sha-256").header == RFC_SHA256
    assert Digest.compute(RFC_BODY, "sha-512").header == RFC_SHA512
    assert Digest.compute(RFC_BODY, "sha-256", "sha-512").header == (
        RFC_SHA256 + b", " + RFC_SHA512
    )


def test_digest_parses_multiple_algorithms_and_verifies_the_strongest_supported() -> None:
    digest = Digest.parse(RFC_SHA256 + b", " + RFC_SHA512)

    assert digest.algorithms == ("sha-256", "sha-512")
    assert digest.verify(RFC_BODY) == "sha-512"


def test_digest_refuses_malformed_or_unusable_integrity_fields() -> None:
    with pytest.raises(DigestError, match="byte sequence"):
        Digest.parse("sha-256=1")
    with pytest.raises(DigestError, match="parameters"):
        Digest.parse("sha-256=:YWJj:;unexpected")
    with pytest.raises(DigestError, match="supported algorithm"):
        Digest.parse("unixsum=:GQU=:").verify(b"hello")
    with pytest.raises(DigestError, match="does not match"):
        Digest.parse(RFC_SHA256).verify(b"changed")


def test_integrity_preferences_follow_rfc_weights_and_server_tie_order() -> None:
    preferences = DigestPreferences.parse("sha-512=3, sha-256=10, unixsum=0")

    assert preferences.header == b"sha-512=3, sha-256=10, unixsum=0"
    assert preferences.preferred("sha-512", "sha-256") == "sha-256"
    assert (
        DigestPreferences.parse("sha-512=10, sha-256=10").preferred("sha-256", "sha-512")
        == "sha-256"
    )
    assert DigestPreferences.parse("sha-256=0").preferred("sha-256") is None


@pytest.mark.parametrize("value", [-1, 11, True, b"one"])
def test_integrity_preferences_refuse_values_outside_zero_to_ten(value: Any) -> None:
    with pytest.raises(DigestError, match="integer from 0 to 10"):
        DigestPreferences({"sha-256": value})


def test_response_can_emit_content_and_selected_representation_digests() -> None:
    response = Response(RFC_BODY, headers=[(b"content-digest", b"old=:AA==:")])

    response.set_content_digest("sha-256")
    response.set_repr_digest("sha-512")

    assert [value for name, value in response.headers if name == b"content-digest"] == [RFC_SHA256]
    assert [value for name, value in response.headers if name == b"repr-digest"] == [RFC_SHA512]


def test_response_content_digest_can_cover_explicit_transmitted_bytes() -> None:
    response = Response(b"different bytes")

    response.set_content_digest("sha-256", content=RFC_BODY)

    assert [value for name, value in response.headers if name == b"content-digest"] == [RFC_SHA256]


def test_partial_response_requires_the_complete_selected_representation() -> None:
    response = Response(b'"world"}\n', status=206)

    with pytest.raises(ValueError, match="selected representation"):
        response.set_repr_digest("sha-256")

    response.set_repr_digest("sha-256", representation=RFC_BODY + b"\n")
    assert any(name == b"repr-digest" for name, _value in response.headers)


@pytest.mark.asyncio
async def test_request_verifies_a_required_content_digest() -> None:
    app = Wreath()

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, str]:
        algorithm = await request.verify_content_digest(required=True)
        return {"algorithm": algorithm or "none"}

    async with TestClient(app) as client:
        accepted = await client.post(
            "/upload", content=RFC_BODY, headers={"content-digest": RFC_SHA256.decode()}
        )
        changed = await client.post(
            "/upload", content=b"changed", headers={"content-digest": RFC_SHA256.decode()}
        )
        missing = await client.post("/upload", content=RFC_BODY)

    assert accepted.status == 200
    assert accepted.json() == {"algorithm": "sha-256"}
    assert changed.status == 400
    assert b"does not match" in changed.body
    assert missing.status == 400
    assert b"Content-Digest is required" in missing.body


@pytest.mark.asyncio
async def test_optional_repr_digest_is_absent_without_the_header() -> None:
    app = Wreath()

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, str | None]:
        return {"algorithm": await request.verify_repr_digest()}

    async with TestClient(app) as client:
        response = await client.post("/upload", content=RFC_BODY)

    assert response.status == 200
    assert response.json() == {"algorithm": None}


@pytest.mark.asyncio
async def test_request_selects_wanted_digest_algorithms_without_failing_on_a_bad_hint() -> None:
    app = Wreath()

    @app.get("/asset")
    async def asset(request: Request) -> Response:
        response = Response(RFC_BODY)
        algorithm = request.preferred_content_digest("sha-512", "sha-256")
        if algorithm is not None:
            response.set_content_digest(algorithm)
        return response

    async with TestClient(app) as client:
        preferred = await client.get(
            "/asset", headers={"want-content-digest": "sha-512=3, sha-256=10"}
        )
        malformed = await client.get("/asset", headers={"want-content-digest": "sha-256=99"})

    assert preferred.header("content-digest") == RFC_SHA256.decode()
    assert malformed.header("content-digest") is None


def test_response_can_ask_for_integrity_on_future_requests() -> None:
    response = Response()
    preferences = DigestPreferences({"sha-512": 10, "sha-256": 5})

    response.set_want_content_digest(preferences)
    response.set_want_repr_digest(preferences)

    assert (b"want-content-digest", preferences.header) in response.headers
    assert (b"want-repr-digest", preferences.header) in response.headers
