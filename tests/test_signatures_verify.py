from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from wreath import Request, Wreath
from wreath.signatures import (
    NonceLedger,
    SignatureError,
    SignatureFacts,
    Signatures,
    SigningKey,
    _digest_expectation,
    sign_request,
)
from wreath.state import BODY_CHECK_SLOT
from wreath.testing import TestClient

KEY_ID = "test-key-ed25519"
PRIVATE = base64.urlsafe_b64decode("n4Ni-HpISpVObnQMW0wOhCKROaIKqKtW_2ZYb2p9KcU=")
PUBLIC = base64.urlsafe_b64decode("JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs=")
DIRECTORY = "https://bot.example/.well-known/http-message-signatures-directory"


@pytest.mark.parametrize(
    "name",
    [
        b"signature-input",
        b"signature",
        b"signature-agent",
        b"host",
        b"content-digest",
    ],
)
def test_duplicate_singular_signature_fields_fail_before_nonce_claim(name: bytes) -> None:
    now = 1_700_000_000.0
    ledger = NonceLedger(max_entries=8, ttl=300.0)
    signatures = build(nonces=ledger, clock=lambda: now)
    headers = [
        (key.lower().encode("ascii"), value.encode("latin-1"))
        for key, value in signed_headers(clock=now, nonce="duplicate").items()
    ]
    originals = [value for key, value in headers if key == name]
    if originals:
        headers.append((name, originals[0]))
    else:
        headers.extend([(name, b"sha-256=:YQ==:"), (name, b"sha-256=:Yg==:")])
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/probe",
            "query_string": b"",
            "headers": headers,
        },
        None,
    )

    facts = signatures.facts(request)
    assert not facts.verified
    assert facts.reason == "signature request field occurs more than once"
    assert ledger.size == 0


def signer(agent: str | None = DIRECTORY) -> SigningKey:
    key = ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE)
    return SigningKey(key_id=KEY_ID, sign=key.sign, agent=agent)


@pytest.mark.parametrize(
    "changes",
    [
        {"key_id": "key\r\nInjected: yes"},
        {"agent": "https://bot.example/\r\nInjected: yes"},
    ],
)
def test_signing_key_refuses_header_control_characters(changes: dict[str, str]):
    values = {"key_id": KEY_ID, "sign": lambda _base: b"x" * 64, "agent": DIRECTORY}
    values.update(changes)
    with pytest.raises(ValueError, match="Structured Fields string"):
        SigningKey(**values)


def directory_document() -> dict:
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "kid": KEY_ID,
                "x": base64.urlsafe_b64encode(PUBLIC).rstrip(b"=").decode(),
            }
        ]
    }


def build(**kwargs) -> Signatures:
    # Keys installed directly, so startup must not reach the network.
    kwargs.setdefault("refresh_on_startup", False)
    signatures = Signatures(directories=(DIRECTORY,), **kwargs)
    signatures.install(DIRECTORY, directory_document())
    return signatures


def app_with(signatures: Signatures) -> Wreath:
    app = Wreath(signatures=signatures)

    @app.get("/probe")
    async def probe(request) -> dict:
        facts = signatures.facts(request)
        return {
            "verified": facts.verified,
            "agent": facts.agent,
            "reason": facts.reason,
        }

    return app


#: The test client sends no Host header of its own, so every signed request
#: here sets one explicitly -- exactly as a real HTTP client does. Without it
#: `@authority` cannot be built, which is itself asserted below.
HOST = "testserver"


def signed_headers(*, clock: float, path: str = "/probe", **kwargs) -> dict[str, str]:
    headers = sign_request(
        signer(),
        method="GET",
        url=f"https://{HOST}{path}",
        created=int(clock),
        **kwargs,
    )
    headers["Host"] = HOST
    return headers


async def test_a_signed_request_verifies_and_names_its_agent():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body == {"verified": True, "agent": DIRECTORY, "reason": None}
    assert signatures.verified == 1
    assert signatures.unverified == 0


