"""Second factors, stage two: step-up, the policy seam, and factor removal.

One test per requirement, deliberately not merged. A single test that logged in,
stepped up, and deleted a factor would pass with the freshness check missing,
with the ownership check missing, or with the rotation missing -- and each of
those three is the whole of a different attack.

Run under `python -O` as well as normally: every check here is a `raise` or a
refusal, and `-O` is the mode where an `assert` would not be.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated, Any

import pytest

from wreath import Wreath
from wreath._auth.requirements import (
    AuthRequirement,
    merge_requirements,
    second_factor_age,
)
from wreath._secondfactor import (
    SecondFactor,
    base32_to_secret,
    remove_second_factor,
    totp_code,
    totp_counter,
)
from wreath.auth import Identity, SessionIdentityBackend, authenticated, second_factor
from wreath.authorization import CedarAuthorizer, CedarPolicies, EntityUid, authorize
from wreath.binding import Query
from wreath.middleware.sessions import SessionMiddleware
from wreath.testing import TestClient
from wreath.users import (
    InMemorySecondFactorStore,
    InMemoryUserStore,
    hash_password,
    second_factor_router,
    user_router,
)

PASSWORD = "correct horse battery staple"


class _Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _cookie(response: Any) -> str:
    value = response.header("set-cookie")
    assert value is not None
    return value.split(";", 1)[0]


# --- the requirement itself, with no HTTP in the way -------------------------


def _identity(**claims: Any) -> Identity:
    return Identity(id="user-1", claims=claims)


def test_an_identity_that_never_proved_a_factor_has_no_age() -> None:
    """None is a refusal, not a zero -- a bearer token must not read as fresh."""
    assert second_factor_age(_identity(), 1_700_000_000.0) is None


def test_a_stamp_in_the_future_reads_as_age_zero_not_a_negative_age() -> None:
    """A clock that stepped back must not make a factor fresher than fresh."""
    age = second_factor_age(_identity(second_factor_at=1_700_000_600), 1_700_000_000.0)
    assert age == 0


def test_a_boolean_stamp_is_not_a_timestamp() -> None:
    """`True` is an `int` in Python, and would otherwise read as 1970."""
    assert second_factor_age(_identity(second_factor_at=True), 1_700_000_000.0) is None


def test_the_age_is_an_integer_because_cedar_has_no_floats() -> None:
    age = second_factor_age(_identity(second_factor_at=1_699_999_940.5), 1_700_000_000.0)
    assert isinstance(age, int) and not isinstance(age, bool)
    assert age == 59


def test_merging_requirements_keeps_the_strictest_window() -> None:
    """A route mounted inside a strict router must not relax it."""
    merged = merge_requirements(
        AuthRequirement(second_factor=60.0), AuthRequirement(second_factor=3600.0)
    )
    assert merged.second_factor == 60.0


def test_merging_leaves_the_window_absent_when_nobody_asked() -> None:
    merged = merge_requirements(AuthRequirement(authenticated=True), AuthRequirement())
    assert merged.second_factor is None


def test_stacking_the_decorator_keeps_the_shorter_window() -> None:
    @second_factor(max_age=3600)
    @second_factor(max_age=30)
    async def endpoint(request: Any) -> dict[str, Any]:
        return {}

    from wreath._auth.requirements import requirement_for

    requirement = requirement_for(endpoint)
    assert requirement.second_factor == 30.0
    # And it implies authentication, so an anonymous caller is a 401 rather than
    # reaching the freshness check with no identity at all.
    assert requirement.authenticated is True


def test_the_decorator_refuses_a_window_that_can_never_be_satisfied() -> None:
    with pytest.raises(ValueError):
        second_factor(max_age=0)


# --- the requirement, enforced on a route ------------------------------------


def _sign_in_routes(app: Wreath) -> None:
    """Two routes that write the session `wreath.users` would have written.

    Deliberately not the real login flow: these tests are about the requirement
    reading a stamp, and driving a whole TOTP enrolment to produce one would
    make a failure ambiguous between the two halves. `age` is how many seconds
    ago the factor was proved, so a test names the interesting number.
    """

    @app.post("/sign-in")
    async def sign_in(request: Any) -> dict[str, Any]:
        request.state.session["principal"] = {"sub": "user-1", "type": "User"}
        return {"ok": True}

    @app.post("/prove")
    async def prove(request: Any, age: Annotated[float, Query()] = 0.0) -> dict[str, Any]:
        principal = dict(request.state.session["principal"])
        principal["second_factor_at"] = int(time.time() - age)
        request.state.session["principal"] = principal
        return {"ok": True}


def _guarded_app(window: float = 300.0) -> Wreath:
    app = Wreath()
    app.add_global_middleware(SessionMiddleware(secret="s" * 32, secure=False))
    app.configure_auth(SessionIdentityBackend())

    @app.get("/vault")
    @second_factor(max_age=window)
    async def vault(request: Any) -> dict[str, Any]:
        return {"ok": True}

    @app.get("/inbox")
    @authenticated()
    async def inbox(request: Any) -> dict[str, Any]:
        return {"ok": True}

    _sign_in_routes(app)
    return app


async def test_an_authenticated_caller_without_a_factor_is_refused() -> None:
    """Authenticated is not enough; the route asks *when*, and gets no answer."""
    async with TestClient(_guarded_app()) as client:
        cookie = _cookie(await client.post("/sign-in"))
        # The same identity is admitted where no factor is demanded, so this is
        # the freshness check refusing rather than authentication failing.
        assert (await client.get("/inbox", headers={"cookie": cookie})).status == 200
        refused = await client.get("/vault", headers={"cookie": cookie})
        assert refused.status == 403


async def test_a_freshly_proved_factor_admits_the_same_caller() -> None:
    async with TestClient(_guarded_app()) as client:
        cookie = _cookie(await client.post("/sign-in"))
        proved = await client.post("/prove", headers={"cookie": cookie})
        cookie = _cookie(proved) or cookie
        assert (await client.get("/vault", headers={"cookie": cookie})).status == 200


async def test_a_stale_factor_is_refused() -> None:
    """The point of the whole stage: having one is not having proved one lately."""
    async with TestClient(_guarded_app(window=300.0)) as client:
        cookie = _cookie(await client.post("/sign-in"))
        proved = await client.post("/prove?age=3600", headers={"cookie": cookie})
        cookie = _cookie(proved) or cookie
        refused = await client.get("/vault", headers={"cookie": cookie})
        assert refused.status == 403


async def test_an_anonymous_caller_is_a_401_not_a_403() -> None:
    """Different remediations: sign in, versus prove a factor."""
    async with TestClient(_guarded_app()) as client:
        assert (await client.get("/vault")).status == 401


# --- the Cedar seam ----------------------------------------------------------

STEP_UP_POLICY = """
permit(principal, action == Action::"close", resource)
when { context has second_factor_age && context.second_factor_age <= 300 };
"""


def _policy_app() -> Wreath:
    app = Wreath()
    app.add_global_middleware(SessionMiddleware(secret="s" * 32, secure=False))
    app.configure_auth(
        SessionIdentityBackend(),
        CedarAuthorizer(engine=CedarPolicies(STEP_UP_POLICY)),
    )

    @app.post("/close")
    @authorize(action="close", resource=EntityUid("Account", "main"))
    async def close(request: Any) -> dict[str, Any]:
        return {"ok": True}

    _sign_in_routes(app)
    return app


async def test_a_policy_requiring_a_recent_factor_denies_without_one() -> None:
    """`context has second_factor_age` is false, so the permit does not apply."""
    async with TestClient(_policy_app()) as client:
        cookie = _cookie(await client.post("/sign-in"))
        assert (await client.post("/close", headers={"cookie": cookie})).status == 403


async def test_a_policy_requiring_a_recent_factor_allows_with_one() -> None:
    async with TestClient(_policy_app()) as client:
        cookie = _cookie(await client.post("/sign-in"))
        cookie = _cookie(await client.post("/prove", headers={"cookie": cookie})) or cookie
        assert (await client.post("/close", headers={"cookie": cookie})).status == 200


async def test_a_policy_requiring_a_recent_factor_denies_a_stale_one() -> None:
    async with TestClient(_policy_app()) as client:
        cookie = _cookie(await client.post("/sign-in"))
        stale = await client.post("/prove?age=3600", headers={"cookie": cookie})
        cookie = _cookie(stale) or cookie
        assert (await client.post("/close", headers={"cookie": cookie})).status == 403


def test_the_cedar_context_omits_the_age_rather_than_faking_one() -> None:
    """Absent, not a sentinel: `when` and `unless` policies both fail closed."""
    from wreath._auth.cedar import _default_context

    class _Request:
        method = "POST"
        path = "/close"
        identity = None

    assert "second_factor_age" not in _default_context(_Request())


# --- the flows over HTTP -----------------------------------------------------


def _app(
    users: InMemoryUserStore,
    factors: InMemorySecondFactorStore,
    clock: _Clock,
    **options: Any,
) -> Wreath:
    app = Wreath()
    app.add_global_middleware(SessionMiddleware(secret="s" * 32, secure=False))
    app.include_router(
        user_router(users, secret="u" * 32, second_factors=factors, clock=clock)
    )
    app.include_router(
        second_factor_router(users, factors, issuer="Wreath", clock=clock, **options)
    )

    @app.get("/session")
    async def show(request: Any) -> dict[str, Any]:
        return dict(request.state.session)

    return app


async def _login(client: Any, email: str = "ann@example.test") -> Any:
    return await client.post("/users/login", json={"email": email, "password": PASSWORD})


async def _enrol(client: Any, clock: _Clock, cookie: str) -> tuple[str, list[str], str]:
    begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
    assert begun.status == 200
    cookie = _cookie(begun) or cookie
    secret_b32 = begun.json()["secret"]
    code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
    confirmed = await client.post(
        "/auth/2fa/totp/confirm", json={"code": code}, headers={"cookie": cookie}
    )
    assert confirmed.status == 200, confirmed.json()
    return secret_b32, confirmed.json()["recovery_codes"], _cookie(confirmed) or cookie


async def _seeded(
    client: Any, users: InMemoryUserStore, clock: _Clock
) -> tuple[str, str, Any]:
    """A signed-in, enrolled user; returns (base32 secret, cookie, user)."""
    user = await users.create("ann@example.test", hash_password(PASSWORD))
    secret_b32, _, cookie = await _enrol(client, clock, _cookie(await _login(client)))
    return secret_b32, cookie, user


async def test_confirming_an_enrolment_stamps_the_session() -> None:
    """The code just checked *is* a proved factor; not stamping it is a dance."""
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        _, cookie, _ = await _seeded(client, users, clock)
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert session["principal"]["second_factor_at"] == int(clock.now)


async def test_promotion_stamps_the_session() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        secret_b32, cookie, _ = await _seeded(client, users, clock)
        await client.post("/users/logout", headers={"cookie": cookie})
        clock.now += 60
        pending = _cookie(await _login(client))
        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        promoted = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": pending}
        )
        assert promoted.status == 200
        session = (
            await client.get("/session", headers={"cookie": _cookie(promoted)})
        ).json()
        assert session["principal"]["second_factor_at"] == int(clock.now)


async def test_step_up_restamps_an_already_signed_in_session() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        secret_b32, cookie, _ = await _seeded(client, users, clock)
        clock.now += 3600
        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        stepped = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": cookie}
        )
        assert stepped.status == 200
        assert stepped.json() == {"status": "second_factor_verified"}
        session = (
            await client.get("/session", headers={"cookie": _cookie(stepped)})
        ).json()
        assert session["principal"]["second_factor_at"] == int(clock.now)


async def test_step_up_rotates_the_session_id() -> None:
    """Gaining the right to delete things is a privilege change like any other."""
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        secret_b32, cookie, _ = await _seeded(client, users, clock)
        clock.now += 3600
        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        stepped = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": cookie}
        )
        assert _cookie(stepped) != cookie


async def test_a_wrong_code_leaves_the_stamp_alone() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        _, cookie, _ = await _seeded(client, users, clock)
        enrolled_at = int(clock.now)
        clock.now += 3600
        refused = await client.post(
            "/auth/2fa/verify", json={"code": "000000"}, headers={"cookie": cookie}
        )
        assert refused.status == 401
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert session["principal"]["second_factor_at"] == enrolled_at


async def test_step_up_is_throttled_per_user() -> None:
    """Otherwise the guard on a destructive action is a million-guess loop."""
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _app(users, factors, clock, max_verify_attempts=2, verify_window=300.0)
    async with TestClient(app) as client:
        secret_b32, cookie, _ = await _seeded(client, users, clock)
        clock.now += 3600
        for _ in range(2):
            wrong = await client.post(
                "/auth/2fa/verify", json={"code": "000000"}, headers={"cookie": cookie}
            )
            assert wrong.status == 401
        refused = await client.post(
            "/auth/2fa/verify", json={"code": "000000"}, headers={"cookie": cookie}
        )
        assert refused.status == 429
        # The correct code is refused too, or the throttle is a speed bump.
        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        still = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": cookie}
        )
        assert still.status == 429


async def test_step_up_without_a_session_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await users.create("ann@example.test", hash_password(PASSWORD))
        response = await client.post("/auth/2fa/verify", json={"code": "000000"})
        assert response.status == 401


async def test_step_up_with_nothing_enrolled_says_so(
) -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await users.create("ann@example.test", hash_password(PASSWORD))
        cookie = _cookie(await _login(client))
        response = await client.post(
            "/auth/2fa/verify", json={"code": "000000"}, headers={"cookie": cookie}
        )
        assert response.status == 400
        assert response.json() == {"error": "no_second_factor_enrolled"}


# --- DELETE /auth/2fa/{id} ---------------------------------------------------


async def test_the_router_mounts_the_removal_route() -> None:
    users, factors = InMemoryUserStore(), InMemorySecondFactorStore()
    router = second_factor_router(users, factors)
    routes = {(route.path, method) for route in router.routes for method in route.methods}
    assert ("/auth/2fa/{factor_id}", "DELETE") in routes


async def test_removal_requires_a_fresh_second_factor() -> None:
    """The act somebody holding a stolen session wants most."""
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _app(users, factors, clock, step_up_ttl=300.0)
    async with TestClient(app) as client:
        _, cookie, user = await _seeded(client, users, clock)
        listed = (await client.get("/auth/2fa", headers={"cookie": cookie})).json()
        factor_id = listed["factors"][0]["id"]

        clock.now += 301
        refused = await client.delete(
            f"/auth/2fa/{factor_id}", headers={"cookie": cookie}
        )
        assert refused.status == 403
        assert refused.json() == {"error": "second_factor_required"}
        assert len(await factors.credentials(user.id)) == 11


async def test_removal_succeeds_after_stepping_up() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _app(users, factors, clock, step_up_ttl=300.0)
    async with TestClient(app) as client:
        secret_b32, cookie, user = await _seeded(client, users, clock)
        listed = (await client.get("/auth/2fa", headers={"cookie": cookie})).json()
        factor_id = listed["factors"][0]["id"]

        clock.now += 3600
        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        stepped = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": cookie}
        )
        cookie = _cookie(stepped) or cookie
        removed = await client.delete(
            f"/auth/2fa/{factor_id}", headers={"cookie": cookie}
        )
        assert removed.status == 200
        assert removed.json()["id"] == factor_id


async def test_removal_is_scoped_to_the_owner() -> None:
    """The credential id is the only thing an HTTP caller supplies."""
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _app(users, factors, clock)
    async with TestClient(app) as client:
        _, victim_cookie, victim = await _seeded(client, users, clock)
        victim_factor = (
            await client.get("/auth/2fa", headers={"cookie": victim_cookie})
        ).json()["factors"][0]["id"]

        await users.create("bob@example.test", hash_password(PASSWORD))
        attacker_cookie = _cookie(await _login(client, "bob@example.test"))
        _, _, attacker_cookie = await _enrol(client, clock, attacker_cookie)

        response = await client.delete(
            f"/auth/2fa/{victim_factor}", headers={"cookie": attacker_cookie}
        )
        assert response.status == 404
        assert any(row.id == victim_factor for row in await factors.credentials(victim.id))


async def test_removal_needs_an_authenticated_session() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        response = await client.delete("/auth/2fa/anything")
        assert response.status == 401


async def test_removing_the_last_factor_takes_the_recovery_codes_with_it() -> None:
    """"Off" must mean off, not a login that still demands a code."""
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        _, cookie, user = await _seeded(client, users, clock)
        factor_id = (
            await client.get("/auth/2fa", headers={"cookie": cookie})
        ).json()["factors"][0]["id"]
        removed = await client.delete(f"/auth/2fa/{factor_id}", headers={"cookie": cookie})
        assert removed.status == 200
        assert await factors.credentials(user.id) == []
        # And the next login is therefore not pending.
        await client.post("/users/logout", headers={"cookie": cookie})
        again = await _login(client)
        assert again.json()["email"] == "ann@example.test"


async def test_a_recovery_credential_cannot_be_deleted_by_id() -> None:
    """One at a time only ever moves a user closer to being locked out."""
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        _, cookie, user = await _seeded(client, users, clock)
        recovery = next(
            row for row in await factors.credentials(user.id) if row.kind == "recovery"
        )
        response = await client.delete(
            f"/auth/2fa/{recovery.id}", headers={"cookie": cookie}
        )
        assert response.status == 404
        assert any(row.id == recovery.id for row in await factors.credentials(user.id))


async def test_remove_second_factor_refuses_another_users_credential() -> None:
    """The flow, without HTTP: an id that is not this user's is simply not found."""
    factors = InMemorySecondFactorStore()
    await factors.add(
        SecondFactor(
            id="cred-1", user_id="user-1", kind="totp", label="Phone",
            created_at=datetime.now(UTC), last_used_at=None,
            material=b"a-twenty-byte-secret",
        )
    )
    assert await remove_second_factor(factors, "user-2", "cred-1") is None
    assert len(await factors.credentials("user-1")) == 1
