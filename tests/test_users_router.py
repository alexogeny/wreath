from __future__ import annotations

from typing import Any

import pytest

from wreath.users import InMemoryUserStore, OrmUserStore, default_user_model, user_router


class _Revocations:
    async def delete_for(self, _subject: str) -> int:
        return 0


_REVOCATIONS = _Revocations()


def test_request_model_reprs_do_not_expose_credentials() -> None:
    from wreath.users import CodeInput, LoginInput, RegisterInput, ResetInput, TokenInput

    secret = "request-credential-secret"
    values = (
        RegisterInput("user@example.com", secret),
        LoginInput("user@example.com", secret),
        TokenInput(secret),
        ResetInput(secret, secret),
        CodeInput(secret),
    )

    assert all(secret not in repr(value) for value in values)


def test_user_record_repr_does_not_expose_password_hash() -> None:
    from wreath.users import UserRecord

    password_hash = "scrypt-password-hash-secret"
    user = UserRecord("user-1", "user@example.com", password_hash)

    assert password_hash not in repr(user)


def _routes(router):
    return {(r.path, m) for r in router.routes for m in r.methods}


def test_user_router_exposes_lifecycle_routes():
    router = user_router(
        InMemoryUserStore(), sessions=_REVOCATIONS, secret="s" * 32, base_url="https://app"
    )
    routes = _routes(router)
    assert ("/users/register", "POST") in routes
    assert ("/users/login", "POST") in routes
    assert ("/users/logout", "POST") in routes
    assert ("/users/verify", "POST") in routes
    assert ("/users/verify/{token}", "GET") in routes
    assert ("/users/forgot-password", "POST") in routes
    assert ("/users/reset-password", "POST") in routes
    assert ("/users/me", "GET") in routes
    assert ("users",) == router.routes[0].tags


def test_user_router_requires_secret():
    with pytest.raises(ValueError, match="at least 32 bytes"):
        user_router(InMemoryUserStore(), secret="")


def test_user_router_refuses_a_short_action_token_secret() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        user_router(InMemoryUserStore(), secret="short")


def test_custom_prefix():
    router = user_router(
        InMemoryUserStore(), sessions=_REVOCATIONS, secret="s" * 32, prefix="/accounts"
    )
    assert ("/accounts/register", "POST") in _routes(router)


async def test_ormuserstore_write_path_uses_unit_of_work():
    Model = default_user_model()
    inst = Model(email="seed@x.co", hashed_password="h")
    assert inst.id is not None  # uuid default applies on instantiation

    class FakeSession:
        def __init__(self):
            self.added, self.flushes, self._row = [], 0, inst

        def add(self, i):
            self.added.append(i)

        async def flush(self):
            self.flushes += 1

        async def get(self, model, pk):
            return self._row

    s = FakeSession()
    store = OrmUserStore(s, Model)
    rec = await store.create("A@B.co", "hash")
    assert len(s.added) == 1 and s.flushes == 1 and rec.email == "a@b.co"

    from wreath.users import UserRecord

    await store.update(UserRecord(str(inst.id), "new@b.co", "h2", True, True))
    assert s.flushes == 2 and inst.email == "new@b.co"


async def test_ormuserstore_batch_lookup_uses_one_query_and_restores_input_order():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Bool, Varchar

    class TextUser(Model, table="batch_lookup_users"):
        id: Mapped[str] = column(Varchar, primary_key=True)
        email: Mapped[str] = column(Varchar)
        hashed_password: Mapped[str] = column(Varchar)
        is_active: Mapped[bool] = column(Bool, default=True)
        is_verified: Mapped[bool] = column(Bool, default=False)

    first = TextUser(id="first", email="first@example.com", hashed_password="h1")
    second = TextUser(id="second", email="second@example.com", hashed_password="h2")

    class FakeSession:
        def __init__(self):
            self.queries = []

        async def fetch(self, query):
            self.queries.append(query)
            return [second, first]

    session = FakeSession()
    store = OrmUserStore(session, TextUser)
    found = await store.get_many_by_id((first.id, "missing", second.id, first.id))

    assert [record.email if record is not None else None for record in found] == [
        "first@example.com",
        None,
        "second@example.com",
        "first@example.com",
    ]
    assert len(session.queries) == 1

    assert await store.get_many_by_id(()) == []
    assert len(session.queries) == 1


