"""A place to cancel a token before it expires (report 23: B-13)."""

from __future__ import annotations

import base64
import hmac
import json
from hashlib import sha256

from wreath._auth.jwt import SymmetricKey, default_identity, verify_jwt

_SECRET = b"k" * 32


def _token(claims: dict) -> str:
    def segment(payload: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).rstrip(b"=").decode("ascii")

    signing_input = f"{segment({'alg': 'HS256'})}.{segment(claims)}".encode("ascii")
    signature = hmac.new(_SECRET, signing_input, sha256).digest()
    tail = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{signing_input.decode('ascii')}.{tail}"


def _verify(token: str, **kwargs):
    import time

    return verify_jwt(
        token,
        key_resolver=lambda header: SymmetricKey(_SECRET),
        algorithms=frozenset({"HS256"}),
        issuer=None,
        audiences=(),
        leeway=60,
        required=(),
        identity=default_identity,
        now=int(time.time()),
        **kwargs,
    )


class TestRevocationHook:
    """B-13: a stolen token is valid until `exp` and there is nowhere to say
    otherwise -- no `jti` cache, no hook, no seam at all."""

    def test_a_revoked_token_does_not_verify(self):
        token = _token({"sub": "u1", "jti": "abc"})
        assert _verify(token) is not None            # valid without the hook
        assert _verify(token, revoked=lambda claims: claims.get("jti") == "abc") is None

    def test_an_unrevoked_token_still_verifies(self):
        token = _token({"sub": "u1", "jti": "def"})
        identity = _verify(token, revoked=lambda claims: claims.get("jti") == "abc")
        assert identity is not None and identity.id == "u1"

    def test_the_hook_sees_the_whole_claim_set(self):
        seen: list[dict] = []
        token = _token({"sub": "u1", "jti": "abc", "sid": "session-9"})

        def revoked(claims):
            seen.append(dict(claims))
            return False

        assert _verify(token, revoked=revoked) is not None
        assert seen and seen[0]["sid"] == "session-9"

    def test_a_hook_that_raises_denies(self):
        """A revocation store that is down must not admit the token it was
        asked about -- that is the one direction this check cannot fail in."""
        token = _token({"sub": "u1", "jti": "abc"})

        def revoked(claims):
            raise RuntimeError("the revocation store is unreachable")

        assert _verify(token, revoked=revoked) is None

    def test_no_hook_is_the_default(self):
        token = _token({"sub": "u1"})
        assert _verify(token) is not None

    def test_the_verifier_class_passes_it_through(self):
        from wreath._auth.jwt import JwtVerifier

        verifier = JwtVerifier(
            algorithms=["HS256"],
            key=SymmetricKey(_SECRET),
            required=(),
            revoked=lambda claims: claims.get("jti") == "abc",
        )
        assert verifier(_token({"sub": "u1", "jti": "abc"})) is None
        assert verifier(_token({"sub": "u1", "jti": "ok"})) is not None


class TestRateLimitTierDocumented:
    """G-65: per-tier stores reset the allowance on a role change. Kept -- an
    upgrade should take effect at once -- but the consequence has to be written
    down where somebody choosing self-service roles will read it."""

    def test_the_consequence_is_stated(self):
        import inspect

        from wreath.policy.ratelimit import TieredRateLimitPolicy

        doc = inspect.getdoc(TieredRateLimitPolicy) or ""
        lowered = doc.lower()
        assert "self-service" in lowered or "toggl" in lowered
        assert "fresh" in lowered or "reset" in lowered