async def test_signature_without_content_digest_parks_no_body_check():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    app = Wreath(signatures=signatures)

    @app.get("/state")
    async def state(request) -> dict:
        facts = signatures.facts(request)
        return {
            "verified": facts.verified,
            "body_check": request.state.get(BODY_CHECK_SLOT, "absent"),
        }

    async with TestClient(app) as client:
        body = (await client.get("/state", headers=signed_headers(clock=now, path="/state"))).json()
    assert body == {"verified": True, "body_check": "absent"}


def test_agentless_key_lookup_continues_past_a_directory_without_the_key():
    empty = "https://empty.example/.well-known/http-message-signatures-directory"
    signatures = Signatures(directories=(empty, DIRECTORY), refresh_on_startup=False)
    signatures.install(empty, {"keys": []})
    signatures.install(DIRECTORY, directory_document())

    assert signatures._key(None, KEY_ID) is not None


async def test_an_expiry_equal_to_the_current_time_is_still_valid():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now, expires_in=0)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["verified"] is True


async def test_an_agent_may_name_the_directory_origin():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now)
    headers["Signature-Agent"] = '"https://bot.example"'
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body == {
        "verified": True,
        "agent": "https://bot.example",
        "reason": None,
    }


@pytest.mark.parametrize(
    "agent",
    ['"https://bot.exampleX', 'Xhttps://bot.example"'],
)
async def test_mismatched_agent_quotes_are_not_stripped(agent: str):
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now)
    headers["Signature-Agent"] = agent
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["verified"] is False
    assert body["reason"] == "unknown signing key"


def test_signing_a_body_covers_its_digest_and_serializes_a_tag():
    headers = sign_request(
        signer(),
        method="POST",
        url="https://example.com/x",
        body=b"payload",
        tag="upload",
        created=1,
    )
    assert '"content-digest"' in headers["Signature-Input"]
    assert ';tag="upload"' in headers["Signature-Input"]
    assert headers["Content-Digest"].startswith("sha-256=:")


def test_signing_does_not_duplicate_an_explicit_digest_component():
    headers = sign_request(
        signer(),
        method="POST",
        url="https://example.com/x",
        body=b"payload",
        components=("@method", "content-digest"),
        created=1,
    )
    assert headers["Signature-Input"].count('"content-digest"') == 1


async def test_an_unsigned_request_is_a_fact_not_a_refusal():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        response = await client.get("/probe")
    assert response.status == 200
    assert response.json() == {"verified": False, "agent": None, "reason": "absent"}
    assert signatures.unverified == 0


def test_the_verifier_directly_recognizes_an_absent_signature_input():
    class EmptyRequest:
        @staticmethod
        def _index_headers():
            return {}

    facts = Signatures(refresh_on_startup=False)._verify(EmptyRequest())
    assert facts == SignatureFacts(reason="absent")


async def test_a_tampered_path_does_not_verify():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    app = app_with(signatures)

    @app.get("/other")
    async def other(request) -> dict:
        return {"verified": signatures.facts(request).verified}

    headers = signed_headers(clock=now)
    async with TestClient(app) as client:
        assert (await client.get("/other", headers=headers)).json()["verified"] is False


async def test_a_stale_created_is_outside_the_window():
    now = 1_700_000_000.0
    signatures = build(max_age=60.0, clock=lambda: now)
    headers = signed_headers(clock=now - 3600)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["verified"] is False
    assert body["reason"] == "signature created outside the accepted window"


async def test_a_future_created_is_refused_too():
    now = 1_700_000_000.0
    signatures = build(max_age=60.0, clock=lambda: now)
    headers = signed_headers(clock=now + 3600)
    async with TestClient(app_with(signatures)) as client:
        assert (await client.get("/probe", headers=headers)).json()["verified"] is False


@pytest.mark.parametrize("moment", [float("nan"), float("inf"), True, "now"])
async def test_a_signature_clock_must_return_a_finite_number(moment: object):
    now = 1_700_000_000.0
    signatures = build(clock=lambda: moment)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body["reason"] == "signature clock must return a finite number"


def test_the_default_signature_clock_uses_system_time(monkeypatch):
    monkeypatch.setattr("wreath.signatures.time.time", lambda: 42.5)

    assert Signatures()._now() == 42.5