# A sweep over `src/wreath/users.py` reported these as `unreached`: no test executed
# them at all. They are the same shape as the `orm/types.py` findings -- the behaviour
# was covered and the *validation* was not -- with one that is a live branch rather
# than a refusal.


@pytest.mark.parametrize("max_attempts", [0, -1, -100])
def test_login_limiter_refuses_a_budget_of_no_attempts(max_attempts: int) -> None:
    from wreath.users import LoginLimiter

    with pytest.raises(ValueError, match="max_attempts must be at least 1"):
        LoginLimiter(max_attempts=max_attempts, window=60.0)


@pytest.mark.parametrize("max_attempts", [True, 1.5, float("nan"), float("inf")])
def test_login_limiter_refuses_a_non_integer_attempt_budget(max_attempts: Any) -> None:
    from wreath.users import LoginLimiter

    with pytest.raises(ValueError, match="positive integer"):
        LoginLimiter(max_attempts=max_attempts, window=60.0)


@pytest.mark.parametrize("window", [0.0, -1.0, -0.001])
def test_login_limiter_refuses_a_window_that_is_not_positive(window: float) -> None:
    from wreath.users import LoginLimiter

    with pytest.raises(ValueError, match="window must be positive"):
        LoginLimiter(max_attempts=3, window=window)


@pytest.mark.parametrize("window", [float("nan"), float("inf")])
def test_login_limiter_refuses_a_non_finite_window(window: float) -> None:
    from wreath.users import LoginLimiter

    with pytest.raises(ValueError, match="positive and finite"):
        LoginLimiter(max_attempts=3, window=window)


def test_login_limiter_accepts_the_smallest_legal_configuration() -> None:
    from wreath.users import LoginLimiter

    limiter = LoginLimiter(max_attempts=1, window=0.001)
    assert limiter.allow("someone@example.com") is True


@pytest.mark.parametrize("parameter", ["login_limiter", "reset_limiter"])
def test_user_router_refuses_an_invalid_attempt_limiter(parameter: str) -> None:
    with pytest.raises(TypeError, match=rf"{parameter} must provide async admit_key\(key\)"):
        user_router(
            InMemoryUserStore(),
            sessions=_REVOCATIONS,
            secret="s" * 32,
            **{parameter: object()},
        )


async def test_injected_attempt_limiter_is_shared_by_login_and_reset(monkeypatch) -> None:
    import wreath.users as users
    from wreath.response import JSONResponse
    from wreath.users import ForgotInput, LoginInput

    class SharedLimiter:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.counts: dict[str, int] = {}

        async def admit_key(self, key: str):
            self.calls.append(key)
            self.counts[key] = self.counts.get(key, 0) + 1
            if self.counts[key] > 1:
                return JSONResponse({"error": "limited"}, status=429)
            return None

    class State:
        session: dict[str, Any] = {}

    class Request:
        state = State()

    local_failures: list[str] = []

    class LocalLimiter:
        def __init__(
            self, *, max_attempts: int, window: float, max_tracked: int = 4096
        ) -> None:
            del max_attempts, window, max_tracked

        def record_failure(self, key: str) -> None:
            local_failures.append(key)

    authenticated = 0
    reset_started = 0

    async def authenticate(*_args: Any) -> None:
        nonlocal authenticated
        authenticated += 1
        return None

    async def start_reset(*_args: Any, **_kwargs: Any) -> None:
        nonlocal reset_started
        reset_started += 1

    monkeypatch.setattr(users._userkit, "authenticate", authenticate)
    monkeypatch.setattr(users._userkit, "start_password_reset", start_reset)
    monkeypatch.setattr(users, "LoginLimiter", LocalLimiter)
    limiter = SharedLimiter()
    routers = [
        user_router(
            InMemoryUserStore(),
            sessions=_REVOCATIONS,
            secret="s" * 32,
            login_limiter=limiter,
            reset_limiter=limiter,
        )
        for _ in range(2)
    ]
    login_endpoints = [
        next(route.endpoint for route in router.routes if route.path.endswith("/login"))
        for router in routers
    ]
    reset_endpoints = [
        next(route.endpoint for route in router.routes if route.path.endswith("/forgot-password"))
        for router in routers
    ]

    login_responses = [
        await endpoint(Request(), LoginInput("Ann@Example.com", "wrong"))
        for endpoint in login_endpoints
    ]
    reset_responses = [
        await endpoint(Request(), ForgotInput("Ann@Example.com")) for endpoint in reset_endpoints
    ]

    assert [response.status for response in login_responses] == [401, 429]
    assert [response.status for response in reset_responses] == [200, 200]
    assert authenticated == 1
    assert reset_started == 1
    assert local_failures == []
    assert limiter.calls == [
        "login:ann@example.com",
        "login:ann@example.com",
        "password-reset:ann@example.com",
        "password-reset:ann@example.com",
    ]


