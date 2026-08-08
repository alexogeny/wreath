"""Adversarial tests for the authentication and second-factor surface.

Every test here was written as an *attack* and watched fail before the thing it
attacks was fixed (docs/decisions/0024). Each attack sits beside a control that
passes in both trees, so a suite that has simply broken everywhere cannot be
mistaken for one that caught something.

Five holes, in five different places:

* **A half-wired second factor that fails open.** `user_router` refuses a login
  it cannot complete when it was given no `second_factors=` at all. Given the
  *wrong* store -- a second `SecondFactorStore` that is not the one
  `second_factor_router` writes to -- it saw an account with no credentials and
  signed the caller straight in. The same mistake, the opposite direction.
* **A recovery code that is single-use unless you send it twice at once.** The
  TOTP branch of `verify_second_factor` closes that race with the store's
  conditional counter advance; the recovery branch removed the row without ever
  asking whether it was the one that removed it.
* **A compact JWS the pure build parses and the native build refuses.** The
  stdlib fallback pads each segment before decoding, so a token with `=` glued
  on the end verifies under `WREATH_PURE=1` and is rejected by `jose.c`. The
  same fallback raises `TypeError` out of `verify_jwt`, which is documented to
  return None and never raise, when `aud` holds an unhashable member.
* **An authentication backend that raises leaves through the front door.** On a
  classifying routing table -- `bitset`, the default, and `decision` -- the
  backend runs before the route is resolved and outside every error boundary,
  so the exception escaped `__call__` with no response started and every global
  `after` hook skipped. `trie` answered 500 for the same backend. On a
  WebSocket the authorization step caught only `HTTPException`, so anything
  else left the handshake neither accepted nor closed and the peer hanging.
* **A shipped example that raises.** `identify()`'s runnable snippet, rendered
  on `docs/reference/auth.md`, read `identity.subject`; the field is `id`.

Run under `python -O` as well: `-O` deletes a test's own `assert`, so the
JWT half of this file drives its subject in a subprocess that `raise`s, and
`test_the_probes_still_have_something_to_check` is the guard on the guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from wreath import Wreath
from wreath._secondfactor import (
    InMemorySecondFactorStore,
    confirm_totp_enrolment,
    generate_totp_secret,
    totp_code,
    totp_counter,
    verify_second_factor,
)
from wreath.policy import HttpPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.testing import TestClient
from wreath.users import (
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
    return value.split(";", 1)[0] if value else ""


# --- 1. the half-wired second factor -----------------------------------------
#
# `docs/decisions/0019` settles the direction: a door that refuses and names its
# own misconfiguration beats one that opens quietly. `user_router` already does
# that for `second_factors=None`. These drive the other half-wiring.


def _two_router_app(
    users: InMemoryUserStore,
    login_store: Any,
    enrol_store: Any,
    clock: _Clock,
) -> Wreath:
    """A `user_router` and a `second_factor_router` that may or may not agree.

    Passing the two stores separately is the whole point: `login_store` is what
    the login path consults and `enrol_store` is where the factor actually
    lands. A correctly wired application passes the same object twice.
    """
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
    app.include_router(
        user_router(users, secret="u" * 32, second_factors=login_store, clock=clock)
    )
    with warnings.catch_warnings():
        # The router warns about `enrolments=None`; that is a different subject.
        warnings.simplefilter("ignore", UserWarning)
        app.include_router(
            second_factor_router(users, enrol_store, issuer="Wreath", clock=clock)
        )

    @app.get("/session")
    async def show(request: Any) -> dict[str, Any]:
        return dict(request.state.session)

    return app


async def _login(client: Any, cookie: str = "") -> Any:
    headers = {"cookie": cookie} if cookie else {}
    return await client.post(
        "/users/login",
        json={"email": "ann@example.test", "password": PASSWORD},
        headers=headers,
    )


async def _enrol_totp(client: Any, clock: _Clock, cookie: str) -> str:
    begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
    assert begun.status == 200, begun.json()
    cookie = _cookie(begun) or cookie
    from wreath._secondfactor import base32_to_secret

    code = totp_code(base32_to_secret(begun.json()["secret"]), totp_counter(clock.now))
    done = await client.post(
        "/auth/2fa/totp/confirm", json={"code": code}, headers={"cookie": cookie}
    )
    assert done.status == 200, done.json()
    return _cookie(done) or cookie


async def test_a_login_wired_to_the_wrong_factor_store_refuses_rather_than_admits() -> None:
    """The attack: enrol a factor, then sign in with the password alone.

    Two `SecondFactorStore`s, which is what a deployment gets by building one
    inline for each router. The account demonstrably has a TOTP factor and this
    login path cannot see it, which is the same knowable moment `second_factors=None`
    is refused at -- so it must refuse here too, rather than issuing a session
    that says the second factor was satisfied when nothing asked for one.
    """
    users, clock = InMemoryUserStore(), _Clock()
    login_store, enrol_store = InMemorySecondFactorStore(), InMemorySecondFactorStore()
    app = _two_router_app(users, login_store, enrol_store, clock)
    async with TestClient(app) as client:
        await users.create("ann@example.test", hash_password(PASSWORD))
        cookie = await _enrol_totp(client, clock, _cookie(await _login(client)))
        await client.post("/users/logout", headers={"cookie": cookie})
        clock.now += 60

        signed_in = await _login(client)

        assert signed_in.status == 500, signed_in.json()
        assert signed_in.json()["error"] == "second_factor_not_wired"
        session = (
            await client.get("/session", headers={"cookie": _cookie(signed_in)})
        ).json()
        # And no session was written -- neither a principal nor a pending marker.
        assert "principal" not in session
        assert "pending_second_factor" not in session


class _SharedUserStore(InMemoryUserStore):
    """Two objects, one table -- the shape `OrmUserStore` has by construction.

    `OrmUserStore(session, model)` holds no data of its own, so two of them over
    the same model read and write the same rows. A deployment that builds one
    inline for each router therefore has two `UserStore` *objects* serving one
    set of users, and `_SecondFactorWiring` matched them with `is`. Here the
    shared table stands in for the shared database, and `store_id` is the
    declared identity the ORM stores derive from their model.
    """

    def __init__(self, shared: dict[str, Any], store_id: object) -> None:
        super().__init__()
        self._by_id = shared["by_id"]
        self._by_email = shared["by_email"]
        self.store_id = store_id


async def test_two_user_store_objects_over_one_table_do_not_defeat_the_wiring_check(
) -> None:
    """The identity keying, attacked where it is weakest.

    `_mounted_second_factors(users)` filtered on `wiring.users is users`, so a
    deployment that constructed its `UserStore` separately for each router
    matched nothing at all: the login found no wiring to consult, decided the
    account had no factor it could not check, and signed the caller in on a
    password alone with 2FA enrolled. Every signal saying protected, nothing
    being so -- the exact failure `second_factor_not_wired` exists to refuse,
    reached by going around the test that finds it.
    """
    shared: dict[str, Any] = {"by_id": {}, "by_email": {}}
    login_users = _SharedUserStore(shared, store_id="users-table")
    enrol_users = _SharedUserStore(shared, store_id="users-table")
    clock = _Clock()
    login_store, enrol_store = InMemorySecondFactorStore(), InMemorySecondFactorStore()

    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
    app.include_router(
        user_router(login_users, secret="u" * 32, second_factors=login_store, clock=clock)
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        app.include_router(
            second_factor_router(enrol_users, enrol_store, issuer="Wreath", clock=clock)
        )

    @app.get("/session")
    async def show(request: Any) -> dict[str, Any]:
        return dict(request.state.session)

    async with TestClient(app) as client:
        await login_users.create("ann@example.test", hash_password(PASSWORD))
        cookie = await _enrol_totp(client, clock, _cookie(await _login(client)))
        await client.post("/users/logout", headers={"cookie": cookie})
        clock.now += 60

        signed_in = await _login(client)

    assert signed_in.status == 500, signed_in.json()
    assert signed_in.json()["error"] == "second_factor_not_wired"


async def test_two_unrelated_stores_are_still_not_consulted_for_each_other() -> None:
    """The control the widened match must not cost.

    Two applications in one process have different users, and
    `InMemoryUserStore` numbers both from `"1"`. Matching on a user id -- or on
    nothing at all -- would make one application's login refuse because the
    *other* application's user 1 has a factor. Declared identities differ here,
    so neither consults the other.
    """
    clock = _Clock()
    first_users = _SharedUserStore({"by_id": {}, "by_email": {}}, store_id="tenant-a")
    second_users = _SharedUserStore({"by_id": {}, "by_email": {}}, store_id="tenant-b")
    second_factors = InMemorySecondFactorStore()

    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
    # The first application's login reads no second-factor store at all, and its
    # own user has no factor. The second application's user 1 does.
    app.include_router(user_router(first_users, secret="u" * 32, clock=clock))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        app.include_router(
            second_factor_router(second_users, second_factors, issuer="Wreath",
                                 clock=clock)
        )

    async with TestClient(app) as client:
        await first_users.create("ann@example.test", hash_password(PASSWORD))
        user = await second_users.create("bob@example.test", hash_password(PASSWORD))
        secret = generate_totp_secret()
        await confirm_totp_enrolment(
            second_factors, user.id, secret=secret,
            code=totp_code(secret, totp_counter(clock.now)),
            label="phone", at=clock.now,
        )
        signed_in = await _login(client)

    assert signed_in.status == 200, signed_in.json()


async def test_the_same_store_in_both_routers_still_prompts_for_the_factor() -> None:
    """Control: the correct wiring is unchanged, and must not start refusing.

    Passes in both trees. A refusal that fires on a correctly wired application
    is a lockout, not a fix.
    """
    users, clock = InMemoryUserStore(), _Clock()
    factors = InMemorySecondFactorStore()
    app = _two_router_app(users, factors, factors, clock)
    async with TestClient(app) as client:
        await users.create("ann@example.test", hash_password(PASSWORD))
        cookie = await _enrol_totp(client, clock, _cookie(await _login(client)))
        await client.post("/users/logout", headers={"cookie": cookie})
        clock.now += 60

        signed_in = await _login(client)

        assert signed_in.status == 200
        assert signed_in.json()["status"] == "second_factor_required"


async def test_a_user_with_no_factor_signs_in_normally_under_either_wiring() -> None:
    """Control: the refusal is about *this account's* credentials, not the wiring.

    Passes in both trees. Somebody who has enrolled nothing is unaffected by a
    misconfiguration they are not using.
    """
    users, clock = InMemoryUserStore(), _Clock()
    login_store, enrol_store = InMemorySecondFactorStore(), InMemorySecondFactorStore()
    app = _two_router_app(users, login_store, enrol_store, clock)
    async with TestClient(app) as client:
        await users.create("ann@example.test", hash_password(PASSWORD))

        signed_in = await _login(client)

        assert signed_in.status == 200
        assert signed_in.json()["email"] == "ann@example.test"


# --- 2. a recovery code redeemed twice at once -------------------------------


class _SuspendingStore(InMemorySecondFactorStore):
    """`InMemorySecondFactorStore` with a suspension point in every await.

    The dict store happens never to yield, so two verifications interleave only
    where the flow itself awaits. A real store goes to a socket at each of these
    calls, and that is the shape the replay defence has to survive -- so the
    double models the *timing* of a database rather than adding a capability it
    does not have (docs/decisions/0020). `_advance` is untouched: its atomicity
    is the thing under test.
    """

    async def credentials(self, user_id: str) -> list[Any]:
        await asyncio.sleep(0)
        return await super().credentials(user_id)

    async def remove(self, user_id: str, credential_id: str) -> None:
        await asyncio.sleep(0)
        await super().remove(user_id, credential_id)

    async def touch(self, credential_id: str, *, counter: int, at: Any) -> bool:
        await asyncio.sleep(0)
        return await super().touch(credential_id, counter=counter, at=at)


async def _enrolled_store() -> tuple[_SuspendingStore, bytes, list[str], float]:
    at = 1_700_000_000.0
    store = _SuspendingStore()
    secret = generate_totp_secret()
    confirmed = await confirm_totp_enrolment(
        store, "user-1", secret=secret, code=totp_code(secret, totp_counter(at)), at=at
    )
    assert confirmed is not None
    return store, secret, confirmed[1], at


async def test_one_recovery_code_cannot_be_redeemed_by_two_requests_at_once() -> None:
    """The attack: race the legitimate user with a code read over their shoulder.

    Exactly what the TOTP counter exists to stop, against the credential that
    has no counter. A phishing proxy that relays a recovery code and uses it at
    the same moment gets a working second factor *and* leaves the victim's own
    attempt succeeding, so nothing looks wrong to them.
    """
    store, _secret, codes, at = await _enrolled_store()

    first, second = await asyncio.gather(
        verify_second_factor(store, "user-1", codes[0], at=at + 30),
        verify_second_factor(store, "user-1", codes[0], at=at + 30),
    )

    assert [first is not None, second is not None].count(True) == 1
    # And the row is gone, so the winner really did spend it.
    remaining = [row for row in await store.credentials("user-1") if row.kind == "recovery"]
    assert len(remaining) == len(codes) - 1


async def test_a_recovery_code_still_works_once_on_its_own() -> None:
    """Control: single use means once, not never.

    Passes in both trees -- so a refusal that simply broke every recovery code
    would not be mistaken for the fix.
    """
    store, _secret, codes, at = await _enrolled_store()

    assert await verify_second_factor(store, "user-1", codes[0], at=at + 30) is not None
    assert await verify_second_factor(store, "user-1", codes[0], at=at + 60) is None
    assert await verify_second_factor(store, "user-1", codes[1], at=at + 60) is not None


async def test_the_race_harness_sees_the_totp_replay_being_refused() -> None:
    """Control, and the guard on the guard: the harness can observe a race at all.

    The TOTP branch already closes this with `SecondFactorStore.touch`. If this
    test ever showed both halves succeeding, `_SuspendingStore` would have
    stopped interleaving and the recovery attack above would be proving nothing.
    """
    store, secret, _codes, at = await _enrolled_store()
    live = totp_code(secret, totp_counter(at + 30))

    first, second = await asyncio.gather(
        verify_second_factor(store, "user-1", live, at=at + 30),
        verify_second_factor(store, "user-1", live, at=at + 30),
    )

    assert [first is not None, second is not None].count(True) == 1


# --- 3. the pure JWT build accepts what the native one refuses ---------------
#
# Driven in a subprocess, twice, because `WREATH_PURE=1` is read at import and
# is the *real* second wiring rather than a monkeypatched approximation of it.
# The probe `raise`s rather than asserting, so `python -O` cannot empty it.

_PROBE = r"""
import base64, hmac, json, sys
from wreath import Wreath
from wreath.auth import BearerTokenBackend, JwtVerifier, SymmetricKey, authenticated
from wreath.testing import TestClient
from wreath._auth import jwt as _jwt

