"""Verification at ingress: the facts published, and every way it must refuse.

The controls under test are declared ones -- the skew window, the required
component set, the algorithm allow-list, the nonce ledger and its full-ledger
behaviour -- so each has a test that goes red when the control is removed.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from wreath import Wreath
from wreath.signatures import (
    NonceLedger,
    SignatureError,
    Signatures,
    SigningKey,
    sign_request,
)
from wreath.testing import TestClient

KEY_ID = "test-key-ed25519"
PRIVATE = base64.urlsafe_b64decode("n4Ni-HpISpVObnQMW0wOhCKROaIKqKtW_2ZYb2p9KcU=")
PUBLIC = base64.urlsafe_b64decode("JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs=")
DIRECTORY = "https://bot.example/.well-known/http-message-signatures-directory"


def signer(agent: str | None = DIRECTORY) -> SigningKey:
    key = ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE)
    return SigningKey(key_id=KEY_ID, sign=key.sign, agent=agent)


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


# --- the happy path, and that it is actually doing work ---------------------


async def test_a_signed_request_verifies_and_names_its_agent():
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body == {"verified": True, "agent": DIRECTORY, "reason": None}
    assert signatures.verified == 1
    assert signatures.unverified == 0


async def test_an_unsigned_request_is_a_fact_not_a_refusal():
    """No signature is normal. It must not 4xx, and must not be counted."""
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        response = await client.get("/probe")
    assert response.status == 200
    assert response.json() == {"verified": False, "agent": None, "reason": "absent"}
    assert signatures.unverified == 0


async def test_a_tampered_path_does_not_verify():
    """The signature covers @path; changing it must break verification."""
    now = 1_700_000_000.0
    signatures = build(clock=lambda: now)
    app = app_with(signatures)

    @app.get("/other")
    async def other(request) -> dict:
        return {"verified": signatures.facts(request).verified}

    headers = signed_headers(clock=now)
    async with TestClient(app) as client:
        assert (await client.get("/other", headers=headers)).json()["verified"] is False


# --- the declared controls --------------------------------------------------


async def test_a_stale_created_is_outside_the_window():
    now = 1_700_000_000.0
    signatures = build(max_age=60.0, clock=lambda: now)
    headers = signed_headers(clock=now - 3600)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["verified"] is False
    assert body["reason"] == "signature created outside the accepted window"


async def test_a_future_created_is_refused_too():
    """The window is two-sided: a forged forward timestamp buys nothing."""
    now = 1_700_000_000.0
    signatures = build(max_age=60.0, clock=lambda: now)
    headers = signed_headers(clock=now + 3600)
    async with TestClient(app_with(signatures)) as client:
        assert (await client.get("/probe", headers=headers)).json()["verified"] is False


async def test_an_expired_signature_is_refused():
    now = 1_700_000_000.0
    signatures = build(max_age=600.0, clock=lambda: now)
    headers = signed_headers(clock=now - 100, expires_in=10)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["reason"] == "signature has expired"


async def test_a_signature_that_covers_too_little_is_refused():
    """A signature over `date` alone replays against every endpoint."""
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
    """No outbound request may be provoked by an inbound `keyid`."""
    now = 1_700_000_000.0
    signatures = Signatures(
        directories=(DIRECTORY,), clock=lambda: now, refresh_on_startup=False
    )
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body["reason"] == "unknown signing key"


async def test_a_key_from_another_operators_directory_is_not_used():
    """`Signature-Agent` pins which directory may satisfy the signature."""
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


# --- replay -----------------------------------------------------------------


async def test_the_same_signed_request_twice_is_refused_the_second_time():
    now = 1_700_000_000.0
    signatures = build(nonces=NonceLedger(max_entries=8, ttl=300.0), clock=lambda: now)
    headers = signed_headers(clock=now, nonce="n-1")
    async with TestClient(app_with(signatures)) as client:
        first = (await client.get("/probe", headers=headers)).json()
        second = (await client.get("/probe", headers=headers)).json()
    assert first["verified"] is True
    assert second["verified"] is False
    assert second["reason"] == "signature nonce was already used"
    assert signatures.nonces.replays == 1


async def test_a_signature_without_a_nonce_is_refused_when_a_ledger_is_configured():
    now = 1_700_000_000.0
    signatures = build(nonces=NonceLedger(), clock=lambda: now)
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=signed_headers(clock=now))).json()
    assert body["reason"] == "signature has no nonce"


async def test_a_full_nonce_ledger_refuses_rather_than_evicting():
    """Fail closed.

    `KV` would evict the least recently used nonce to make room, which lets an
    attacker flush the ledger and replay what it displaced. The ledger must
    refuse instead, and count it.
    """
    ledger = NonceLedger(max_entries=2, ttl=300.0)
    assert ledger.claim("a") is True
    assert ledger.claim("b") is True
    assert ledger.claim("c") is False  # full: refused, not evicted
    assert ledger.refusals == 1
    # And the displaced-nothing check: "a" is still remembered.
    assert ledger.claim("a") is False
    assert ledger.replays == 1


async def test_a_nonce_is_forgotten_after_its_ttl():
    ledger = NonceLedger(max_entries=4, ttl=10.0)
    assert ledger.claim("a", now=0.0) is True
    assert ledger.claim("a", now=5.0) is False
    assert ledger.claim("a", now=20.0) is True


@pytest.mark.parametrize("bad", [{"max_entries": 0}, {"ttl": 0.0}])
async def test_nonce_ledger_bounds_must_be_positive(bad):
    with pytest.raises(ValueError):
        NonceLedger(**bad)


# --- configuration ----------------------------------------------------------


async def test_an_unknown_profile_is_refused_at_construction():
    with pytest.raises(ValueError, match="unknown signature profile"):
        Signatures(profile="web-bot-auth-2029")


async def test_a_plaintext_directory_is_refused():
    with pytest.raises(ValueError, match="must be https"):
        Signatures(directories=("http://bot.example/x",))


async def test_max_age_must_be_positive():
    with pytest.raises(ValueError):
        Signatures(max_age=0)


async def test_installing_an_unconfigured_directory_raises():
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    with pytest.raises(KeyError):
        signatures.install("https://other.example/x", directory_document())


async def test_a_malformed_key_does_not_blind_the_directory():
    """One unreadable JWK is the operator's problem, not an outage."""
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


