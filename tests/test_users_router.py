from __future__ import annotations

import pytest

from wreath.users import InMemoryUserStore, OrmUserStore, default_user_model, user_router


def _routes(router):
    return {(r.path, m) for r in router.routes for m in r.methods}


def test_user_router_exposes_lifecycle_routes():
    router = user_router(InMemoryUserStore(), secret="s" * 32, base_url="https://app")
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
    router = user_router(InMemoryUserStore(), secret="s" * 32, prefix="/accounts")
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


# A sweep over `src/wreath/users.py` reported these as `unreached`: no test executed
# them at all. They are the same shape as the `orm/types.py` findings -- the behaviour
# was covered and the *validation* was not -- with one that is a live branch rather
# than a refusal.


@pytest.mark.parametrize("max_attempts", [0, -1, -100])
def test_login_limiter_refuses_a_budget_of_no_attempts(max_attempts: int) -> None:
    from wreath.users import LoginLimiter

    with pytest.raises(ValueError, match="max_attempts must be at least 1"):
        LoginLimiter(max_attempts=max_attempts, window=60.0)


@pytest.mark.parametrize("window", [0.0, -1.0, -0.001])
def test_login_limiter_refuses_a_window_that_is_not_positive(window: float) -> None:
    from wreath.users import LoginLimiter

    with pytest.raises(ValueError, match="window must be positive"):
        LoginLimiter(max_attempts=3, window=window)


def test_login_limiter_accepts_the_smallest_legal_configuration() -> None:
    from wreath.users import LoginLimiter

    limiter = LoginLimiter(max_attempts=1, window=0.001)
    assert limiter.allow("someone@example.com") is True


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


async def test_a_bad_verification_token_is_a_400_and_says_it_was_invalid() -> None:
    from wreath.app import Wreath
    from wreath.testing import TestClient

    app = Wreath()
    app.include_router(user_router(InMemoryUserStore(), secret="s" * 32))
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
    app.include_router(user_router(store, secret="s" * 32))
    async with TestClient(app) as client:
        response = await client.post("/users/verify", json={"token": token})
        assert response.status == 200
        assert response.json()["status"] == "verified"


async def test_the_verification_link_refuses_a_forged_token() -> None:
    from wreath.app import Wreath
    from wreath.testing import TestClient

    app = Wreath()
    app.include_router(user_router(InMemoryUserStore(), secret="s" * 32))
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
    app.include_router(user_router(store, secret="s" * 32))
    async with TestClient(app) as client:
        response = await client.get(f"/users/verify/{token}")
        assert response.status == 200
        assert response.json()["status"] == "verified"