SECRET = b"k" * 32


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint(claims):
    header = b64(json.dumps({"alg": "HS256"}).encode())
    payload = b64(json.dumps(claims).encode())
    signing = (header + "." + payload).encode("ascii")
    return header + "." + payload + "." + b64(hmac.new(SECRET, signing, "sha256").digest())


def build(**options):
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(
            JwtVerifier(
                algorithms=["HS256"], key=SymmetricKey(SECRET), required=(), **options
            )
        )
    )

    @app.get("/vault")
    @authenticated()
    async def vault(request):
        return {"sub": request.identity.id}

    return app


async def status(client, token):
    # The status a server would send. `TestClient` re-raises what the
    # application let out rather than rendering it, so an unhandled error has
    # to be spelled here as the 500 it becomes on any real server.
    try:
        response = await client.get(
            "/vault", headers={"authorization": "Bearer " + token}
        )
    except Exception as error:
        return "500:" + type(error).__name__
    return response.status


async def main():
    out = {"native": _jwt._native_parse is not None}
    async with TestClient(build()) as client:
        good = mint({"sub": "ada", "exp": 2**40})
        out["plain"] = await status(client, good)
        out["padded"] = await status(client, good + "=")
    # The audience is only compared when one was configured, so the nested-aud
    # probe needs a verifier that asks for one.
    async with TestClient(build(audience="api")) as client:
        out["plain_aud"] = await status(
            client, mint({"sub": "ada", "exp": 2**40, "aud": "api"})
        )
        out["nested_aud"] = await status(
            client, mint({"sub": "ada", "exp": 2**40, "aud": [["api"]]})
        )
    print(json.dumps(out))


