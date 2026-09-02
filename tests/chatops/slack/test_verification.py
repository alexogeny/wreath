from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.chat import ChatOps
from wreath.chat.slack import Slack
from wreath.testing import TestClient

from .conftest import NOW, SIGNING_SECRET, json_body, signed_headers


def client_for(**options: object) -> TestClient:
    app = Wreath()
    ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW, **options),),
    )
    return TestClient(app)


async def test_signature_covers_the_exact_raw_body() -> None:
    raw = b'{"type":"url_verification", "challenge":"exact"}'
    compacted = b'{"type":"url_verification","challenge":"exact"}'
    headers = signed_headers(raw)

    accepted = await client_for().post("/chat/slack/events", content=raw, headers=headers)
    refused = await client_for().post("/chat/slack/events", content=compacted, headers=headers)

    assert (accepted.status, accepted.body) == (200, b"exact")
    assert refused.status == 401


@pytest.mark.parametrize("missing", ["x-slack-signature", "x-slack-request-timestamp"])
async def test_signature_headers_are_required(missing: str) -> None:
    body = json_body({"type": "url_verification", "challenge": "exact"})
    headers = signed_headers(body)
    del headers[missing]
    response = await client_for().post("/chat/slack/events", content=body, headers=headers)
    assert response.status == 401


@pytest.mark.parametrize("signature", ["", "v1=deadbeef", "v0=xyz", "v0=" + "0" * 64])
async def test_malformed_or_wrong_signatures_are_refused(signature: str) -> None:
    body = b"{}"
    headers = signed_headers(body)
    headers["x-slack-signature"] = signature
    response = await client_for().post("/chat/slack/events", content=body, headers=headers)
    assert response.status == 401


@pytest.mark.parametrize("timestamp", [NOW - 301, NOW + 301])
async def test_timestamp_is_bounded_in_both_directions(timestamp: int) -> None:
    body = b"{}"
    response = await client_for(max_age=300).post(
        "/chat/slack/events", content=body, headers=signed_headers(body, timestamp=timestamp)
    )
    assert response.status == 401


async def test_timestamp_boundary_is_inclusive() -> None:
    body = json_body({"type": "url_verification", "challenge": "edge"})
    response = await client_for(max_age=300).post(
        "/chat/slack/events",
        content=body,
        headers=signed_headers(body, timestamp=NOW - 300),
    )
    assert (response.status, response.body) == (200, b"edge")


@pytest.mark.parametrize("timestamp", ["1.5", "not-a-number", "", "+1800000000"])
async def test_timestamp_must_be_canonical_decimal_seconds(timestamp: str) -> None:
    body = b"{}"
    headers = signed_headers(body)
    headers["x-slack-request-timestamp"] = timestamp
    response = await client_for().post("/chat/slack/events", content=body, headers=headers)
    assert response.status == 401


async def test_deprecated_payload_token_never_substitutes_for_a_signature() -> None:
    body = json_body({"token": "legacy", "type": "url_verification", "challenge": "no"})
    response = await client_for().post(
        "/chat/slack/events",
        content=body,
        headers={"x-slack-request-timestamp": str(NOW), "content-type": "application/json"},
    )
    assert response.status == 401


async def test_header_names_are_case_insensitive() -> None:
    body = json_body({"type": "url_verification", "challenge": "exact"})
    headers = {name.title(): value for name, value in signed_headers(body).items()}
    response = await client_for().post("/chat/slack/events", content=body, headers=headers)
    assert (response.status, response.body) == (200, b"exact")