async def test_an_expired_signature_is_refused():
    now = 1_700_000_000.0
    signatures = build(max_age=600.0, clock=lambda: now)
    headers = signed_headers(clock=now - 100, expires_in=10)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature has expired"


async def test_a_signature_that_covers_too_little_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        signer(),
        method="GET",
        url=f"https://{HOST}/probe",
        headers={"date": "Tue, 20 Apr 2021 02:07:55 GMT"},
        components=("date",),
        created=int(now),
    )
    headers["date"] = "Tue, 20 Apr 2021 02:07:55 GMT"
    headers["Host"] = HOST
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature does not cover @method"


async def test_an_unknown_key_is_not_fetched_and_does_not_verify():
    now = 1_700_000_000.0
    signatures = Signatures(directories=(DIRECTORY,), clock=lambda: now, refresh_on_startup=False)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body["reason"] == "unknown signing key"


async def test_a_key_from_another_operators_directory_is_not_used():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        SigningKey(
            key_id=KEY_ID,
            sign=ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE).sign,
            agent="https://elsewhere.example/.well-known/x",
        ),
        method="GET",
        url=f"https://{HOST}/probe",
        created=int(now),
    )
    headers["Host"] = HOST
    async with TestClient(app_with(signatures)) as client:
        assert (await client.get("/probe", headers=headers)).json()["reason"] == (
            "unknown signing key"
        )


async def test_an_unsupported_algorithm_is_refused_by_name():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now)
    headers["Signature-Input"] = headers["Signature-Input"].replace(
        'alg="ed25519"', 'alg="rsa-pss-sha512"'
    )
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "unsupported signature algorithm 'rsa-pss-sha512'"


async def test_the_same_signed_request_twice_is_refused_the_second_time():
    now = 1_700_000_000.0
    ledger = NonceLedger(max_entries=8, ttl=300.0)
    signatures = build(nonces=ledger, clock=lambda: now)
    headers = signed_headers(clock=now, nonce="n-1")
    async with TestClient(app_with(signatures)) as client:
        first = (await client.get("/probe", headers=headers)).json()
        second = (await client.get("/probe", headers=headers)).json()
    assert first["verified"] is True
    assert second["verified"] is False
    assert second["reason"] == "signature nonce was already used"
    assert ledger.replays == 1


async def test_nonce_ledger_is_scoped_by_the_verified_public_key():
    now = 1_700_000_000.0
    other_directory = "https://other.example/.well-known/http-message-signatures-directory"
    other_private = ed25519.Ed25519PrivateKey.generate()
    other_public = other_private.public_key().public_bytes_raw()
    signatures = Signatures(
        directories=(DIRECTORY, other_directory),
        nonces=NonceLedger(max_entries=8, ttl=300.0),
        clock=lambda: now,
        refresh_on_startup=False,
    )
    signatures.install(DIRECTORY, directory_document())
    signatures.install(
        other_directory,
        {
            "keys": [
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "kid": KEY_ID,
                    "x": base64.urlsafe_b64encode(other_public).rstrip(b"=").decode(),
                }
            ]
        },
    )
    first = sign_request(
        signer(),
        method="GET",
        url=f"https://{HOST}/probe",
        created=int(now),
        nonce="shared-nonce",
    )
    first["Host"] = HOST
    second = sign_request(
        SigningKey(key_id=KEY_ID, sign=other_private.sign, agent=other_directory),
        method="GET",
        url=f"https://{HOST}/probe",
        created=int(now),
        nonce="shared-nonce",
    )
    second["Host"] = HOST

    async with TestClient(app_with(signatures)) as client:
        first_result = (await client.get("/probe", headers=first)).json()
        second_result = (await client.get("/probe", headers=second)).json()

    assert first_result["verified"] is True
    assert second_result["verified"] is True


