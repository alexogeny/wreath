"""ServiceClient: base-path prefixing + auto-injected refreshing bearer token."""
from __future__ import annotations

import pytest

from wreath.service_client import ServiceClient

pytestmark = pytest.mark.asyncio


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, method, target, *, headers=(), body=b"", idempotency_key=None):
        self.calls.append({"method": method, "target": target, "headers": headers,
                            "body": body, "idempotency_key": idempotency_key})
        return "response"


def _auth(headers) -> bytes | None:
    return dict(headers).get(b"authorization")


async def test_base_path_is_prefixed() -> None:
    client = _FakeClient()
    svc = ServiceClient(client, base_path="/billing/v1")
    await svc.get("/invoices/7")
    assert client.calls[-1]["target"] == "/billing/v1/invoices/7"
    await svc.get("invoices/8")               # missing leading slash still joins cleanly
    assert client.calls[-1]["target"] == "/billing/v1/invoices/8"


async def test_static_string_token_is_injected() -> None:
    client = _FakeClient()
    svc = ServiceClient(client, token="abc123")
    await svc.get("/x")
    assert _auth(client.calls[-1]["headers"]) == b"Bearer abc123"


async def test_client_credentials_style_token_is_awaited() -> None:
    class _Creds:
        def __init__(self) -> None:
            self.n = 0

        async def token(self) -> str:
            self.n += 1
            return f"tok-{self.n}"

    client, creds = _FakeClient(), _Creds()
    svc = ServiceClient(client, token=creds)
    await svc.get("/a")
    await svc.get("/b")
    assert _auth(client.calls[0]["headers"]) == b"Bearer tok-1"
    assert _auth(client.calls[1]["headers"]) == b"Bearer tok-2"   # re-asked each call


async def test_async_callable_token() -> None:
    async def provider() -> str:
        return "callable-tok"

    client = _FakeClient()
    await ServiceClient(client, token=provider).post("/x", body=b"{}")
    assert _auth(client.calls[-1]["headers"]) == b"Bearer callable-tok"


async def test_no_token_means_no_authorization_header() -> None:
    client = _FakeClient()
    await ServiceClient(client).get("/x")
    assert _auth(client.calls[-1]["headers"]) is None


async def test_default_and_per_call_headers_merge() -> None:
    client = _FakeClient()
    svc = ServiceClient(client, token="t", default_headers=((b"x-app", b"acme"),))
    await svc.get("/x", headers=((b"x-req", b"1"),))
    names = dict(client.calls[-1]["headers"])
    assert names[b"authorization"] == b"Bearer t"
    assert names[b"x-app"] == b"acme" and names[b"x-req"] == b"1"


async def test_verb_helpers_route_to_the_right_method() -> None:
    client = _FakeClient()
    svc = ServiceClient(client)
    await svc.put("/x", body=b"1")
    await svc.patch("/x", body=b"2")
    await svc.delete("/x")
    assert [c["method"] for c in client.calls] == ["PUT", "PATCH", "DELETE"]
