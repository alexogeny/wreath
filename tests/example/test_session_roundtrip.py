"""Signing in, and then being recognised — the round trip through the cookie.

`test_authorization.py` asks what each role may see, and does it with
`acting_as`, which installs an identity directly. That is the right tool for a
policy matrix, and it is why this file exists separately: `acting_as` never
touches the session cookie, so nothing there exercises the path a browser
actually takes — POST the credentials, receive `Set-Cookie`, send it back, be
recognised.

That gap hid a real defect. `GET /session` reported `signed_in: false` to a
caller holding a cookie it had just issued, because it read `request.identity`
on a route with no authentication requirement, and `authenticated()` is what
asks the backend and populates that attribute. The route existed, the route
table asserted it existed, and nothing ever asked it the question it is for.

`TestClient` keeps no cookie jar, so the cookie is carried by hand here. That is
not a workaround to apologise for: it makes the thing under test visible in the
test, and a jar would have hidden which request actually carried the credential.
"""

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
    """"Am I signed in" is a question an anonymous caller may ask.

    A 401 here would be wrong: the console calls this on load to decide whether
    to render the sign-in form, and a 401 is not an answer to that question.
    """
    response = await anonymous_client.get("/session")
    assert response.status == 200
    assert response.json() == {"signed_in": False}


@skip_without_database
async def test_signing_in_is_visible_to_the_next_request(anonymous_client) -> None:
    """The defect this file was written for.

    Sign in, then ask who you are, carrying the cookie that was just issued.
    Before the fix this answered `{"signed_in": false}` — the handler read
    `request.identity` on a route with no authentication requirement, and that
    attribute is populated only where `authenticated()` (or a decorator that
    implies it) has asked the backend. The route cannot *use* that decorator
    without turning the anonymous case into a 401, so it reads the session it
    was handed instead.
    """
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
    """And the reverse: the answer goes back to anonymous rather than staying stale."""
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
    """The cookie is not decorative: it is what `/reserves` accepts.

    Asserted here rather than assumed, because every other database test in
    this package uses `acting_as` and would keep passing if the cookie stopped
    being honoured entirely.
    """
    assert (await anonymous_client.get("/reserves")).status == 401

    signed_in = await anonymous_client.post("/session", params={"email": VOLUNTEER})
    admitted = await anonymous_client.get("/reserves", headers=cookie(signed_in))
    assert admitted.status == 200
    assert admitted.json()["items"], "signed in, but the reserve list came back empty"
