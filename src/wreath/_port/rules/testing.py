"""The test suite: the test client and dependency overrides."""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED

TESTS: dict[str, tuple[str, str, str, str]] = {
    "test.client": (
        "test",
        "other",
        NEEDS_REVIEW,
        "wreath.testing.TestClient is async. Three changes: open it with `async with TestClient(app) as client`, await every request, and read response.status instead of response.status_code.",
    ),
    "test.client_local": (
        "test",
        "other",
        TRANSLATED,
        "This function-local client has an exact async lifetime: make the test async, open the client with `async with`, await its requests, and use response.status. Pass --opinionated and the porter writes all four changes together.",
    ),
    "test.dependency_override": (
        "test",
        "other",
        NEEDS_REVIEW,
        'There is no dependency_overrides in wreath. Authentication becomes client.acting_as("principal", roles=[...]); an outbound service becomes a registered ServiceClient over a test transport adapter; database tests keep the same Session and select a real or replay PostgreSQL adapter underneath it. Delete fake repositories instead of porting or injecting them.',
    ),
    "test.dependency_override_auth": (
        "test",
        "other",
        NEEDS_REVIEW,
        "This overrides an authentication dependency. Delete the global override and set the test identity with client.acting_as(subject, roles=...) inside the TestClient lifespan.",
    ),
    "test.dependency_override_adapter": (
        "test",
        "other",
        NEEDS_REVIEW,
        "This overrides an application adapter. Make the adapter an explicit build_app(...) argument and construct the test app with an in-memory, replay, or test-transport implementation; Wreath intentionally has no global override map.",
    ),
}
