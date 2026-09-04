from __future__ import annotations

import asyncio
import time

import pytest


class TestPasswordHashingIsOffTheLoop:
    """R-70: scrypt (N=2^14, ~16 MB) runs synchronously inside the async flows,
    so every login stalls the whole worker for the duration."""

    async def test_registering_does_not_block_the_event_loop(self):
        from wreath._userkit import (
            CapturingEmailSender,
            InMemoryUserStore,
            register,
        )

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        beat = asyncio.create_task(heartbeat())
        try:
            await register(
                InMemoryUserStore(),
                CapturingEmailSender(),
                secret="s" * 32,
                email="a@example.com",
                password="hunter2hunter2",
                link_builder=lambda purpose, token: f"/{purpose}/{token}",
            )
        finally:
            beat.cancel()
        # A scrypt hash takes tens of milliseconds; if it held the loop, the
        # heartbeat could not have run.
        assert ticks > 1, "the event loop was blocked for the whole hash"

    async def test_authenticating_does_not_block_the_event_loop(self):
        from wreath._userkit import InMemoryUserStore, authenticate, hash_password

        store = InMemoryUserStore()
        await store.create(
            "a@example.com", await asyncio.to_thread(hash_password, "hunter2hunter2")
        )

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        beat = asyncio.create_task(heartbeat())
        try:
            user = await authenticate(store, "a@example.com", "hunter2hunter2")
        finally:
            beat.cancel()
        assert user is not None
        assert ticks > 1, "the event loop was blocked for the whole verify"


class TestPasswordLengthIsBounded:
    """R-71: no maximum length before scrypt, so a multi-megabyte password is a
    CPU amplifier an anonymous caller controls."""

    def test_an_enormous_password_is_refused(self):
        from wreath._userkit import hash_password

        with pytest.raises(ValueError):
            hash_password("x" * 100_000)

    def test_verification_refuses_it_without_spending_the_cpu(self):
        from wreath._userkit import hash_password, verify_password

        encoded = hash_password("hunter2hunter2")
        started = time.perf_counter()
        assert verify_password("x" * 100_000, encoded) is False
        assert time.perf_counter() - started < 0.05

    def test_an_ordinary_password_still_works(self):
        from wreath._userkit import hash_password, verify_password

        encoded = hash_password("hunter2hunter2")
        assert verify_password("hunter2hunter2", encoded) is True
        assert verify_password("wrong", encoded) is False


class TestRegistrationTiming:
    """R-72: `register` returns a uniform response but does wildly different
    work for a known and an unknown address, so latency answers the question the
    response refuses to."""

    async def test_a_duplicate_registration_costs_what_a_new_one_costs(self):
        from wreath._userkit import (
            CapturingEmailSender,
            InMemoryUserStore,
            register,
        )

        store = InMemoryUserStore()
        mail = CapturingEmailSender()
        kwargs = dict(
            secret="s" * 32,
            password="hunter2hunter2",
            link_builder=lambda purpose, token: f"/{purpose}/{token}",
        )

        started = time.perf_counter()
        await register(store, mail, email="new@example.com", **kwargs)
        fresh = time.perf_counter() - started

        started = time.perf_counter()
        await register(store, mail, email="new@example.com", **kwargs)
        duplicate = time.perf_counter() - started

        assert len(mail.verifications) == 1, "a duplicate must not send a second mail"
        # The hash dominates both paths, so the duplicate cannot be an order of
        # magnitude cheaper.
        assert duplicate > fresh / 4, (fresh, duplicate)


class TestResetPasswordEdges:
    """G-74: `reset_password("")` raises out of `hash_password` instead of
    reporting failure, so a reachable input becomes a 500."""

    async def test_an_empty_new_password_is_refused_not_raised(self):
        from wreath._userkit import (
            InMemoryUserStore,
            hash_password,
            reset_password,
            sign_token,
        )
        from wreath._userkit import fingerprint as _fingerprint

        store = InMemoryUserStore()
        user = await store.create("a@example.com", hash_password("hunter2hunter2"))
        token = sign_token(
            "s" * 32,
            "reset",
            user.id,
            ttl=3600,
            bound=_fingerprint(user.hashed_password),
        )
        assert await reset_password(store, secret="s" * 32, token=token, new_password="") is False


