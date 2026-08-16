"""Issuing tokens, and the replay defences that make it safe.

Migrated from `tests/thesis/test_sso_contract.py`. The load-bearing test is
`test_a_minted_token_is_accepted_by_wreaths_own_verifier`: an issuer whose
output its own verifier rejects is two features rather than one.
"""

from __future__ import annotations

import pytest

from wreath.auth import JwtVerifier, SymmetricKey
from wreath.oauth import AuthorizationServer, ClientRegistration, OAuthRefusal

CLIENT = ClientRegistration(
    client_id="console",
    redirect_uris=("https://app.example/cb",),
    scopes=("read", "write"),
    confidential=True,
)
PUBLIC = ClientRegistration(
    client_id="spa", redirect_uris=("https://app.example/spa",), scopes=("read",))


@pytest.fixture
def server() -> AuthorizationServer:
    return AuthorizationServer(
        issuer="https://app.example", secret=b"a" * 32, clients=(CLIENT, PUBLIC))


# --- discovery --------------------------------------------------------------


def test_metadata_names_the_issuer_and_its_endpoints(server) -> None:
    metadata = server.metadata()
    assert metadata["issuer"] == "https://app.example"
    assert metadata["token_endpoint"].endswith("/oauth/token")
    assert metadata["code_challenge_methods_supported"] == ["S256"]


def test_the_jwks_is_empty_for_an_hmac_signer_and_that_is_a_fact(server) -> None:
    """**Not an omission.**

    HS256 has no public half. Publishing the shared secret as an `oct` entry
    would hand every reader the ability to mint tokens, so the honest answer is
    an empty key set -- and a deployment whose tokens third parties verify
    passes an asymmetric `signer=`.
    """
    assert server.jwks() == {"keys": []}


# --- the authorization endpoint ---------------------------------------------


def test_a_redirect_uri_is_matched_exactly_and_never_by_prefix(server) -> None:
    """`https://app.example.evil/cb` has the registered URI as a prefix.

    An open redirect on the authorization endpoint is an authorization-code
    exfiltration, so the match is exact.
    """
    with pytest.raises(OAuthRefusal, match="matched exactly") as raised:
        server.authorize(client_id="console", redirect_uri="https://app.example.evil/cb")
    assert raised.value.reason == "redirect_uri-mismatch"


def test_the_registered_redirect_uri_is_accepted(server) -> None:
    assert server.authorize(
        client_id="console", redirect_uri="https://app.example/cb").client_id == "console"


def test_the_plain_pkce_method_is_refused_at_the_authorization_endpoint(server) -> None:
    with pytest.raises(OAuthRefusal, match="protects nothing") as raised:
        server.authorize(client_id="console", redirect_uri="https://app.example/cb",
                         challenge_method="plain")
    assert raised.value.reason == "weak-pkce"


def test_an_unknown_client_is_refused(server) -> None:
    with pytest.raises(OAuthRefusal, match="no client registered"):
        server.authorize(client_id="ghost", redirect_uri="https://app.example/cb")


