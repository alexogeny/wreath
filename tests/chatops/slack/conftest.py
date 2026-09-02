from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from urllib.parse import urlencode

import pytest

SIGNING_SECRET = "slack-signing-secret"
NOW = 1_800_000_000


def signed_headers(
    body: bytes,
    *,
    timestamp: int = NOW,
    secret: str = SIGNING_SECRET,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    base = b"v0:" + str(timestamp).encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-slack-request-timestamp": str(timestamp),
        "x-slack-signature": signature,
        **dict(extra or {}),
    }


def json_body(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def form_body(**values: str) -> bytes:
    return urlencode(values).encode()


@pytest.fixture
def event_envelope() -> dict[str, object]:
    return {
        "type": "event_callback",
        "team_id": "T123",
        "api_app_id": "A123",
        "event_id": "Ev123",
        "event_time": NOW,
        "authorizations": [
            {
                "enterprise_id": None,
                "team_id": "T123",
                "user_id": "UAPP",
                "is_bot": True,
                "is_enterprise_install": False,
            }
        ],
        "event": {
            "type": "app_mention",
            "user": "U123",
            "channel": "C123",
            "text": "<@UAPP> deploy production",
            "event_ts": "1800000000.000100",
        },
    }


@pytest.fixture
def slash_values() -> dict[str, str]:
    return {
        "api_app_id": "A123",
        "team_id": "T123",
        "team_domain": "acme",
        "channel_id": "C123",
        "channel_name": "operations",
        "user_id": "U123",
        "user_name": "mara",
        "command": "/deploy",
        "text": "production",
        "response_url": "https://hooks.slack.com/commands/T123/1/secret",
        "trigger_id": "123.456",
    }


class RecordingInstallationStore:
    def __init__(self, installations: Mapping[tuple[str | None, str | None], object] = ()):
        self.installations = dict(installations)
        self.queries: list[tuple[str | None, str | None, bool]] = []
        self.saved: list[object] = []

    async def fetch(
        self,
        *,
        enterprise_id: str | None,
        team_id: str | None,
        is_enterprise_install: bool,
    ) -> object | None:
        self.queries.append((enterprise_id, team_id, is_enterprise_install))
        key = (enterprise_id, None) if is_enterprise_install else (enterprise_id, team_id)
        return self.installations.get(key)

    async def save(self, installation: object) -> None:
        self.saved.append(installation)


class RecordingTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], object]] = ()):
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []

    async def request(
        self, method: str, url: str, *, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], object]:
        self.requests.append((method, url, headers, body))
        if self.responses:
            return self.responses.pop(0)
        return 200, {}, {"ok": True}