def test_a_verify_link_and_a_reset_link_are_different_urls() -> None:
    from wreath.users import _default_link

    build = _default_link("https://app.example.com/", "/users")

    verify = build("verify", "tok123")
    reset = build("reset", "tok123")

    assert verify == "https://app.example.com/users/verify/tok123"
    assert reset == "https://app.example.com/users/reset-password?token=tok123"
    assert verify != reset
    assert "tok123" in verify and "tok123" in reset


def test_the_base_url_keeps_exactly_one_slash_before_the_prefix() -> None:
    from wreath.users import _default_link

    with_slash = _default_link("https://app.example.com/", "/users")("verify", "t")
    without = _default_link("https://app.example.com", "/users")("verify", "t")
    assert with_slash == without
    assert "//users" not in with_slash.removeprefix("https://")


async def test_default_mailer_does_not_print_action_tokens(capsys) -> None:
    from wreath.users import ForgotInput, RegisterInput, hash_password

    store = InMemoryUserStore()
    await store.create("ann@example.com", hash_password("hunter2hunter2"))
    router = user_router(
        store,
        sessions=_REVOCATIONS,
        secret="s" * 32,
        base_url="https://app.example.com",
    )
    endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("/forgot-password")
    )
    register = next(route.endpoint for route in router.routes if route.path.endswith("/register"))

    await endpoint(object(), ForgotInput("ann@example.com"))
    await register(object(), RegisterInput("bea@example.com", "hunter2hunter2"))

    output = capsys.readouterr().out
    assert "ann@example.com" in output
    assert "bea@example.com" in output
    assert "reset-password?token=" not in output
    assert "/verify/" not in output


async def test_log_mailer_exposes_a_link_only_after_an_explicit_opt_in() -> None:
    from wreath.users import LogEmailSender

    output: list[str] = []
    sender = LogEmailSender(output.append, expose_tokens=True)

    await sender.send_password_reset("ann@example.com", "https://app.test/reset?token=secret")

    assert output == [
        "[wreath.users] reset ann@example.com: https://app.test/reset?token=secret"
    ]


async def test_direct_password_reset_rejects_an_invalid_token() -> None:
    from wreath import _userkit

    assert not await _userkit.reset_password(
        InMemoryUserStore(),
        secret="s" * 32,
        token="invalid",
        new_password="a-much-better-password",
    )


async def test_password_stays_unchanged_when_session_revocation_fails() -> None:
    from wreath import _userkit
    from wreath.users import hash_password, reset_password_endpoint

    class FailingRevocations:
        async def delete_for(self, _subject: str) -> int:
            raise RuntimeError("session store unavailable")

    store = InMemoryUserStore()
    original = await store.create("ann@example.com", hash_password("hunter2hunter2"))
    token = _userkit.sign_token(
        "s" * 32,
        _userkit._RESET,
        original.id,
        ttl=3600,
        bound=_userkit.fingerprint(original.hashed_password),
    )

    with pytest.raises(RuntimeError, match="session store unavailable"):
        await reset_password_endpoint(
            store,
            FailingRevocations(),
            secret="s" * 32,
            token=token,
            new_password="a-much-better-password",
        )

    current = await store.get_by_id(original.id)
    assert current is not None
    assert current.hashed_password == original.hashed_password


async def test_password_reset_revokes_on_both_sides_of_the_credential_change() -> None:
    from wreath import _userkit
    from wreath.users import hash_password, reset_password_endpoint

    events: list[str] = []

    class OrderingStore(InMemoryUserStore):
        async def compare_and_set_password(
            self, user_id: str, expected: str, replacement: str
        ) -> bool:
            events.append("credential-change")
            return await super().compare_and_set_password(user_id, expected, replacement)

    class Revocations:
        async def delete_for(self, _subject: str) -> int:
            events.append("revoke")
            return 0

    store = OrderingStore()
    original = await store.create("ann@example.com", hash_password("hunter2hunter2"))
    token = _userkit.sign_token(
        "s" * 32,
        _userkit._RESET,
        original.id,
        ttl=3600,
        bound=_userkit.fingerprint(original.hashed_password),
    )

    assert await reset_password_endpoint(
        store,
        Revocations(),
        secret="s" * 32,
        token=token,
        new_password="a-much-better-password",
    )
    assert events == ["revoke", "credential-change", "revoke"]