async def test_a_signature_without_a_nonce_is_refused_when_a_ledger_is_configured():
    now = 1_700_000_000.0
    signatures = build(nonces=NonceLedger(), clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body["reason"] == "signature has no nonce"


async def test_a_full_nonce_ledger_refuses_rather_than_evicting():
    ledger = NonceLedger(max_entries=2, ttl=300.0)
    assert ledger.claim("a") is True
    assert ledger.claim("b") is True
    assert ledger.claim("c") is False  # full: refused, not evicted
    assert ledger.refusals == 1
    # And the displaced-nothing check: "a" is still remembered.
    assert ledger.claim("a") is False
    assert ledger.replays == 1


async def test_a_full_nonce_ledger_cannot_verify_a_fresh_signed_request():
    now = 1_700_000_000.0
    ledger = NonceLedger(max_entries=1, ttl=300.0)
    assert ledger.claim("occupied") is True
    signatures = build(nonces=ledger, clock=lambda: now)

    async with TestClient(app_with(signatures)) as client:
        body = (
            await client.get("/probe", headers=signed_headers(clock=now, nonce="fresh-nonce"))
        ).json()

    assert body["verified"] is False
    assert body["reason"] == "signature nonce was already used"
    assert ledger.refusals == 1


async def test_a_nonce_is_forgotten_after_its_ttl():
    ledger = NonceLedger(max_entries=4, ttl=10.0)
    assert ledger.claim("a", now=0.0) is True
    assert ledger.claim("a", now=5.0) is False
    assert ledger.claim("a", now=20.0) is True


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"max_entries": 0}, "nonce ledger max_entries must be positive"),
        ({"ttl": 0.0}, "nonce ledger ttl must be positive"),
    ],
)
async def test_nonce_ledger_bounds_must_be_positive(bad, message):
    with pytest.raises(ValueError, match=message):
        NonceLedger(**bad)


def test_missing_and_non_byte_content_digests_are_refused_cleanly():
    with pytest.raises(SignatureError, match="is not present"):
        _digest_expectation(None)
    with pytest.raises(SignatureError, match="byte sequence"):
        _digest_expectation(b"sha-256=1")


async def test_an_unknown_profile_is_refused_at_construction():
    with pytest.raises(ValueError, match="unknown signature profile"):
        Signatures(profile="web-bot-auth-2029")


async def test_a_plaintext_directory_is_refused():
    with pytest.raises(ValueError, match="must be https"):
        Signatures(directories=("http://bot.example/x",))


@pytest.mark.parametrize(
    "url",
    (
        "https:///directory",
        "https://operator@bot.example/directory",
        "https://bot.example:0/directory",
        "https://bot.example:invalid/directory",
        "https://bot.example/directory#fragment",
        "https://trusted.example\\@evil.example/directory",
        "https://bot.example/direc\ntory",
        "https://bot.example/directory\x7f",
        "https://bot.example/directory\x80",
    ),
)
def test_malformed_signature_directory_is_refused_at_construction(url):
    with pytest.raises(ValueError, match="must be https, absolute"):
        Signatures(directories=(url,))


@pytest.mark.parametrize("max_age", [0, float("nan"), float("inf"), True, "60"])
async def test_max_age_must_be_finite_and_positive(max_age):
    with pytest.raises(ValueError, match="finite and positive"):
        Signatures(max_age=max_age)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"refresh_on_startup": 1}, "refresh_on_startup must be a bool"),
        ({"clock": 1}, "clock must be callable or None"),
    ],
)
def test_signature_runtime_configuration_refuses_wrong_types(
    options: dict[str, object], message: str
):
    with pytest.raises(ValueError, match=message):
        Signatures(**options)


async def test_installing_an_unconfigured_directory_raises():
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    with pytest.raises(KeyError):
        signatures.install("https://other.example/x", directory_document())


async def test_a_malformed_key_does_not_blind_the_directory():
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    installed = signatures.install(
        DIRECTORY,
        {
            "keys": [
                {"kty": "OKP", "crv": "P-256", "kid": "wrong-curve", "x": "AA"},
                {"kty": "nonsense"},
                json.loads(json.dumps(directory_document()["keys"][0])),
            ]
        },
    )
    assert installed == 1


async def test_signing_and_verifying_agree():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body["verified"] is True


