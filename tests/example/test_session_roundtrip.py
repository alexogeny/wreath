from __future__ import annotations

import os

import pytest

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the camera-trap session round-trip tests",
)

#: The sightings are irrelevant here, so this is the smallest sample that still
#: builds a schema with the seeded observers in it.
SAMPLE = 200

#: Seeded by `camera_trap.seed`. A volunteer rather than a ranger because a
#: volunteer is the constrained role: if the round trip works for the identity
#: that is refused things, it works for the one that is refused nothing.
VOLUNTEER = "volunteer1@example.org"


def cookie(response) -> dict[str, str]:
    """The `Set-Cookie` value from `response`, as a request `Cookie` header.

    Reads `headers` directly rather than `response.header("set-cookie")`: that
    accessor returns the *first* match, and a response that sets more than one
    cookie would silently lose the rest. Only the `name=value` pair is sent
    back, which is what a browser does — the attributes are instructions to the
    client, not part of the credential.
    """
    values = [v.decode("latin-1") for name, v in response.headers if name.lower() == b"set-cookie"]
    assert values, "no Set-Cookie on a response that was supposed to establish a session"
    return {"cookie": "; ".join(value.split(";", 1)[0] for value in values)}


@pytest.fixture
async def anonymous_client():
    """The application, on a freshly built schema, with no identity installed.

    Deliberately *not* `acting_as`: this file is about the cookie, and
    `acting_as` short-circuits exactly the machinery under test.
    """
    from _camera_trap import build_schema, drop_schema
    from camera_trap.app import build

    from wreath.postgres import connect
    from wreath.testing import TestClient

    connection = await connect(_DSN)
    try:
        await build_schema(connection, seed_rows=SAMPLE)
    finally:
        await connection.close()

    async with TestClient(build()) as test_client:
        yield test_client

    connection = await connect(_DSN)
    try:
        await drop_schema(connection)
    finally:
        await connection.close()


@skip_without_database
async def test_an_anonymous_caller_is_told_so_rather_than_refused(anonymous_client) -> None:
    response = await anonymous_client.get("/session")
    assert response.status == 200
    assert response.json() == {"signed_in": False}


@skip_without_database
async def test_signing_in_is_visible_to_the_next_request(anonymous_client) -> None:
    signed_in = await anonymous_client.post("/session", params={"email": VOLUNTEER})
    assert signed_in.status == 200
    assert signed_in.json()["role"] == "volunteer"

    response = await anonymous_client.get("/session", headers=cookie(signed_in))
    assert response.status == 200
    body = response.json()
    assert body["signed_in"] is True, "the session cookie was issued and then not believed"
    assert body["roles"] == ["volunteer"]


@skip_without_database
async def test_signing_out_is_visible_to_the_next_request(anonymous_client) -> None:
    signed_in = await anonymous_client.post("/session", params={"email": VOLUNTEER})
    held = cookie(signed_in)
    assert (await anonymous_client.get("/session", headers=held)).json()["signed_in"] is True

    signed_out = await anonymous_client.delete("/session", headers=held)
    assert signed_out.status == 204

    cleared = await anonymous_client.get("/session", headers=cookie(signed_out))
    assert cleared.json() == {"signed_in": False}


@skip_without_database
async def test_the_session_cookie_admits_the_caller_to_a_protected_route(
    anonymous_client,
) -> None:
    assert (await anonymous_client.get("/reserves")).status == 401

    signed_in = await anonymous_client.post("/session", params={"email": VOLUNTEER})
    admitted = await anonymous_client.get("/reserves", headers=cookie(signed_in))
    assert admitted.status == 200
    assert admitted.json()["items"], "signed in, but the reserve list came back empty"
