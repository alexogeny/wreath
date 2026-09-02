from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any


def discord() -> Any:
    return importlib.import_module("wreath.chat.discord")


def chatops() -> Any:
    return importlib.import_module("wreath.chat")


@dataclass
class Clock:
    value: float = 1_000.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.value += delay


@dataclass
class HTTPResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    json: Any = None


class HTTPClient:
    def __init__(self, responses: list[HTTPResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> HTTPResponse:
        self.requests.append((method, path, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return HTTPResponse(200, json={"id": "message-1"})


def command_payload(
    *,
    interaction_id: str = "interaction-1",
    token: str = "interaction-token",
    application_id: str = "application-1",
    guild_id: str = "guild-1",
    user_id: str = "user-1",
) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "application_id": application_id,
        "type": 2,
        "token": token,
        "version": 1,
        "guild_id": guild_id,
        "context": 0,
        "authorizing_integration_owners": {"0": guild_id},
        "member": {"user": {"id": user_id}},
        "data": {
            "id": "command-1",
            "name": "agent",
            "type": 1,
            "options": [
                {
                    "name": "ask",
                    "type": 1,
                    "options": [
                        {"name": "prompt", "type": 3, "value": "ship it"},
                        {"name": "private", "type": 5, "value": True},
                    ],
                }
            ],
        },
    }


def component_payload(custom_id: str = "approval:approve:nonce-1") -> dict[str, Any]:
    payload = command_payload()
    payload["type"] = 3
    payload["data"] = {"custom_id": custom_id, "component_type": 2}
    payload["message"] = {"id": "message-1"}
    return payload


def modal_payload() -> dict[str, Any]:
    payload = command_payload()
    payload["type"] = 5
    payload["data"] = {
        "custom_id": "agent:details",
        "components": [
            {
                "type": 18,
                "component": {
                    "type": 4,
                    "custom_id": "prompt",
                    "value": "full request",
                },
            }
        ],
    }
    return payload
