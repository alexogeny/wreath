from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from wreath.client_facts import ClientFactsProvider, UserAgentDatabase
from wreath.request import Request


async def receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


def known_count(classification: tuple[object, ...]) -> int:
    database = SimpleNamespace(_classify=lambda raw: classification)
    provider = ClientFactsProvider(user_agents=cast("UserAgentDatabase", database))
    request = Request({"type": "http", "headers": []}, receive)
    provider.resolve(request)
    return provider.counters().values["ua_known"]


def test_browser_alone_counts_as_a_known_user_agent() -> None:
    assert known_count(("Browser", None, None, None, False, 1)) == 1


def test_platform_alone_counts_as_a_known_user_agent() -> None:
    assert known_count((None, None, "Platform", None, False, 1)) == 1


def test_mobile_classification_alone_counts_as_a_known_user_agent() -> None:
    assert known_count((None, None, None, False, False, 1)) == 1