async def test_sign_request_needs_an_absolute_url():
    with pytest.raises(ValueError, match="absolute url"):
        sign_request(signer(), method="GET", url="/relative")


async def test_a_signer_returning_the_wrong_length_is_refused():
    key = SigningKey(key_id="k", sign=lambda _base: b"short")
    with pytest.raises(SignatureError, match="64 bytes"):
        sign_request(key, method="GET", url="https://h/x")


async def test_cedar_context_always_carries_a_boolean():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    app = Wreath(signatures=signatures)

    @app.get("/ctx")
    async def ctx(request) -> dict:
        return dict(signatures.cedar_context(request))

    async with TestClient(app) as client:
        unsigned = (await client.get("/ctx")).json()
        signed = (await client.get("/ctx", headers=signed_headers(clock=now, path="/ctx"))).json()
    assert unsigned == {"signature_verified": False}
    assert signed == {
        "signature_verified": True,
        "signature_agent": DIRECTORY,
        "signature_covered": ["@method", "@authority", "@path", "@query"],
    }


def test_cedar_context_omits_an_absent_agent_from_verified_facts():
    class FixedFacts(Signatures):
        def facts(self, request):
            return SignatureFacts(verified=True, covered=("@method",))

    context = FixedFacts(refresh_on_startup=False).cedar_context(object())
    assert context == {
        "signature_verified": True,
        "signature_covered": ["@method"],
    }


async def test_facts_are_resolved_once_per_request():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    app = Wreath(signatures=signatures)

    @app.get("/twice")
    async def twice(request) -> dict:
        first = signatures.facts(request)
        second = signatures.facts(request)
        return {"same": first is second}

    async with TestClient(app) as client:
        headers = signed_headers(clock=now, path="/twice")
        assert (await client.get("/twice", headers=headers)).json() == {"same": True}
    # One verification, not two, for one request.
    assert signatures.verified == 1


class _StubResponse:
    def __init__(self, content: bytes, *, streamed: bool = False) -> None:
        self.content = None if streamed else content
        self._body = content
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        return self._body


class _StubClient:
    """Enough of `HTTPClient` for the refresh path, with no socket."""

    def __init__(
        self,
        body: bytes | None = None,
        error: Exception | None = None,
        *,
        streamed: bool = False,
    ):
        self.body = body
        self.error = error
        self.streamed = streamed
        self.requested: list[str] = []
        self.closed = False
        self.response: _StubResponse | None = None

    async def get(self, path: str):
        self.requested.append(path)
        if self.error is not None:
            raise self.error
        self.response = _StubResponse(self.body or b"{}", streamed=self.streamed)
        return self.response

    async def aclose(self) -> None:
        self.closed = True


async def test_refresh_installs_keys_and_asks_the_directory_path():
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    client = _StubClient(json.dumps(directory_document()).encode())
    installed = await signatures.refresh(client_factory=lambda origin: client)
    assert installed == 1
    assert client.requested == ["/.well-known/http-message-signatures-directory"]
    assert client.closed is True
    assert signatures.refreshes == 1
    assert signatures.refresh_errors == 0
    assert client.response is not None and client.response.reads == 0


@pytest.mark.parametrize(
    ("url", "expected_origin", "expected_path"),
    (
        ("https://bot.example", "https://bot.example", "/"),
        (
            "https://[2001:db8::1]:8443/keys?tenant=one",
            "https://[2001:db8::1]:8443",
            "/keys?tenant=one",
        ),
    ),
)
async def test_directory_refresh_preserves_a_valid_origin_and_target(
    url, expected_origin, expected_path
):
    signatures = Signatures(directories=(url,), refresh_on_startup=False)
    client = _StubClient(json.dumps(directory_document()).encode())
    origins = []

    await signatures.refresh(client_factory=lambda origin: origins.append(origin) or client)

    assert origins == [expected_origin]
    assert client.requested == [expected_path]


async def test_refresh_reads_a_streamed_directory_response():
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    client = _StubClient(json.dumps(directory_document()).encode(), streamed=True)

    installed = await signatures.refresh(client_factory=lambda origin: client)

    assert installed == 1
    assert signatures.refresh_errors == 0
    assert client.response is not None and client.response.reads == 1


