from __future__ import annotations

import base64
import json
from typing import Any

import pytest

import wreath._auth.oidc as oidc


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def header(self, name: bytes) -> None:
        return None


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def get(self, path: str) -> _Response:
        return self.response


def _provider(*, issuer: str = "https://idp.example", response: _Response | None = None):
    client = _Client(response or _Response(200, b'{"keys": []}'))
    return oidc.OidcProvider("idp", issuer=issuer, audience="api", http_client=client)


def _token(header: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    return f"{encoded}.e30."


@pytest.mark.parametrize(
    ("endpoint", "accepted_issuer"),
    [
        ("http://idp.example/resource", "https://idp.example:80"),
        ("https://user@idp.example/resource", "https://idp.example"),
        ("https://:password@idp.example/resource", "https://idp.example"),
        ("https://idp.example/resource#fragment", "https://idp.example"),
        ("https://idp.example:8444/resource", "https://idp.example:8443"),
    ],
)
def test_same_origin_refuses_each_independent_origin_difference(
    endpoint: str, accepted_issuer: str
) -> None:
    with pytest.raises(ValueError, match="not on the pinned issuer origin"):
        oidc._require_same_origin(accepted_issuer, endpoint)


@pytest.mark.parametrize(
    "issuer",
    [
        "http://idp.example:443",
        "https:",
        "https://user@idp.example",
        "https://:password@idp.example",
        "https://idp.example?tenant=acme",
        "https://idp.example#issuer",
    ],
)
def test_provider_refuses_each_invalid_issuer_form(issuer: str) -> None:
    with pytest.raises(
        ValueError,
        match="OIDC issuer must be an absolute HTTPS URL without credentials",
    ):
        _provider(issuer=issuer)


def test_same_origin_accepts_matching_explicit_non_default_ports() -> None:
    endpoint = "https://idp.example:8443/resource"

    assert oidc._require_same_origin("https://idp.example:8443", endpoint) == endpoint


def test_same_origin_path_supplies_root_and_omits_an_empty_query_marker() -> None:
    assert oidc._same_origin_path("https://idp.example", "https://idp.example") == "/"
    assert oidc._same_origin_path("https://idp.example", "https://idp.example/token") == "/token"


async def test_discovery_refuses_a_non_success_response() -> None:
    provider = _provider(response=_Response(503, b"unavailable"))

    with pytest.raises(RuntimeError, match="OIDC discovery for 'idp' failed: HTTP 503"):
        await provider.discover()


async def test_discovery_refuses_a_document_over_the_absolute_size_cap() -> None:
    document = {
        "issuer": "https://idp.example",
        "jwks_uri": "https://idp.example/jwks",
        "padding": "x" * 65_537,
    }
    provider = _provider(response=_Response(200, json.dumps(document).encode()))

    with pytest.raises(ValueError, match="OIDC discovery document exceeds size cap"):
        await provider.discover()


async def test_discovery_refuses_a_document_for_another_issuer() -> None:
    document = {
        "issuer": "https://other.example",
        "jwks_uri": "https://idp.example/jwks",
    }
    provider = _provider(response=_Response(200, json.dumps(document).encode()))

    with pytest.raises(
        ValueError,
        match="OIDC discovery 'issuer' does not match the configured issuer",
    ):
        await provider.discover()


async def test_default_bearer_verifier_uses_the_provider_audience(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class Cache:
        async def resolve(self, kid: str | None) -> object:
            return object()

    def capture_verify(token: str, **kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    provider = _provider()
    provider._cache = Cache()
    monkeypatch.setattr(oidc, "verify_jwt", capture_verify)

    assert await provider.bearer_verifier()(_token({"kid": "key-1"})) is not None
    assert seen["audiences"] == frozenset({"api"})


async def test_bearer_verifier_fails_closed_before_discovery() -> None:
    assert await _provider().bearer_verifier()(_token({"kid": "key-1"})) is None


async def test_bearer_verifier_fails_closed_for_a_malformed_header() -> None:
    class Cache:
        async def resolve(self, kid: str | None) -> object:
            raise AssertionError("a malformed header must not reach key resolution")

    provider = _provider()
    provider._cache = Cache()

    assert await provider.bearer_verifier()("not-base64!.payload.signature") is None


@pytest.mark.parametrize(("kid", "expected"), [("key-1", "key-1"), (7, None)])
async def test_bearer_verifier_normalizes_the_key_id(kid: object, expected: str | None) -> None:
    seen: list[str | None] = []

    class Cache:
        async def resolve(self, resolved_kid: str | None) -> None:
            seen.append(resolved_kid)
            return None

    provider = _provider()
    provider._cache = Cache()

    assert await provider.bearer_verifier()(_token({"kid": kid})) is None
    assert seen == [expected]


async def test_bearer_verifier_does_not_verify_without_a_resolved_key(monkeypatch) -> None:
    class Cache:
        async def resolve(self, kid: str | None) -> None:
            return None

    def unexpected_verify(token: str, **kwargs: Any) -> None:
        raise AssertionError("verification requires a resolved key")

    provider = _provider()
    provider._cache = Cache()
    monkeypatch.setattr(oidc, "verify_jwt", unexpected_verify)

    assert await provider.bearer_verifier()(_token({"kid": "missing"})) is None