# --- signing ----------------------------------------------------------------


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


# --- the Cedar facts --------------------------------------------------------


async def test_cedar_context_always_carries_a_boolean():
    """Both `when` and `unless` policy shapes must read alike on an unsigned
    request, so the key is always present."""
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


# --- refresh, which is the only thing that touches the network --------------


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
        self, body: bytes | None = None, error: Exception | None = None, *,
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


async def test_refresh_reads_a_streamed_directory_response():
    signatures = Signatures(directories=(DIRECTORY,), refresh_on_startup=False)
    client = _StubClient(json.dumps(directory_document()).encode(), streamed=True)

    installed = await signatures.refresh(client_factory=lambda origin: client)

    assert installed == 1
    assert signatures.refresh_errors == 0
    assert client.response is not None and client.response.reads == 1


async def test_a_failed_refresh_keeps_the_previous_keys_and_counts():
    """A transient fault must not silently unverify every legitimate agent."""
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


# --- the cost ordering ------------------------------------------------------


async def test_a_bad_keyid_never_reaches_the_verify(monkeypatch):
    """The cheap refusals must all sit above `verify_ed25519`.

    Measured: parsing is ~17us and one Ed25519 verification is ~2459us, so a
    caller who fails a cheap check must not pay the expensive one. Nothing
    about *correctness* would notice if the verify moved up, which is why this
    test is about the cost instead.
    """
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
    """Falsifies the three tests above.

    Each asserts `calls == 0`, which also passes if the monkeypatch never took
    effect. This one asserts the counter reaches 1 on a request that *should*
    reach the verify, so a broken patch fails here instead of silently making
    the cost tests vacuous.
    """
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


# --- malformed signature parameters -----------------------------------------
#
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
    """`alg=7` must not slip past the allow-list by not being a string."""
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
    """`Signature-Agent` is a structured-field string, but senders differ on
    whether they quote it. Both spellings must name the same directory."""
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
    """`alg` is advisory -- the key's own family decides -- and many senders
    omit it. Signed by hand rather than mangled, because the parameters are
    themselves covered by `@signature-params`: editing the header after signing
    invalidates the signature, which is the protocol working correctly and not
    a way to test this.
    """
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
            f'sig1=("@method" "@authority" "@path" "@query");'
            f'created={int(now)};keyid="{KEY_ID}"'
        ),
        "Signature": f"sig1=:{base64.b64encode(signature).decode()}:",
        "Signature-Agent": DIRECTORY,
    }
    async with TestClient(app_with(signatures)) as client:
        body = (await client.get("/probe", headers=headers)).json()
    assert body["verified"] is True


# --- what the signature actually covers -------------------------------------
#
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
    """`@query` is required, so the query string is part of what was signed."""
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
    """The completeness check names it, rather than verifying a partial cover."""
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
    """A body nothing covers is a body anyone may swap."""
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
    """Covering the header is worth nothing unless the digest is recomputed.

    The whole attack: `Content-Digest` is a header like any other, so a
    canonicalizer that copies its *text* into the base proves only that the
    sender typed it. The bytes have to be hashed.
    """
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
    """A policy reading `signature_verified` must be able to ask what was signed."""
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


# --- the nonce ledger is a bounded resource ---------------------------------


async def test_an_unverifiable_signature_does_not_fill_the_nonce_ledger():
    """`saml.py:1213-1219` states the rule this module has to follow.

    A ledger is reachable by anyone who can name a `keyid`, and a `keyid` is
    published in the operator's directory. Claiming before the signature
    verifies makes the replay defence a denial-of-service primitive against the
    agents it protects.
    """
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
    """The sharper form: pre-spending the identifier a real agent is about to use."""
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
    """The digest is hashed chunk by chunk, so streaming is not a way around it.

    A handler that never materialises the body would otherwise read forged bytes
    with a verified signature beside them -- the same defect, reachable by
    choosing the other reader.
    """
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