def test_a_scope_the_client_was_not_registered_for_is_refused(server) -> None:
    """A client cannot ask for more than registration granted it."""
    _, challenge = _pkce()
    with pytest.raises(OAuthRefusal, match="not registered for scope") as raised:
        server.issue_code(
            client_id="console", subject="u", scope=("admin",), challenge=challenge,
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "invalid-scope"


# --- the code, once ---------------------------------------------------------


def test_a_code_can_be_redeemed_once(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    token = server.redeem(
        code, verifier=verifier, client_id="console",
        redirect_uri="https://app.example/cb",
    )
    assert token.subject == "u"
    assert token.scope == ("read",)


def test_redeeming_a_code_twice_revokes_the_token_the_first_redemption_issued(
    server,
) -> None:
    """**The only safe answer to "two parties hold this code".**

    Refusing the second redemption alone leaves the attacker's token live if
    they got there first, so neither party keeps anything.
    """
    verifier, _ = _pkce()
    code = _issued(server)
    redemption = {
        "verifier": verifier,
        "client_id": "console",
        "redirect_uri": "https://app.example/cb",
    }
    first = server.redeem(code, **redemption)
    assert not server.is_revoked(first.access_token)
    with pytest.raises(OAuthRefusal, match="has been revoked") as raised:
        server.redeem(code, **redemption)
    assert raised.value.reason == "code-replayed"
    assert server.is_revoked(first.access_token)


def test_a_code_redeemed_with_the_wrong_pkce_verifier_is_refused(server) -> None:
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="does not match the challenge") as raised:
        server.redeem(
            code, verifier="a-different-one", client_id="console",
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "pkce-mismatch"


def test_the_matching_verifier_is_accepted(server) -> None:
    """So the refusal above is not passing for free."""
    verifier, _ = _pkce()
    code = _issued(server)
    assert server.redeem(
        code, verifier=verifier, client_id="console",
        redirect_uri="https://app.example/cb",
    ).subject == "u"


# --- refresh ----------------------------------------------------------------


def test_a_refresh_token_rotates(server) -> None:
    first = server.issue_refresh(subject="u")
    rotated = server.rotate(first)
    assert rotated.refresh_token
    assert rotated.refresh_token != first.token


def test_presenting_a_rotated_refresh_token_again_is_refused(server) -> None:
    """Rotation without reuse detection is rotation that tells you nothing."""
    first = server.issue_refresh(subject="u")
    server.rotate(first)
    with pytest.raises(OAuthRefusal, match="already been rotated") as raised:
        server.rotate(first)
    assert raised.value.reason == "refresh-reused"


def test_revoking_the_chain_revokes_every_access_token_it_issued(server) -> None:
    """One compromised refresh token means every token descended from it."""
    first = server.issue_refresh(subject="u")
    second = server.rotate(first)
    assert server.revoke_chain(first.chain) >= 1
    assert server.is_revoked(second.access_token)


# --- client credentials -----------------------------------------------------


def test_a_client_credentials_grant_cannot_carry_a_subject(server) -> None:
    """A machine token naming a person is a machine that can act as one, and
    every audit trail downstream then attributes its writes to them."""
    with pytest.raises(OAuthRefusal, match="no resource owner") as raised:
        server.client_credentials(client_id="console", subject="u")
    assert raised.value.reason == "subject-on-client-credentials"


def test_a_public_client_cannot_use_the_client_credentials_grant(server) -> None:
    """A browser or mobile app cannot keep a secret, so it has no credentials
    to present -- and a grant it can complete anyway is one anybody can."""
    with pytest.raises(OAuthRefusal, match="cannot keep a secret") as raised:
        server.client_credentials(client_id="spa")
    assert raised.value.reason == "public-client"


def test_a_confidential_client_gets_a_subjectless_token(server) -> None:
    token = server.client_credentials(client_id="console")
    assert token.subject is None


# --- tenancy ----------------------------------------------------------------


def test_a_token_carries_the_tenant_it_was_minted_in(server) -> None:
    """It composes with `wreath.tenancy` rather than around it: a token minted
    inside one tenant must not read another's data, whatever roles it holds."""
    verifier, _ = _pkce()
    code = _issued(server, tenant="acme")
    assert server.redeem(
        code, verifier=verifier, client_id="console",
        redirect_uri="https://app.example/cb",
    ).tenant == "acme"


# --- the half that makes it one feature rather than two ---------------------


def test_a_minted_token_is_accepted_by_wreaths_own_verifier(server) -> None:
    """**An issuer whose output its own verifier rejects is two features.**

    Driven through the real `JwtVerifier` -- the same class `BearerTokenBackend`
    and `MCPAuth` use -- rather than through a decoder written for this test,
    because a decoder written here would agree with the encoder written here.
    """
    token = server.issue_access(
        subject="u-9", audience="https://api.example", scope=("read",))
    verifier = JwtVerifier(
        algorithms=["HS256"],
        key=SymmetricKey(secret=server.secret),
        issuer="https://app.example",
        audience="https://api.example",
    )
    identity = verifier(token.access_token)
    # `Identity.id`, not `.subject`: wreath's identity carries `id`/`type`, and
    # the `sub` claim is what `default_identity` maps onto it.
    assert identity.id == "u-9"


def test_a_token_minted_for_another_audience_is_refused_by_that_verifier(
    server,
) -> None:
    """The audience binding `MCPAuth` already relies on, checked from this end.

    A token this server minted for one resource must not be accepted by another,
    or the issuer has undone the protection the verifier provides.
    """
    token = server.issue_access(subject="u", audience="https://other.example")
    verifier = JwtVerifier(
        algorithms=["HS256"], key=SymmetricKey(secret=server.secret),
        issuer="https://app.example", audience="https://api.example")
    # `None`, not an exception: `JwtVerifier` is a `Verifier` callable and a
    # token that does not verify is "nobody", which is what
    # `BearerTokenBackend` turns into a 401.
    assert verifier(token.access_token) is None


def test_a_token_signed_with_another_secret_is_refused(server) -> None:
    """The signature is load-bearing, so it is asserted rather than assumed."""
    token = server.issue_access(subject="u", audience="https://api.example")
    verifier = JwtVerifier(
        algorithms=["HS256"], key=SymmetricKey(secret=b"b" * 32),
        issuer="https://app.example", audience="https://api.example")
    assert verifier(token.access_token) is None


# --- the asymmetric signer --------------------------------------------------
#
# Added after the module docstring claimed asymmetric signing would need a
# `cryptography` dependency. It does not: `wreath._webpush` already signs ES256
# with a hedged nonce over the standard library, and `JwtVerifier` already
# verifies ES256. Both halves were in the tree and the claim was wrong.


def _es256_server() -> tuple[AuthorizationServer, object]:
    from wreath.oauth import Es256Signer

    signer = Es256Signer.generate()
    return AuthorizationServer(
        issuer="https://app.example", clients=(CLIENT,), signer=signer), signer


def test_the_es256_signer_publishes_a_real_key_set() -> None:
    """The half HS256 cannot have: something safe to put on the JWKS endpoint."""
    server, signer = _es256_server()
    keys = server.jwks()["keys"]
    assert len(keys) == 1
    assert keys[0]["kty"] == "EC" and keys[0]["crv"] == "P-256"
    assert keys[0]["kid"] == signer.kid


def test_the_published_key_set_cannot_contain_the_private_half() -> None:
    """**The difference from the HMAC case, asserted rather than assumed.**

    An `oct` entry's `k` *is* the signing secret, which is why HS256 publishes
    nothing. An EC public entry has no field the private scalar could occupy, so
    the dangerous value is unspellable here rather than merely omitted.
    """
    server, signer = _es256_server()
    entry = server.jwks()["keys"][0]
    assert "d" not in entry and "k" not in entry
    assert signer.private_bytes.hex() not in str(entry)


def test_a_token_signed_with_es256_verifies_against_the_published_key() -> None:
    """End to end through the real verifier, with the key taken from the key set
    this server publishes rather than rebuilt from the private scalar -- so the
    two cannot drift into disagreeing."""
    server, signer = _es256_server()
    token = server.issue_access(subject="u-9", audience="https://api.example")
    verifier = JwtVerifier(
        algorithms=["ES256"], key=signer.verifying_key(),
        issuer="https://app.example", audience="https://api.example")
    assert verifier(token.access_token).id == "u-9"


def test_an_es256_token_names_its_key_id_so_rotation_is_possible() -> None:
    """Without a `kid` a verifier holding two keys during a rotation has to try
    both, and cannot tell a wrong key from a bad signature."""
    import json
    from base64 import urlsafe_b64decode

    server, signer = _es256_server()
    token = server.issue_access(subject="u", audience="a")
    raw = token.access_token.split(".")[0]
    header = json.loads(urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert header["alg"] == "ES256"
    assert header["kid"] == signer.kid


def test_a_token_signed_by_another_es256_key_is_refused() -> None:
    """The signature is load-bearing, so it is falsified rather than trusted."""
    from wreath.oauth import Es256Signer

    server, _ = _es256_server()
    token = server.issue_access(subject="u", audience="https://api.example")
    verifier = JwtVerifier(
        algorithms=["ES256"], key=Es256Signer.generate().verifying_key(),
        issuer="https://app.example", audience="https://api.example")
    assert verifier(token.access_token) is None


def test_a_signer_round_trips_through_its_private_bytes() -> None:
    """The scalar is what a deployment stores in configuration, so restoring
    from it has to produce the same public key -- otherwise a restart silently
    invalidates every token in flight."""
    from wreath.oauth import Es256Signer

    signer = Es256Signer.generate()
    restored = Es256Signer.from_bytes(signer.private_bytes, kid=signer.kid)
    assert restored.public_jwks() == signer.public_jwks()


def test_signing_cost_is_counted_so_the_ceiling_can_be_watched() -> None:
    """**The footgun made observable rather than warned about.**

    ES256 signing is CPU-bound pure Python, so it holds the loop while it runs.
    Measured, the median request is unaffected at every rate and the request
    *behind* a signature waits the full ~3 ms -- a tail problem, not a
    throughput one, until roughly 330 a second saturates a core.

    A threshold baked into wreath would be one guess applied to every
    deployment's latency budget, so instead the cost is a counter
    `wreath.metrics.collect` picks up by asking, and the number to alert on is
    `signing_nanoseconds` over wall time: the fraction of a core this is spending.
    """
    server, _ = _es256_server()
    for _ in range(5):
        server.issue_access(subject="u", audience="a")
    counters = server.counters()
    assert counters.values["tokens_issued"] == 5
    assert counters.values["signing_nanoseconds"] > 0


def test_the_hmac_path_counts_tokens_but_not_signing_time(server) -> None:
    """Measuring a ~12 us HMAC would cost a meaningful fraction of what it
    measures -- the trap `AGENTS.md` names about cProfile, one order down."""
    server.issue_access(subject="u", audience="a")
    counters = server.counters()
    assert counters.subsystem == "oauth"
    assert counters.values == {"tokens_issued": 1, "signing_nanoseconds": 0}


# --- the code endpoint's preconditions --------------------------------------
#
# Each of these is a check RFC 6749 §4.1.3 requires of the token endpoint and
# that `redeem` did not make. They are grouped because they share one shape: a
# field the code carries, stored and then never read.


def _pkce() -> tuple[str, str]:
    """A verifier and its S256 challenge."""
    import hashlib
    from base64 import urlsafe_b64encode

    verifier = "the-real-one-and-it-is-long-enough-to-be-worth-something"
    challenge = urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _issued(server: AuthorizationServer, **overrides) -> str:
    verifier, challenge = _pkce()
    options = {
        "client_id": "console",
        "subject": "u",
        "scope": ("read",),
        "challenge": challenge,
        "redirect_uri": "https://app.example/cb",
    }
    options.update(overrides)
    return server.issue_code(**options)


def test_a_code_cannot_be_minted_without_a_pkce_challenge(server) -> None:
    """The module docstring says PKCE is required; the default made it optional.

    `challenge=""` skipped the comparison in `redeem` entirely, so a deployment
    that simply did not pass one got a PKCE-free authorization-code flow that
    the documentation says cannot exist.
    """
    with pytest.raises(OAuthRefusal, match="code_challenge") as raised:
        server.issue_code(
            client_id="console", subject="u", challenge="",
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "weak-pkce"


def test_a_challenge_that_is_not_an_s256_digest_is_refused(server) -> None:
    """A `plain` challenge is 43 characters of *verifier*, and looks like one."""
    with pytest.raises(OAuthRefusal, match="code_challenge") as raised:
        server.issue_code(
            client_id="console", subject="u", challenge="short",
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "weak-pkce"


def test_redeeming_without_a_verifier_is_refused(server) -> None:
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="does not match the challenge") as raised:
        server.redeem(
            code, verifier="", client_id="console",
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "pkce-mismatch"


def test_a_code_is_redeemable_only_by_the_client_it_was_issued_to(server) -> None:
    """Otherwise a leaked code is redeemable by any registered client."""
    verifier, _ = _pkce()
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="issued to a different client") as raised:
        server.redeem(
            code, verifier=verifier, client_id="spa",
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "client-mismatch"


def test_a_code_is_redeemable_only_against_the_redirect_uri_it_was_issued_for(
    server,
) -> None:
    """RFC 6749 §4.1.3. The field was stored and read nowhere."""
    verifier, _ = _pkce()
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="redirect_uri") as raised:
        server.redeem(
            code, verifier=verifier, client_id="console",
            redirect_uri="https://app.example/other",
        )
    assert raised.value.reason == "redirect_uri-mismatch"


def test_an_authorization_code_expires(server) -> None:
    """`issued_at` was stored and never read, so a leaked code never went stale."""
    verifier, _ = _pkce()
    code = _issued(server, now=1000.0)
    with pytest.raises(OAuthRefusal, match="expired") as raised:
        server.redeem(
            code, verifier=verifier, client_id="console",
            redirect_uri="https://app.example/cb", now=1000.0 + 61.0,
        )
    assert raised.value.reason == "code-expired"


def test_a_code_inside_its_window_still_redeems(server) -> None:
    """So the expiry above is not passing for free."""
    verifier, _ = _pkce()
    code = _issued(server, now=1000.0)
    token = server.redeem(
        code, verifier=verifier, client_id="console",
        redirect_uri="https://app.example/cb", now=1000.0 + 30.0,
    )
    assert token.subject == "u"


# --- refresh reuse actually revokes -----------------------------------------


def test_refresh_reuse_revokes_the_chain_it_says_it_revokes(server) -> None:
    """The refusal message claims the chain is dead. It has to be dead.

    Detection that fires and does nothing is worse than no detection: the
    operator reads "every token in its chain has been revoked" and believes the
    incident is contained while the thief keeps rotating.
    """
    first = server.issue_refresh(subject="u")
    stolen = server.rotate(first)
    further = server.rotate(stolen.refresh_token)

    with pytest.raises(OAuthRefusal, match="already been rotated"):
        server.rotate(first)

    assert server.is_revoked(stolen.access_token)
    assert server.is_revoked(further.access_token)
    with pytest.raises(OAuthRefusal, match="already been rotated"):
        server.rotate(further.refresh_token)


def test_revoking_a_chain_covers_the_access_token_minted_beside_its_first_refresh(
    server,
) -> None:
    """`issue_refresh` seeded an empty chain, so the token issued alongside it
    was never in the chain and survived the revocation."""
    verifier, _ = _pkce()
    code = _issued(server)
    token = server.redeem(
        code, verifier=verifier, client_id="console",
        redirect_uri="https://app.example/cb",
    )
    assert token.refresh_token
    with pytest.raises(OAuthRefusal, match="already been rotated"):
        server.rotate(token.refresh_token)
        server.rotate(token.refresh_token)
    assert server.is_revoked(token.access_token)