async def test_a_failed_refresh_keeps_the_previous_keys_and_counts():
    signatures = build()
    client = _StubClient(error=OSError("connection refused"))
    installed = await signatures.refresh(client_factory=lambda origin: client)
    assert installed == 0
    assert signatures.refresh_errors == 1
    assert client.closed is True
    # The keys installed before the failed refresh are still there.
    now = 1_700_000_000.0
    signatures._clock = lambda: now
    async with TestClient(app_with(signatures)) as client_app:
        body = (await client_app.get("/probe", headers=signed_headers(clock=now))).json()
    assert body["verified"] is True


async def test_an_oversized_directory_document_is_refused():
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    huge = b'{"keys":[]}' + b" " * (512 * 1024 + 1)
    await signatures.refresh(client_factory=lambda origin: _StubClient(huge))
    assert signatures.refresh_errors == 1


async def test_refresh_with_no_directories_does_nothing():
    signatures = Signatures(refresh_on_startup=False)
    assert await signatures.refresh() == 0


async def test_refresh_accepts_a_client_without_a_close_hook():
    class Client:
        async def get(self, path):
            return _StubResponse(json.dumps(directory_document()).encode())

    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    assert await signatures.refresh(client_factory=lambda origin: Client()) == 1


async def test_refresh_uses_the_default_directory_client_factory(monkeypatch):
    calls = []
    client = _StubClient(json.dumps(directory_document()).encode())

    def factory(name, *, base_url):
        calls.append((name, base_url))
        return client

    monkeypatch.setattr("wreath.http_client.HTTPClient", factory)
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    assert await signatures.refresh() == 1
    assert calls == [("wreath-signature-directory", "https://bot.example")]


async def test_a_non_object_directory_document_is_counted_as_a_refresh_error():
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    await signatures.refresh(client_factory=lambda origin: _StubClient(b"[]"))
    assert signatures.refresh_errors == 1


async def test_a_bad_keyid_never_reaches_the_verify(monkeypatch):
    calls = 0
    import wreath._auth._ecverify as ecverify

    real = ecverify.verify_ed25519

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(ecverify, "verify_ed25519", counting)

    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now)
    headers["Signature-Input"] = headers["Signature-Input"].replace(
        'keyid="test-key-ed25519"', 'keyid="not-a-key-we-hold"'
    )
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "unknown signing key"
    assert calls == 0, "an unknown keyid must be refused before the signature check"


async def test_a_stale_signature_never_reaches_the_verify(monkeypatch):
    calls = 0
    import wreath._auth._ecverify as ecverify

    real = ecverify.verify_ed25519

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(ecverify, "verify_ed25519", counting)

    now = 1_700_000_000.0
    signatures = build(max_age=60.0, clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        await client.get("/probe", headers=signed_headers(clock=now - 3600))
    assert calls == 0


async def test_a_replayed_nonce_never_reaches_the_verify(monkeypatch):
    now = 1_700_000_000.0
    signatures = build(nonces=NonceLedger(max_entries=8, ttl=300.0), clock=lambda: now)
    headers = signed_headers(clock=now, nonce="n-cost")
    async with TestClient(app_with(signatures)) as client:
        await client.get("/probe", headers=headers)

        calls = 0
        import wreath._auth._ecverify as ecverify

        real = ecverify.verify_ed25519

        def counting(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(ecverify, "verify_ed25519", counting)
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature nonce was already used"
    assert calls == 0


async def test_the_verify_counter_harness_actually_counts(monkeypatch):
    calls = 0
    import wreath._auth._ecverify as ecverify

    real = ecverify.verify_ed25519

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(ecverify, "verify_ed25519", counting)

    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body["verified"] is True
    assert calls == 1


# Every parameter below is parsed straight out of an attacker-controlled header,
# so each wrong *type* is a reachable path rather than a defensive nicety.
# Mutation testing found all four of these untested: the refusals were there and
# nothing objected when they were removed.


def _mangle(headers: dict[str, str], old: str, new: str) -> dict[str, str]:
    headers = dict(headers)
    headers["Signature-Input"] = headers["Signature-Input"].replace(old, new)
    return headers


async def test_a_non_integer_created_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = _mangle(signed_headers(clock=now), "created=1700000000", 'created="soon"')
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature has no created parameter"


async def test_a_boolean_created_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = _mangle(signed_headers(clock=now), "created=1700000000", "created=?1")
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature has no created parameter"


@pytest.mark.parametrize("replacement", ['expires="later"', "expires=?1"])
async def test_a_non_integer_expires_is_refused(replacement: str):
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = _mangle(
        signed_headers(clock=now, expires_in=60),
        "expires=1700000060",
        replacement,
    )
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature expires parameter must be an integer"


async def test_a_missing_created_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = _mangle(signed_headers(clock=now), "created=1700000000;", "")
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature has no created parameter"


async def test_a_non_string_keyid_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = _mangle(signed_headers(clock=now), 'keyid="test-key-ed25519"', "keyid=7")
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature has no keyid"


async def test_a_non_string_alg_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = _mangle(signed_headers(clock=now), 'alg="ed25519"', "alg=7")
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "unsupported signature algorithm 7"


async def test_a_signature_that_is_not_a_byte_sequence_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now)
    headers["Signature"] = 'sig1="not-a-byte-sequence"'
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature value must be a byte sequence"


async def test_labels_must_appear_in_both_headers():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now)
    headers["Signature"] = headers["Signature"].replace("sig1=", "other=")
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "no label appears in both signature headers"