import asyncio

asyncio.run(main())
"""


def _probe(pure: bool) -> dict[str, Any]:
    environment = dict(os.environ)
    if pure:
        environment["WREATH_PURE"] = "1"
    else:
        environment.pop("WREATH_PURE", None)
    root = Path(__file__).resolve().parent.parent
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=environment,
        cwd=root,
        timeout=300,
    )
    if completed.returncode != 0:
        raise AssertionError(f"probe failed:\n{completed.stdout}\n{completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def probes() -> dict[str, dict[str, Any]]:
    return {"native": _probe(pure=False), "pure": _probe(pure=True)}


def test_the_probes_still_have_something_to_check(probes: dict[str, Any]) -> None:
    """Guard on the guard: the two runs really are the two builds.

    A `WREATH_PURE=1` that stopped selecting the stdlib twins, or a build with
    no `_core` at all, would make every comparison below a tautology over one
    implementation -- green, and examining nothing (docs/decisions/0024).
    """
    if probes["native"]["native"] is not True:
        raise AssertionError("the native probe did not resolve the native jose parser")
    if probes["pure"]["native"] is not False:
        raise AssertionError("WREATH_PURE=1 did not select the pure jose fallback")
    # Control: a well-formed token authenticates in both builds, with and
    # without an audience configured.
    if (probes["native"]["plain"], probes["pure"]["plain"]) != (200, 200):
        raise AssertionError(f"a valid token was refused: {probes}")
    if (probes["native"]["plain_aud"], probes["pure"]["plain_aud"]) != (200, 200):
        raise AssertionError(f"a valid audience was refused: {probes}")


def test_base64_padding_glued_to_a_token_does_not_authenticate(
    probes: dict[str, Any],
) -> None:
    """The attack: `<token>=` is a second string that verifies as the token.

    RFC 7515 writes compact-serialization segments as base64url *without*
    padding, and `jose.c` refuses the `=`. The stdlib fallback re-pads before
    decoding, so under `WREATH_PURE=1` an unbounded family of strings
    authenticates as one token -- which quietly defeats any deployment that
    blocklists a leaked token by its value.
    """
    if probes["pure"]["padded"] != 401:
        raise AssertionError(
            f"the pure build authenticated a padded token: {probes['pure']}"
        )
    if probes["native"]["padded"] != 401:
        raise AssertionError(
            f"the native build authenticated a padded token: {probes['native']}"
        )


def test_a_nested_audience_is_a_refusal_not_a_server_error(
    probes: dict[str, Any],
) -> None:
    """The attack: `aud: [["api"]]` takes the authentication path to a 500.

    `verify_jwt` documents that it "Returns None (never raises) for every
    authentication failure". The stdlib claim validator builds `set(aud)`, which
    raises `TypeError` on an unhashable member, and the raise leaves
    `verify_jwt` altogether -- so a token that should have been refused becomes
    a server error instead.
    """
    for build, result in probes.items():
        if result["nested_aud"] != 401:
            raise AssertionError(
                f"the {build} build answered {result['nested_aud']} to a nested aud"
            )


# --- 4. an authentication backend that raises --------------------------------
#
# The third "the control holds on one supported wiring and not another" of the
# day. On a classifying table (`bitset`, the default, and `decision`)
# authentication runs early, before the route is resolved, and used to run
# outside every error boundary the rest of the request has. On `trie`, and on
# the lazy `identify()` path in every mode, the same backend answered 500.
#
# A backend is documented to refuse with None rather than raise, so a raising
# one is misuse -- but escaping the application is not an acceptable response to
# misuse, and it is reachable without any application mistake at all:
# `OidcProvider.bearer_verifier()` awaits a JWKS fetch inside `authenticate`, so
# an identity provider that is merely unreachable raises straight through here.

_ROUTING_MODES = ("bitset", "trie", "decision")


class _Egress:
    """A global middleware that records what the caller was actually sent.

    The audit half of the defect: an exception that escapes `__call__` skips
    every global `after` hook, so security headers, access logging, rate-limit
    accounting and the flight recorder's finish are all skipped for exactly the
    requests that failed inside authentication.
    """

    global_scope = True

    def __init__(self) -> None:
        self.statuses: list[Any] = []

    async def before(self, request: Any) -> None:
        return None

    async def after(self, request: Any, response: Any) -> Any:
        self.statuses.append(getattr(response, "status", None))
        return response


def _exploding_verifier(token: str) -> Any:
    """A `BearerTokenBackend` verifier that raises on one input.

    Application code, which is what a verifier is: a database lookup, a cache
    read, or the JWKS fetch `OidcProvider` does. `verify_jwt` is careful never
    to raise; nothing makes a hand-written one careful.
    """
    if token == "boom":
        raise RuntimeError("the verifier could not reach its key source")
    return None


def _raising_backend_app(routing: str) -> tuple[Wreath, _Egress]:
    from wreath.auth import BearerTokenBackend, authenticated, identify

    app = Wreath(routing=routing)
    egress = _Egress()
    app.add_global_middleware(egress)
    app.configure_auth(BearerTokenBackend(_exploding_verifier))

    @app.get("/vault")
    @authenticated()
    async def vault(request: Any) -> dict[str, Any]:
        return {"ok": True}

    @app.get("/who")
    @identify()
    async def who(request: Any) -> dict[str, Any]:
        return {"ok": True}

    return app, egress


@pytest.mark.parametrize("routing", _ROUTING_MODES)
async def test_a_backend_that_raises_on_a_protected_route_is_a_500(routing: str) -> None:
    """The attack: make the backend raise and watch the app answer nothing.

    Parametrised over every routing mode rather than pinned to the broken one,
    because the defect *is* the disagreement between them -- a test that only
    drove `bitset` would go green again the moment the early path moved.
    """
    app, egress = _raising_backend_app(routing)
    async with TestClient(app) as client:
        refused = await client.get(
            "/vault", headers={"authorization": "Bearer boom"}
        )

        assert refused.status == 500
        # And the egress the request was owed still ran.
        assert egress.statuses == [500]


@pytest.mark.parametrize("routing", _ROUTING_MODES)
async def test_a_backend_that_raises_on_a_public_identify_route_is_a_500(
    routing: str,
) -> None:
    """Control: the lazy path was already correct and must stay so.

    Passes in both trees, in all three modes -- so a run where everything is
    red is distinguishable from one that caught the protected-route hole.
    """
    app, egress = _raising_backend_app(routing)
    async with TestClient(app) as client:
        refused = await client.get("/who", headers={"authorization": "Bearer boom"})

        assert refused.status == 500
        assert egress.statuses == [500]


@pytest.mark.parametrize("routing", _ROUTING_MODES)
async def test_a_backend_that_refuses_still_answers_401(routing: str) -> None:
    """Control: the ordinary refusal is untouched, in every mode.

    Passes in both trees. A fix that turned every anonymous call into a 500
    would satisfy the attack above and break the framework.
    """
    app, egress = _raising_backend_app(routing)
    async with TestClient(app) as client:
        refused = await client.get("/vault", headers={"authorization": "Bearer nope"})

        assert refused.status == 401
        assert egress.statuses == [401]


async def test_a_websocket_backend_that_raises_closes_rather_than_escaping() -> None:
    """The same class on the third wiring: the handshake's authorization step.

    Three lines above it, the WebSocket ingress hooks catch `Exception` and
    close with 1008 because security ingress fails closed. The authorization
    call beside them caught only `HTTPException`, so anything else escaped the
    application with the handshake neither accepted nor closed.
    """
    from wreath.auth import BearerTokenBackend
    from wreath.auth import authenticated as authenticated_decorator

    app = Wreath()
    app.configure_auth(BearerTokenBackend(_exploding_verifier))

    @app.websocket("/feed")
    @authenticated_decorator()
    async def feed(socket: Any) -> None:  # pragma: no cover - never reached
        await socket.accept()

    async with TestClient(app) as client:
        # Bounded, because the failure mode is a *hang*: the application task
        # dies before sending anything, so the handshake is neither accepted nor
        # closed and the peer waits for a frame that will never come. An
        # unbounded `async with` here would take the suite with it.
        with pytest.raises(ConnectionError) as caught:
            async with asyncio.timeout(5):
                async with client.websocket(
                    "/feed", headers={"authorization": "Bearer boom"}
                ):
                    pass

    # 1008 is what the ingress hooks three lines above already close with.
    assert "1008" in str(caught.value)


async def test_a_websocket_backend_that_refuses_still_closes_the_handshake() -> None:
    """Control: an ordinary anonymous WebSocket is still refused, and promptly.

    Passes in both trees. It is also the guard on the test above: if the client
    stopped distinguishing a close from a hang, this would stop passing too.
    """
    from wreath.auth import BearerTokenBackend
    from wreath.auth import authenticated as authenticated_decorator

    app = Wreath()
    app.configure_auth(BearerTokenBackend(_exploding_verifier))

    @app.websocket("/feed")
    @authenticated_decorator()
    async def feed(socket: Any) -> None:  # pragma: no cover - never reached
        await socket.accept()

    async with TestClient(app) as client:
        with pytest.raises(ConnectionError) as caught:
            async with asyncio.timeout(5):
                async with client.websocket(
                    "/feed", headers={"authorization": "Bearer nope"}
                ):
                    pass

    assert "1008" in str(caught.value)


# --- 5. the shipped `identify()` example ------------------------------------


def _fenced_python(docstring: str | None) -> str:
    """The one ```python block in `docstring`, dedented and ready to exec.

    Raises rather than returning empty, because a docstring that stopped
    carrying an example would otherwise turn the test below into a check with
    nothing to check.
    """
    from textwrap import dedent

    if docstring is None:
        raise AssertionError("the docstring is missing entirely")
    blocks = docstring.split("```python")
    if len(blocks) != 2:
        raise AssertionError(f"expected exactly one ```python block, got {len(blocks) - 1}")
    body, fence, _rest = blocks[1].partition("```")
    if not fence:
        raise AssertionError("the ```python block is never closed")
    return dedent(body)


