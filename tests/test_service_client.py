from __future__ import annotations

import pytest

from wreath.service_client import ServiceClient

pytestmark = pytest.mark.asyncio


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, method, target, *, headers=(), body=b"", idempotency_key=None):
        self.calls.append(
            {
                "method": method,
                "target": target,
                "headers": headers,
                "body": body,
                "idempotency_key": idempotency_key,
            }
        )
        return "response"


def _auth(headers) -> bytes | None:
    return dict(headers).get(b"authorization")


async def test_base_path_is_prefixed() -> None:
    client = _FakeClient()
    svc = ServiceClient(client, base_path="/billing/v1")
    await svc.get("/invoices/7")
    assert client.calls[-1]["target"] == "/billing/v1/invoices/7"
    await svc.get("invoices/8")  # missing leading slash still joins cleanly
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
    assert _auth(client.calls[1]["headers"]) == b"Bearer tok-2"  # re-asked each call


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


async def test_no_token_refuses_multiple_caller_authorization_headers() -> None:
    client = _FakeClient()
    service = ServiceClient(client)

    with pytest.raises(ValueError, match="more than one Authorization"):
        await service.get(
            "/x",
            headers=(
                (b"authorization", b"Bearer first"),
                (b"Authorization", b"Bearer second"),
            ),
        )

    assert client.calls == []


async def test_no_token_refuses_multiple_default_authorization_headers() -> None:
    with pytest.raises(ValueError, match="more than one Authorization"):
        ServiceClient(
            _FakeClient(),
            default_headers=(
                (b"authorization", b"Bearer first"),
                (b"Authorization", b"Bearer second"),
            ),
        )


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


# `f"Bearer {value}".encode("latin-1")` had three distinct wrong answers, and
# the docstring named none of them: CRLF spliced a second header into the
# request, U+0080-U+00FF encoded to bytes no peer decodes back, and anything
# above U+00FF raised UnicodeEncodeError from inside `encode`. A bearer token is
# printable ASCII (RFC 6750), so all three are the same precondition.


async def test_a_token_carrying_crlf_is_refused_not_spliced() -> None:
    client = _FakeClient()
    svc = ServiceClient(client, token="abc\r\nX-Admin: true")
    with pytest.raises(ValueError, match=r"splice a second header"):
        await svc.get("/x")
    assert client.calls == [], "the request went out despite an unusable token"


@pytest.mark.parametrize(
    ("token", "shown"),
    [
        ("tökén", "'ö'"),  # latin-1 encoded this silently, and wrongly
        ("токен", "'т'"),  # this raised UnicodeEncodeError from `encode`
        ("abc\tdef", "'\\t'"),  # a tab is ASCII but not printable
        ("abc\x7f", "'\\x7f'"),  # DEL
    ],
)
async def test_a_non_ascii_or_unprintable_token_is_refused(token, shown) -> None:
    client = _FakeClient()
    svc = ServiceClient(client, token=token)
    with pytest.raises(ValueError, match="printable ASCII") as caught:
        await svc.get("/x")
    assert shown in str(caught.value), "the message must name the offending character"
    assert "position" in str(caught.value)
    assert client.calls == []


async def test_the_refusal_names_a_refreshing_provider_as_the_source() -> None:
    class _Creds:
        async def token(self) -> str:
            return "bad\nvalue"

    svc = ServiceClient(_FakeClient(), token=_Creds())
    with pytest.raises(ValueError, match=r"_Creds\.token\(\)"):
        await svc.get("/x")


async def test_an_ordinary_token_is_unaffected() -> None:
    client = _FakeClient()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.-_~+/=abcDEF123"
    svc = ServiceClient(client, token=jwt)
    await svc.get("/x")
    assert _auth(client.calls[-1]["headers"]) == f"Bearer {jwt}".encode("ascii")