async def test_failed_password_compare_and_set_does_not_run_the_second_revocation() -> None:
    from wreath import _userkit
    from wreath.users import hash_password, reset_password_endpoint

    class RacingStore(InMemoryUserStore):
        async def compare_and_set_password(
            self, user_id: str, expected: str, replacement: str
        ) -> bool:
            return False

    revoked: list[str] = []

    class Revocations:
        async def delete_for(self, subject: str) -> int:
            revoked.append(subject)
            return 0

    store = RacingStore()
    original = await store.create("ann@example.com", hash_password("hunter2hunter2"))
    token = _userkit.sign_token(
        "s" * 32,
        _userkit._RESET,
        original.id,
        ttl=3600,
        bound=_userkit.fingerprint(original.hashed_password),
    )

    assert not await reset_password_endpoint(
        store,
        Revocations(),
        secret="s" * 32,
        token=token,
        new_password="a-much-better-password",
    )
    assert revoked == [original.id]


async def test_invalid_password_reset_token_does_not_revoke_sessions() -> None:
    from wreath.users import reset_password_endpoint

    revoked: list[str] = []

    class Revocations:
        async def delete_for(self, subject: str) -> int:
            revoked.append(subject)
            return 0

    assert not await reset_password_endpoint(
        InMemoryUserStore(),
        Revocations(),
        secret="s" * 32,
        token="invalid",
        new_password="a-much-better-password",
    )
    assert revoked == []


async def test_a_bad_verification_token_is_a_400_and_says_it_was_invalid() -> None:
    from wreath.app import Wreath
    from wreath.testing import TestClient

    app = Wreath()
    app.include_router(user_router(InMemoryUserStore(), sessions=_REVOCATIONS, secret="s" * 32))
    async with TestClient(app) as client:
        response = await client.post("/users/verify", json={"token": "not-a-real-token"})
        assert response.status == 400
        assert response.json()["status"] == "invalid_token"


async def test_a_real_verification_token_is_a_200_and_says_verified() -> None:
    from wreath import _userkit
    from wreath.app import Wreath
    from wreath.testing import TestClient
    from wreath.users import hash_password

    store = InMemoryUserStore()
    user = await store.create("ann@example.com", hash_password("hunter2hunter2"))
    # The same call `_userkit.request_email_verification` makes, with the module's own
    # purpose constant rather than the literal, so a renamed purpose fails here too.
    token = _userkit.sign_token("s" * 32, _userkit._VERIFY, user.id, ttl=3600)

    app = Wreath()
    app.include_router(user_router(store, sessions=_REVOCATIONS, secret="s" * 32))
    async with TestClient(app) as client:
        response = await client.post("/users/verify", json={"token": token})
        assert response.status == 200
        assert response.json()["status"] == "verified"


async def test_the_verification_link_refuses_a_forged_token() -> None:
    from wreath.app import Wreath
    from wreath.testing import TestClient

    app = Wreath()
    app.include_router(user_router(InMemoryUserStore(), sessions=_REVOCATIONS, secret="s" * 32))
    async with TestClient(app) as client:
        response = await client.get("/users/verify/not-a-real-token")
        assert response.status == 400
        assert response.json()["status"] == "invalid_token"


async def test_the_verification_link_accepts_a_real_token() -> None:
    from wreath import _userkit
    from wreath.app import Wreath
    from wreath.testing import TestClient
    from wreath.users import hash_password

    store = InMemoryUserStore()
    user = await store.create("bea@example.com", hash_password("hunter2hunter2"))
    token = _userkit.sign_token("s" * 32, _userkit._VERIFY, user.id, ttl=3600)

    app = Wreath()
    app.include_router(user_router(store, sessions=_REVOCATIONS, secret="s" * 32))
    async with TestClient(app) as client:
        response = await client.get(f"/users/verify/{token}")
        assert response.status == 200
        assert response.json()["status"] == "verified"