async def test_the_identify_docstring_example_runs_as_written() -> None:
    """The example in `identify()`'s own docstring is compiled and driven.

    It is rendered on `docs/reference/auth.md` as runnable, and it read
    `identity.subject`. `Identity` carries `id`, `type`, `roles`, `permissions`
    and `claims`, and nothing called `subject` -- so a reader who copied it got
    an `AttributeError`, a 500 on the one route whose whole job is to answer
    "who is this, if anyone?".

    The example is **read out of `identify.__doc__` and executed**, not
    transcribed into this file. A transcription would go green the moment it was
    written and stay green however the docstring drifted, which is the shape
    docs/decisions/0024 is about: it would be testing this test.
    """
    from wreath.auth import BearerTokenBackend, Identity, identify
    from wreath.request import Request

    source = _fenced_python(identify.__doc__)
    assert "request.identity" in source  # the example really is the one meant

    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: Identity(id="ada") if token == "ok" else None)
    )
    namespace: dict[str, Any] = {"app": app, "identify": identify, "Request": Request}
    exec(compile(source, "<identify docstring>", "exec"), namespace)  # noqa: S102

    async with TestClient(app) as client:
        known = await client.get("/session", headers={"authorization": "Bearer ok"})
        assert known.status == 200, known.json()
        assert known.json() == {"signed_in": True, "subject": "ada"}
        # Control: the anonymous half of the same example, which never broke.
        assert (await client.get("/session")).json() == {"signed_in": False}