class TestJwtHostileTokens:
    """R-59: `peek_header` decodes an unbounded first segment. R-60: deep JSON
    raises RecursionError, which `verify_jwt` does not catch, so a crafted token
    is a 500 rather than a 401."""

    def test_an_enormous_header_segment_is_refused(self):
        import base64

        from wreath._auth.jwt import peek_header

        blob = base64.urlsafe_b64encode(b"{" + b'"a":1,' * 2_000_000 + b"}").rstrip(b"=")
        assert peek_header(blob.decode("ascii") + ".x.y") is None

    def test_a_deeply_nested_token_is_a_refusal_not_a_crash(self):
        import base64
        import json

        from wreath._auth.jwt import default_identity, verify_jwt

        def segment(payload):
            return (
                base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode("ascii")
            )

        nested: object = "x"
        for _ in range(2000):
            nested = [nested]
        token = f"{segment({'alg': 'HS256'})}.{segment({'sub': 'a', 'deep': nested})}.AAAA"
        assert (
            verify_jwt(
                token,
                key_resolver=lambda header: None,
                algorithms=frozenset({"HS256"}),
                issuer=None,
                audiences=(),
                leeway=60,
                required=(),
                identity=default_identity,
            )
            is None
        )


class TestJwksKeyQuality:
    """R-61: no minimum RSA modulus, so a 512-bit key from a JWKS verifies.
    R-65: an EC JWK's point is never checked to be on P-256."""

    def test_a_short_rsa_modulus_is_refused(self):
        import base64

        from wreath._auth.jwt import JwtError, key_from_jwk

        def b64(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        with pytest.raises(JwtError, match="bits|small|modulus"):
            key_from_jwk({"kty": "RSA", "n": b64((1 << 512) - 1), "e": b64(65537)})

    def test_a_point_off_the_curve_is_refused(self):
        import base64

        from wreath._auth.jwt import JwtError, key_from_jwk

        def b64(value: int) -> str:
            return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode("ascii")

        with pytest.raises(JwtError, match="curve"):
            key_from_jwk({"kty": "EC", "crv": "P-256", "x": b64(1), "y": b64(1)})


class TestSsoSession:
    """R-68: the login callback never rotates the session, so a fixed id
    survives authentication. G-73: the session principal carries roles and not
    permissions, so `@authorize(permissions=...)` always refuses an SSO caller
    that a bearer token would admit."""

    def test_the_callback_rotates_the_session(self):
        import inspect

        from wreath._auth import oauth2

        source = inspect.getsource(oauth2.register_oauth2_login)
        assert "rotate_session" in source

    def test_the_callback_binds_the_id_token_to_the_client_id(self):
        import inspect

        from wreath._auth import oauth2

        source = inspect.getsource(oauth2.register_oauth2_login)
        assert "bearer_verifier(audience=client_id)" in source

    async def test_both_login_routes_refuse_an_app_with_no_session_middleware(self):
        from wreath import Wreath
        from wreath._auth.oauth2 import register_oauth2_login
        from wreath._auth.oidc import OidcProvider
        from wreath.testing import TestClient

        provider = OidcProvider(
            "idp",
            issuer="https://idp.example",
            audience="client",
            http_client=object(),
        )
        # Discovered, so `provider_not_discovered` cannot answer for either route.
        provider.authorization_endpoint = "https://idp.example/authorize"
        provider.token_endpoint = "https://idp.example/token"

        app = Wreath()
        register_oauth2_login(
            app,
            "idp",
            provider=provider,
            client_id="client",
            client_secret="secret",
            redirect_uri="https://app.example/auth/callback",
        )
        async with TestClient(app) as client:
            for path in ("/auth/login", "/auth/callback?code=c&state=s"):
                response = await client.get(path)
                assert response.status == 500, path
                assert response.json() == {"error": "session_middleware_required"}

    async def test_the_callback_refuses_a_state_the_session_never_issued(self):
        from wreath import Wreath
        from wreath._auth.oauth2 import register_oauth2_login
        from wreath._auth.oidc import OidcProvider
        from wreath.policy import HttpPolicy, SessionPolicy
        from wreath.testing import TestClient

        provider = OidcProvider(
            "idp",
            issuer="https://idp.example",
            audience="client",
            http_client=object(),
        )
        provider.authorization_endpoint = "https://idp.example/authorize"
        provider.token_endpoint = "https://idp.example/token"

        app = Wreath()
        app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
        register_oauth2_login(
            app,
            "idp",
            provider=provider,
            client_id="client",
            client_secret="secret",
            redirect_uri="https://app.example/auth/callback",
        )
        async with TestClient(app) as client:
            response = await client.get("/auth/callback?code=c&state=forged")
            assert response.status == 400
            assert response.json() == {"error": "invalid_state"}
            # And the login route reaches its redirect rather than the refusal.
            started = await client.get("/auth/login")
            assert started.status == 302
            assert started.header("location").startswith("https://idp.example/authorize?")

    @pytest.mark.parametrize(
        ("query", "session", "expected"),
        [
            (
                "state=issued",
                {"_oidc_state_idp": "issued", "_oidc_verifier_idp": "v"},
                (400, {"error": "invalid_state"}, 0),
            ),
            (
                "code=c",
                {"_oidc_verifier_idp": "v"},
                (400, {"error": "invalid_state"}, 0),
            ),
            (
                "code=c&state=issued",
                {"_oidc_state_idp": "issued"},
                (400, {"error": "invalid_state"}, 0),
            ),
            (
                "code=c&state=wrong",
                {"_oidc_state_idp": "issued", "_oidc_verifier_idp": "v"},
                (400, {"error": "invalid_state"}, 0),
            ),
            (
                "code=c&state=issued",
                {
                    "_oidc_state_idp": "issued",
                    "_oidc_verifier_idp": "v",
                    "_oidc_nonce_idp": "nonce",
                },
                (502, {"error": "token_exchange_failed"}, 1),
            ),
        ],
        ids=("missing-code", "missing-state", "missing-verifier", "wrong-state", "valid"),
    )
    async def test_the_callback_requires_each_state_binding(
        self,
        query: str,
        session: dict[str, str],
        expected: tuple[int, dict[str, str], int],
    ):
        from wreath import JSONResponse, Wreath
        from wreath._auth.oauth2 import register_oauth2_login
        from wreath._auth.oidc import OidcProvider
        from wreath.policy import HttpPolicy, SessionPolicy
        from wreath.testing import TestClient

        class _Response:
            status = 500
            body = b""

        class _TokenClient:
            calls = 0

            async def post(self, *args, **kwargs):
                self.calls += 1
                return _Response()

        token_client = _TokenClient()
        provider = OidcProvider(
            "idp",
            issuer="https://idp.example",
            audience="client",
            http_client=token_client,
        )
        provider.authorization_endpoint = "https://idp.example/authorize"
        provider.token_endpoint = "https://idp.example/token"

        app = Wreath()
        app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))

        @app.get("/seed")
        async def seed(request):
            request.state.session.update(session)
            return JSONResponse({})

        register_oauth2_login(
            app,
            "idp",
            provider=provider,
            client_id="client",
            client_secret="secret",
            redirect_uri="https://app.example/auth/callback",
        )
        async with TestClient(app) as client:
            seeded = await client.get("/seed")
            cookie = seeded.header("set-cookie")
            assert seeded.status == 200 and cookie is not None
            response = await client.get(
                f"/auth/callback?{query}", headers={"cookie": cookie.split(";", 1)[0]}
            )

        assert (response.status, response.json(), token_client.calls) == expected

    async def test_the_session_backend_carries_permissions(self):
        from wreath._auth.session_backend import SessionIdentityBackend

        class _State:
            session = {
                "principal": {
                    "sub": "u1",
                    "roles": ["editor"],
                    "permissions": ["posts:write"],
                }
            }

        class _Request:
            state = _State()

        identity = await SessionIdentityBackend().authenticate(_Request())
        assert identity is not None
        assert identity.roles == frozenset({"editor"})
        assert identity.permissions == frozenset({"posts:write"})