async def test_a_signature_input_without_a_signature_header_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now)
    del headers["Signature"]
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "no-signature-header"
    assert signatures.unverified == 1


async def test_an_unquoted_signature_agent_is_accepted():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = signed_headers(clock=now)
    headers["Signature-Agent"] = DIRECTORY  # no surrounding quotes
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body == {"verified": True, "agent": DIRECTORY, "reason": None}


async def test_a_signature_with_no_agent_falls_back_to_any_directory():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        SigningKey(
            key_id=KEY_ID,
            sign=ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE).sign,
            agent=None,
        ),
        method="GET",
        url=f"https://{HOST}/probe",
        created=int(now),
    )
    headers["Host"] = HOST
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["verified"] is True
    assert body["agent"] is None


async def test_a_signature_with_no_alg_parameter_still_verifies():
    from wreath.signatures import RequestMessage, signature_base

    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    key = ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE)
    components = (("@method", {}), ("@authority", {}), ("@path", {}), ("@query", {}))
    params = {"created": int(now), "keyid": KEY_ID}
    message = RequestMessage(
        method="GET",
        scheme="https",
        authority=HOST,
        path="/probe",
        headers={b"host": HOST.encode()},
    )
    signature = key.sign(signature_base(message, components, params))
    headers = {
        "Host": HOST,
        "Signature-Input": (
            f'sig1=("@method" "@authority" "@path" "@query");created={int(now)};keyid="{KEY_ID}"'
        ),
        "Signature": f"sig1=:{base64.b64encode(signature).decode()}:",
        "Signature-Agent": DIRECTORY,
    }
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["verified"] is True


# A signature over method+authority+path is a signature over an *endpoint*, not
# over a request: everything a caller can vary without touching those three --
# the query string and the whole body -- rides along uncovered. Both halves are
# reachable by anyone who observes one signed request.


def app_with_echo(signatures: Signatures) -> Wreath:
    """`app_with`, plus a route that reads the body the way a handler does."""
    app = app_with(signatures)

    @app.post("/echo")
    async def echo(request) -> dict:
        payload = await request.body()
        return {
            "verified": signatures.facts(request).verified,
            "reason": signatures.facts(request).reason,
            "body": payload.decode(),
        }

    return app


async def test_a_signed_request_is_not_replayable_against_another_query():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        signer(), method="GET", url=f"https://{HOST}/probe?x=1", created=int(now)
    )
    headers["Host"] = HOST
    async with TestClient(app_with(signatures)) as client:
        honest = (await client.get("/probe?x=1", headers=headers)).json()
        replayed = (await client.get("/probe?admin=1", headers=headers)).json()
    assert honest["verified"] is True
    assert replayed["verified"] is False
    assert replayed["reason"] == "signature does not verify"


async def test_a_signature_that_omits_the_query_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        signer(),
        method="GET",
        url=f"https://{HOST}/probe",
        components=("@method", "@authority", "@path"),
        created=int(now),
    )
    headers["Host"] = HOST
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature does not cover @query"


async def test_a_signed_post_must_cover_a_content_digest():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        signer(),
        method="POST",
        url=f"https://{HOST}/echo",
        components=("@method", "@authority", "@path", "@query"),
        created=int(now),
    )
    headers["Host"] = HOST
    async with TestClient(app_with_echo(signatures)) as client:
        body = (await client.post("/echo", content=b"honest", headers=headers)).json()
    assert body["verified"] is False
    assert body["reason"] == "signature does not cover content-digest"


async def test_a_swapped_body_under_a_covered_digest_is_refused():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        signer(),
        method="POST",
        url=f"https://{HOST}/echo",
        body=b"honest",
        created=int(now),
    )
    headers["Host"] = HOST
    async with TestClient(app_with_echo(signatures)) as client:
        honest = await client.post("/echo", content=b"honest", headers=headers)
        swapped = await client.post("/echo", content=b"forged", headers=headers)
    assert honest.status == 200, honest.json()
    assert honest.json()["verified"] is True
    assert swapped.status == 400


async def test_the_covered_component_set_reaches_a_policy():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        signer(),
        method="POST",
        url=f"https://{HOST}/context",
        body=b"honest",
        created=int(now),
    )
    headers["Host"] = HOST
    app = app_with_echo(signatures)

    seen: dict = {}

    @app.post("/context")
    async def context(request) -> dict:
        seen.update(signatures.cedar_context(request))
        return {"ok": True}

    async with TestClient(app) as client:
        await client.post("/context", content=b"honest", headers=headers)
    assert seen["signature_verified"] is True
    assert "content-digest" in seen["signature_covered"]
    assert "@query" in seen["signature_covered"]


async def test_an_unverifiable_signature_does_not_fill_the_nonce_ledger():
    now = 1_700_000_000.0
    ledger = NonceLedger(max_entries=4, ttl=300.0)
    signatures = build(nonces=ledger, clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        for index in range(8):
            headers = signed_headers(clock=now, nonce=f"flood-{index}")
            headers["Signature"] = "sig1=:" + base64.b64encode(b"\x00" * 64).decode() + ":"
            refused = (await client.get("/probe", headers=headers)).json()
            assert refused["reason"] == "signature does not verify"
    assert ledger.size == 0
    assert ledger.refusals == 0


async def test_a_nonce_cannot_be_burned_by_an_unverifiable_request():
    now = 1_700_000_000.0
    ledger = NonceLedger(max_entries=8, ttl=300.0)
    signatures = build(nonces=ledger, clock=lambda: now)
    honest = signed_headers(clock=now, nonce="n-victim")
    forged = dict(honest)
    forged["Signature"] = "sig1=:" + base64.b64encode(b"\x00" * 64).decode() + ":"
    async with TestClient(app_with(signatures)) as client:
        assert (await client.get("/probe", headers=forged)).json()["verified"] is False
        assert (await client.get("/probe", headers=honest)).json()["verified"] is True


async def test_a_streaming_handler_gets_the_same_body_guarantee():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    headers = sign_request(
        signer(),
        method="POST",
        url=f"https://{HOST}/drain",
        body=b"honest",
        created=int(now),
    )
    headers["Host"] = HOST
    app = app_with(signatures)

    @app.post("/drain")
    async def drain(request) -> dict:
        seen = 0
        async for chunk in request.stream():
            seen += len(chunk)
        return {"seen": seen}

    async with TestClient(app) as client:
        honest = await client.post("/drain", content=b"honest", headers=headers)
        swapped = await client.post("/drain", content=b"forged", headers=headers)
    assert honest.status == 200, honest.json()
    assert honest.json() == {"seen": 6}
    assert swapped.status == 400
