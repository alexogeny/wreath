from __future__ import annotations

import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import pytest
from _pgfidelity import check_for

from wreath import Wreath
from wreath._secondfactor import (
    CHALLENGE_ENROLMENT,
    MAX_SKEW,
    MemoryChallengeStore,
    SecondFactor,
    base32_to_secret,
    begin_totp_enrolment,
    confirm_totp_enrolment,
    discoverable_credential_id,
    hash_recovery_code,
    remove_second_factor,
    secret_to_base32,
    totp_code,
    totp_counter,
    verify_recovery_code,
    verify_second_factor,
    verify_totp,
)
from wreath.auth import Identity, SessionIdentityBackend
from wreath.binding import Body
from wreath.orm.registry import Registry
from wreath.policy import HttpPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.testing import TestClient
from wreath.users import (
    InMemorySecondFactorStore,
    InMemoryUserStore,
    hash_password,
    second_factor_router,
    totp_uri,
    user_router,
)

# RFC 6238 appendix B fixes the shared secret for SHA-1 as the ASCII string
# "12345678901234567890" -- twenty bytes, which is also the minimum this module
# will mint.
RFC_SECRET = b"12345678901234567890"

#: The published table, verbatim: (Unix time, expected 8-digit value). These are
#: the difference between an implementation that agrees with itself and one that
#: is TOTP -- a wrong truncation offset or a little-endian counter reproduces
#: consistently and interoperates with nothing.
RFC6238_SHA1_VECTORS = (
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
)

SECRET = b"a-twenty-byte-secret"
PASSWORD = "correct horse battery staple"

#: One scrypt, not one per seeded user. `hash_password` is deliberately slow --
#: 60 ms measured, and it is the same `PASSWORD` every time -- so re-deriving it
#: per test spent 1.3s of this file on a value that never varies. What the tests
#: exercise is `verify_password` on the login path, which still runs for real
#: against this hash; only the setup is memoised.
#:
#: Sharing one salt across seeded users is safe *here* because nothing in this
#: file reads a stored hash. A test that asserted two users hash differently
#: would have to call `hash_password` itself, which is what it is asserting about.
PASSWORD_HASH = hash_password(PASSWORD)


def _at(moment: float) -> str:
    """The 6-digit code a phone would show at `moment`."""
    return totp_code(SECRET, totp_counter(moment))


def _factor(**overrides: Any) -> SecondFactor:
    fields: dict[str, Any] = {
        "id": "cred-1",
        "user_id": "user-1",
        "kind": "totp",
        "label": "Phone",
        "created_at": datetime.now(UTC),
        "last_used_at": None,
        "material": SECRET,
        "counter": 0,
    }
    fields.update(overrides)
    return SecondFactor(**fields)


@pytest.mark.parametrize(("moment", "expected"), RFC6238_SHA1_VECTORS)
def test_rfc6238_sha1_test_vectors(moment: int, expected: str) -> None:
    assert totp_code(RFC_SECRET, totp_counter(moment), digits=8) == expected


def test_rfc6238_vectors_verify_as_well_as_generate() -> None:
    for moment, expected in RFC6238_SHA1_VECTORS:
        matched = verify_totp(RFC_SECRET, expected, at=moment, digits=8, skew=0)
        assert matched == totp_counter(moment)


async def test_a_code_that_verified_once_never_verifies_again() -> None:
    store = InMemorySecondFactorStore()
    await store.add(_factor())
    moment = 1_700_000_000.0
    code = _at(moment)

    first = await verify_second_factor(store, "user-1", code, at=moment)
    assert first is not None

    # Same code, same step, still inside its thirty seconds.
    assert await verify_second_factor(store, "user-1", code, at=moment) is None
    assert await verify_second_factor(store, "user-1", code, at=moment + 5) is None


async def test_verification_records_the_supplied_time() -> None:
    store = InMemorySecondFactorStore()
    await store.add(_factor())
    moment = 1_700_000_000.0

    matched = await verify_second_factor(store, "user-1", _at(moment), at=moment)

    assert matched is not None
    assert matched.last_used_at == datetime.fromtimestamp(moment, UTC)


async def test_a_recovery_row_cannot_answer_as_totp() -> None:
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    await store.add(_factor(id="rec-1", kind="recovery", material=SECRET))

    assert await verify_second_factor(store, "user-1", _at(moment), at=moment) is None


async def test_replay_survives_the_skew_window_of_a_later_step() -> None:
    store = InMemorySecondFactorStore()
    await store.add(_factor())
    moment = 1_700_000_000.0
    code = _at(moment)
    assert await verify_second_factor(store, "user-1", code, at=moment) is not None
    assert await verify_second_factor(store, "user-1", code, at=moment + 30) is None


def test_verify_totp_requires_a_strictly_greater_counter() -> None:
    moment = 1_700_000_000.0
    counter = totp_counter(moment)
    code = totp_code(SECRET, counter)
    assert verify_totp(SECRET, code, at=moment, skew=0) == counter
    assert verify_totp(SECRET, code, at=moment, skew=0, last_counter=counter) is None
    assert verify_totp(SECRET, code, at=moment, skew=0, last_counter=counter + 1) is None


async def test_the_confirming_code_cannot_also_sign_in() -> None:
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    enrolment = begin_totp_enrolment(account="a@b.co", secret=SECRET)
    code = totp_code(enrolment.secret, totp_counter(moment))
    confirmed = await confirm_totp_enrolment(
        store,
        "user-1",
        secret=enrolment.secret,
        code=code,
        at=moment,
        recovery_codes=1,
    )
    assert confirmed is not None
    assert await verify_second_factor(store, "user-1", code, at=moment) is None


async def test_a_store_refuses_to_move_a_counter_backwards() -> None:
    store = InMemorySecondFactorStore()
    await store.add(_factor(counter=100))
    assert await store.touch("cred-1", counter=99, at=datetime.now(UTC)) is False
    assert (await store.credentials("user-1"))[0].counter == 100


async def test_a_store_advance_is_conditional_and_says_whether_it_won() -> None:
    store = InMemorySecondFactorStore()
    at = datetime.now(UTC)
    await store.add(_factor(counter=100))
    assert await store.touch("cred-1", counter=101, at=at) is True
    assert await store.touch("cred-1", counter=101, at=at) is False
    assert await store.touch("cred-1", counter=100, at=at) is False
    assert (await store.credentials("user-1"))[0].counter == 101
    # A credential that is no longer there has not advanced either.
    assert await store.touch("gone", counter=999, at=at) is False


class _Interleaved:
    """A store that suspends where every real one does: at `credentials`.

    `InMemorySecondFactorStore` awaits nothing, so two verifications against it
    can never interleave and a race cannot be staged against it at all -- a test
    that "raced" it would be examining an ordering that cannot occur, which is
    the shape AGENTS.md's has-nothing-to-check rule is about. A store backed by anything at all
    suspends while it reads, and that suspension is exactly where the two
    requests meet: both read the same unspent counter, then both go on to write.

    Everything else delegates, so the compare-and-set under test is the real
    store's own.
    """

    def __init__(self, inner: Any, barrier: Any) -> None:
        self._inner = inner
        self._barrier = barrier

    async def credentials(self, user_id: str) -> Any:
        rows = await self._inner.credentials(user_id)
        await self._barrier.wait()
        return rows

    async def add(self, credential: Any) -> Any:
        return await self._inner.add(credential)

    async def remove(self, user_id: str, credential_id: str) -> None:
        await self._inner.remove(user_id, credential_id)

    async def touch(self, credential_id: str, *, counter: int, at: Any) -> bool:
        return await self._inner.touch(credential_id, counter=counter, at=at)


async def test_two_concurrent_verifications_of_one_code_admit_one() -> None:
    import asyncio

    store = InMemorySecondFactorStore()
    await store.add(_factor())
    moment = 1_700_000_000.0
    code = _at(moment)
    raced = _Interleaved(store, asyncio.Barrier(2))

    async def racer() -> Any:
        return await verify_second_factor(raced, "user-1", code, at=moment)

    outcomes = await asyncio.wait_for(asyncio.gather(racer(), racer()), timeout=5)
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert (await store.credentials("user-1"))[0].counter == totp_counter(moment)


@pytest.mark.parametrize("code", ["١٢٣٤٥٦", "²²²²²²", "12345٦"])
def test_a_non_ascii_digit_is_refused_rather_than_compared(code: str) -> None:
    assert len(code) == 6 and code.isdigit()
    assert verify_totp(SECRET, code, at=1_700_000_000.0) is None


def test_skew_accepts_exactly_one_step_either_side_by_default() -> None:
    moment = 1_700_000_000.0
    assert verify_totp(SECRET, _at(moment - 30), at=moment) is not None
    assert verify_totp(SECRET, _at(moment), at=moment) is not None
    assert verify_totp(SECRET, _at(moment + 30), at=moment) is not None


def test_skew_rejects_two_steps_either_side_by_default() -> None:
    moment = 1_700_000_000.0
    assert verify_totp(SECRET, _at(moment - 60), at=moment) is None
    assert verify_totp(SECRET, _at(moment + 60), at=moment) is None


def test_skew_is_configurable_within_a_stop() -> None:
    moment = 1_700_000_000.0
    assert verify_totp(SECRET, _at(moment - 60), at=moment, skew=2) is not None
    assert verify_totp(SECRET, _at(moment - 60), at=moment, skew=0) is None
    with pytest.raises(ValueError):
        verify_totp(SECRET, _at(moment), at=moment, skew=-1)
    with pytest.raises(ValueError):
        verify_totp(SECRET, _at(moment), at=moment, skew=MAX_SKEW + 1)


def test_codes_are_compared_with_hmac_compare_digest(monkeypatch) -> None:
    import wreath._secondfactor as module

    seen: list[tuple[str, str]] = []
    real = module.hmac.compare_digest

    def spy(left: Any, right: Any) -> bool:
        seen.append((left, right))
        return real(left, right)

    monkeypatch.setattr(module.hmac, "compare_digest", spy)
    moment = 1_700_000_000.0
    assert verify_totp(SECRET, _at(moment), at=moment, skew=0) is not None
    assert len(seen) == 1


async def test_recovery_codes_are_compared_with_compare_digest(monkeypatch) -> None:
    import wreath._secondfactor as module

    store = InMemorySecondFactorStore()
    await store.add(
        _factor(
            id="rec-1",
            kind="recovery",
            # Stored against the normalized form: hyphens and case are how a
            # human copies one off paper, not part of the secret.
            material=hash_recovery_code("abcdefgh").encode("utf-8"),
        )
    )
    seen: list[tuple[str, str]] = []
    real = module.hmac.compare_digest

    def spy(left: Any, right: Any) -> bool:
        seen.append((left, right))
        return real(left, right)

    monkeypatch.setattr(module.hmac, "compare_digest", spy)
    assert await verify_second_factor(store, "user-1", "ABCD-EFGH ") is not None
    assert seen
    assert await verify_second_factor(store, "user-1", "abcd-efgi") is None


async def test_one_recovery_guess_is_hashed_once_for_every_current_code(monkeypatch) -> None:
    import wreath._secondfactor as module

    store = InMemorySecondFactorStore()
    await store.add_many(
        tuple(
            _factor(
                id=f"recovery-{index}",
                kind="recovery",
                label="Recovery code",
                material=hash_recovery_code(f"code-{index:012d}").encode("utf-8"),
            )
            for index in range(10)
        )
    )
    calls = 0
    real = module.hashlib.sha256

    def spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(module.hashlib, "sha256", spy)

    assert await verify_second_factor(store, "user-1", "wrong-code") is None
    assert calls == 1


async def test_a_stage_one_scrypt_recovery_code_still_verifies() -> None:
    store = InMemorySecondFactorStore()
    await store.add(
        _factor(
            id="rec-1",
            kind="recovery",
            material=hash_password("abcdefgh").encode("utf-8"),
        )
    )
    assert await verify_second_factor(store, "user-1", "abcd-efgi") is None
    assert await verify_second_factor(store, "user-1", "ABCD-EFGH ") is not None


async def test_a_legacy_recovery_hash_is_verified_off_the_event_loop(
    monkeypatch,
) -> None:
    import wreath._secondfactor as module

    store = InMemorySecondFactorStore()
    await store.add(
        _factor(
            id="rec-1",
            kind="recovery",
            material=hash_password("abcdefgh").encode("utf-8"),
        )
    )
    calls: list[Any] = []
    real = module.asyncio.to_thread

    async def observed(function, /, *args, **kwargs):
        calls.append(function)
        return await real(function, *args, **kwargs)

    monkeypatch.setattr(module.asyncio, "to_thread", observed)
    assert await verify_second_factor(store, "user-1", "ABCD-EFGH") is not None
    assert calls == [verify_recovery_code]


async def test_an_empty_code_cannot_redeem_an_empty_recovery_digest() -> None:
    store = InMemorySecondFactorStore()
    await store.add(
        _factor(
            id="rec-1",
            kind="recovery",
            material=hash_recovery_code("").encode("utf-8"),
        )
    )

    assert await verify_second_factor(store, "user-1", " --- ") is None
    assert await store.credential("rec-1") is not None


async def test_an_empty_code_does_not_schedule_legacy_hash_work(monkeypatch) -> None:
    import wreath._secondfactor as module

    store = InMemorySecondFactorStore()
    await store.add(
        _factor(
            id="rec-1",
            kind="recovery",
            material=PASSWORD_HASH.encode("utf-8"),
        )
    )
    calls = 0

    async def unexpected(*args, **kwargs):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(module.asyncio, "to_thread", unexpected)
    assert await verify_second_factor(store, "user-1", " --- ") is None
    assert calls == 0


async def test_begin_enrols_nothing() -> None:
    store = InMemorySecondFactorStore()
    enrolment = begin_totp_enrolment(account="a@b.co", issuer="Wreath")
    assert enrolment.uri.startswith("otpauth://totp/")
    assert await store.credentials("user-1") == []


async def test_a_wrong_code_at_confirm_enrols_nothing() -> None:
    store = InMemorySecondFactorStore()
    enrolment = begin_totp_enrolment(account="a@b.co", secret=SECRET)
    confirmed = await confirm_totp_enrolment(
        store,
        "user-1",
        secret=enrolment.secret,
        code="000000",
        at=1_700_000_000.0,
    )
    assert confirmed is None
    assert await store.credentials("user-1") == []


def test_begin_enrolment_keeps_a_caller_supplied_secret() -> None:
    enrolment = begin_totp_enrolment(account="a@b.co", secret=SECRET)
    assert enrolment.secret == SECRET


async def test_confirm_enrols_and_issues_recovery_codes_once() -> None:
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    enrolment = begin_totp_enrolment(account="a@b.co", secret=SECRET)
    confirmed = await confirm_totp_enrolment(
        store,
        "user-1",
        secret=enrolment.secret,
        code=totp_code(enrolment.secret, totp_counter(moment)),
        at=moment,
        recovery_codes=3,
    )
    assert confirmed is not None
    credential, codes = confirmed
    assert credential.kind == "totp"
    assert credential.created_at == datetime.fromtimestamp(moment, UTC)
    assert credential.last_used_at == datetime.fromtimestamp(moment, UTC)
    assert len(codes) == 3 and len(set(codes)) == 3
    rows = await store.credentials("user-1")
    assert sum(1 for row in rows if row.kind == "recovery") == 3


async def test_enrolling_twice_is_refused() -> None:
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    await confirm_totp_enrolment(
        store,
        "user-1",
        secret=SECRET,
        code=totp_code(SECRET, totp_counter(moment)),
        at=moment,
        recovery_codes=1,
    )
    with pytest.raises(ValueError):
        await confirm_totp_enrolment(
            store,
            "user-1",
            secret=SECRET,
            code=totp_code(SECRET, totp_counter(moment + 30)),
            at=moment + 30,
            recovery_codes=1,
        )


async def test_recovery_codes_are_hashed_never_stored_in_plaintext() -> None:
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    confirmed = await confirm_totp_enrolment(
        store,
        "user-1",
        secret=SECRET,
        code=totp_code(SECRET, totp_counter(moment)),
        at=moment,
        recovery_codes=2,
    )
    assert confirmed is not None
    _, codes = confirmed
    stored = [row.material for row in await store.credentials("user-1") if row.kind == "recovery"]
    for material in stored:
        assert material.decode("utf-8").startswith("sha256$")
    for code in codes:
        packed = code.replace("-", "").encode("utf-8")
        assert all(packed not in material for material in stored)
        assert all(code.encode("utf-8") not in material for material in stored)


async def test_a_recovery_code_works_once_and_then_never() -> None:
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    confirmed = await confirm_totp_enrolment(
        store,
        "user-1",
        secret=SECRET,
        code=totp_code(SECRET, totp_counter(moment)),
        at=moment,
        recovery_codes=2,
    )
    assert confirmed is not None
    _, codes = confirmed

    first = await verify_second_factor(store, "user-1", codes[0], at=moment + 60)
    assert first is not None and first.kind == "recovery"
    assert await verify_second_factor(store, "user-1", codes[0], at=moment + 90) is None
    # The other code is untouched -- redeeming one must not burn the set.
    assert await verify_second_factor(store, "user-1", codes[1], at=moment + 90)


async def test_a_recovery_code_is_scoped_to_its_own_user() -> None:
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    confirmed = await confirm_totp_enrolment(
        store,
        "user-1",
        secret=SECRET,
        code=totp_code(SECRET, totp_counter(moment)),
        at=moment,
        recovery_codes=1,
    )
    assert confirmed is not None
    _, codes = confirmed
    assert await verify_second_factor(store, "user-2", codes[0], at=moment) is None


def test_a_credential_repr_does_not_carry_its_material() -> None:
    text = repr(_factor(material=b"the-shared-secret-value"))
    assert "the-shared-secret-value" not in text
    assert "cred-1" in text


def test_an_enrolment_repr_carries_neither_secret_nor_uri() -> None:
    enrolment = begin_totp_enrolment(account="a@b.co", secret=SECRET)
    text = repr(enrolment)
    assert secret_to_base32(SECRET) not in text
    assert "otpauth" not in text


def test_totp_uri_is_the_de_facto_format() -> None:
    uri = totp_uri(SECRET, account="ann@example.test", issuer="Wreath", digits=6)
    assert uri.startswith("otpauth://totp/Wreath%3Aann%40example.test?")
    assert f"secret={secret_to_base32(SECRET)}" in uri
    assert "issuer=Wreath" in uri and "period=30" in uri and "digits=6" in uri
    with pytest.raises(ValueError):
        totp_uri(SECRET, account="a:b", issuer="Wreath")


def test_a_totp_uri_without_an_issuer_has_a_bare_account_label() -> None:
    uri = totp_uri(SECRET, account="ann@example.test")
    assert uri.startswith("otpauth://totp/ann%40example.test?")
    assert "issuer=" not in uri


def test_an_empty_recovery_code_is_not_a_candidate() -> None:
    assert verify_recovery_code(" --- ", hash_recovery_code("")) is False


def test_a_discoverable_credential_id_cannot_be_derived_from_empty_material() -> None:
    with pytest.raises(ValueError, match="non-empty id"):
        discoverable_credential_id(b"")


async def test_confirm_totp_uses_the_current_time_when_none_is_supplied() -> None:
    store = InMemorySecondFactorStore()
    moment = time.time()
    code = totp_code(SECRET, totp_counter(moment))
    confirmed = await confirm_totp_enrolment(
        store,
        "user-1",
        secret=SECRET,
        code=code,
        recovery_codes=0,
    )
    assert confirmed is not None
    credential, _ = confirmed
    assert abs(credential.created_at.timestamp() - moment) < 5


@pytest.mark.parametrize(
    "module_name",
    # Every module a second factor's refusals live in, not just the one this
    # file is named after: `_webauthn` holds the ceremony and wire-format
    # checks, `users` holds the router's, and an `assert` in either would vanish
    # under `-O` exactly as one here would. Scoping this to `_secondfactor`
    # alone was a check that excluded most of what it claimed to cover.
    ["wreath._secondfactor", "wreath._webauthn", "wreath.users"],
)
def test_the_second_factor_modules_contain_no_assert_statements(module_name: str) -> None:
    import ast
    import importlib

    module = importlib.import_module(module_name)
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert [node for node in ast.walk(tree) if isinstance(node, ast.Assert)] == []


class _Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _cookie(response: Any) -> str:
    value = response.header("set-cookie")
    assert value is not None
    return value.split(";", 1)[0]


async def _seed(users: InMemoryUserStore, email: str = "ann@example.test") -> Any:
    return await users.create(email, PASSWORD_HASH)


def _app(
    users: InMemoryUserStore,
    factors: InMemorySecondFactorStore,
    clock: _Clock,
    **options: Any,
) -> Wreath:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
    app.include_router(user_router(users, secret="u" * 32, second_factors=factors, clock=clock))
    # No `pytest.warns` wrapper: building without `enrolments=` no longer warns,
    # because it no longer degrades. See `test_users_webauthn.py`.
    router = second_factor_router(users, factors, issuer="Wreath", clock=clock, **options)
    app.include_router(router)

    @app.get("/session")
    async def show(request: Any) -> dict[str, Any]:
        return dict(request.state.session)

    @app.post("/adopt/{user_id}")
    async def adopt(request: Any, user_id: str) -> dict[str, Any]:
        """Sign somebody in the way an application that is not `user_router` does.

        `wreath._auth.oauth2` writes the principal onto the session itself and
        knows nothing about a half-finished enrolment left on it. `user_router`
        clears one at both ends of a session, so this is the remaining path on
        which a begun enrolment can meet a session that has changed hands -- and
        therefore the only one on which the enrolment's user binding is what
        refuses it, rather than the absence of anything to refuse.
        """
        request.state.session["principal"] = {"sub": user_id, "type": "User", "roles": []}
        return {"status": "adopted"}

    return app


async def _login(client: Any, email: str = "ann@example.test", cookie: str = "") -> Any:
    """Sign in, optionally *on an existing session*.

    Passing the cookie is what makes a session change hands rather than a fresh
    one being minted -- `TestClient` keeps no cookie jar, so a call without it
    signs in on an empty session and exercises none of what a carried-over one
    would.
    """
    headers = {"cookie": cookie} if cookie else {}
    return await client.post(
        "/users/login", json={"email": email, "password": PASSWORD}, headers=headers
    )


async def test_a_user_with_no_factors_still_logs_straight_in() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        response = await _login(client)
        assert response.status == 200
        assert response.json()["email"] == "ann@example.test"


async def _enrol(client: Any, clock: _Clock, cookie: str) -> tuple[str, list[str], str]:
    """Run the two-phase enrolment; returns (secret_b32, recovery codes, cookie)."""
    begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
    assert begun.status == 200
    cookie = _cookie(begun) or cookie
    secret_b32 = begun.json()["secret"]
    from wreath._secondfactor import base32_to_secret

    code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
    confirmed = await client.post(
        "/auth/2fa/totp/confirm", json={"code": code}, headers={"cookie": cookie}
    )
    assert confirmed.status == 200, confirmed.json()
    return secret_b32, confirmed.json()["recovery_codes"], _cookie(confirmed) or cookie


async def test_login_leaves_a_pending_session_that_is_not_an_identity() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _app(users, factors, clock)
    async with TestClient(app) as client:
        user = await _seed(users)
        first = await _login(client)
        _, _, cookie = await _enrol(client, clock, _cookie(first))
        await client.post("/users/logout", headers={"cookie": cookie})

        clock.now += 60
        response = await _login(client)
        assert response.status == 200
        assert response.json() == {
            "status": "second_factor_required",
            "methods": ["recovery", "totp"],
        }
        assert "email" not in response.json()
        pending = _cookie(response)
        session = (await client.get("/session", headers={"cookie": pending})).json()
        assert "principal" not in session
        assert session["pending_second_factor"]["sub"] == user.id


async def test_the_identity_backend_refuses_a_pending_principal() -> None:
    backend = SessionIdentityBackend()

    class _Request:
        class state:
            session = {"principal": {"sub": "user-1", "pending": True}}

    assert await backend.authenticate(_Request()) is None
    _Request.state.session = {"principal": {"sub": "user-1"}}
    identity = await backend.authenticate(_Request())
    assert isinstance(identity, Identity) and identity.id == "user-1"


async def test_verifying_promotes_the_session_and_rotates_its_cookie() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    from wreath._secondfactor import base32_to_secret

    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        first = await _login(client)
        secret_b32, _, cookie = await _enrol(client, clock, _cookie(first))
        await client.post("/users/logout", headers={"cookie": cookie})

        clock.now += 60
        pending = _cookie(await _login(client))
        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        promoted = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": pending}
        )
        assert promoted.status == 200
        assert promoted.json()["email"] == "ann@example.test"
        rotated = _cookie(promoted)
        assert rotated != pending
        session = (await client.get("/session", headers={"cookie": rotated})).json()
        assert session["principal"]["sub"] == users._by_email["ann@example.test"]
        assert "pending_second_factor" not in session


async def test_verify_without_a_pending_login_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        response = await client.post("/auth/2fa/verify", json={"code": "000000"})
        assert response.status == 401
        assert response.json() == {"error": "no_pending_second_factor"}


async def test_a_pending_login_expires() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    from wreath._secondfactor import base32_to_secret

    async with TestClient(_app(users, factors, clock, pending_ttl=120.0)) as client:
        await _seed(users)
        first = await _login(client)
        secret_b32, _, cookie = await _enrol(client, clock, _cookie(first))
        await client.post("/users/logout", headers={"cookie": cookie})
        clock.now += 60
        pending = _cookie(await _login(client))

        clock.now += 121
        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        response = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": pending}
        )
        assert response.status == 401
        assert response.json() == {"error": "second_factor_expired"}


async def test_verification_is_throttled_per_user() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    from wreath._secondfactor import base32_to_secret

    app = _app(users, factors, clock, max_verify_attempts=3, verify_window=300.0)
    async with TestClient(app) as client:
        await _seed(users)
        first = await _login(client)
        secret_b32, _, cookie = await _enrol(client, clock, _cookie(first))
        await client.post("/users/logout", headers={"cookie": cookie})
        clock.now += 60
        pending = _cookie(await _login(client))

        for _ in range(3):
            wrong = await client.post(
                "/auth/2fa/verify", json={"code": "000000"}, headers={"cookie": pending}
            )
            assert wrong.status == 401
        refused = await client.post(
            "/auth/2fa/verify", json={"code": "000000"}, headers={"cookie": pending}
        )
        assert refused.status == 429
        assert refused.header("retry-after") == "300"
        # And the *correct* code is refused too while the budget is spent --
        # otherwise the throttle is only a speed bump on the guessing loop.
        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        still = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": pending}
        )
        assert still.status == 429


async def test_a_non_ascii_code_is_a_refusal_over_http_not_a_500() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        first = await _login(client)
        _, _, cookie = await _enrol(client, clock, _cookie(first))
        await client.post("/users/logout", headers={"cookie": cookie})
        clock.now += 60
        pending = _cookie(await _login(client))
        response = await client.post(
            "/auth/2fa/verify", json={"code": "١٢٣٤٥٦"}, headers={"cookie": pending}
        )
        assert response.status == 401
        assert response.json() == {"error": "invalid_code"}


async def test_enrolment_confirmation_is_throttled_too() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _app(users, factors, clock, max_verify_attempts=2, verify_window=300.0)
    async with TestClient(app) as client:
        await _seed(users)
        cookie = _cookie(await _login(client))
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        cookie = _cookie(begun) or cookie
        for _ in range(2):
            wrong = await client.post(
                "/auth/2fa/totp/confirm",
                json={"code": "000000"},
                headers={"cookie": cookie},
            )
            assert wrong.status == 400
        refused = await client.post(
            "/auth/2fa/totp/confirm", json={"code": "000000"}, headers={"cookie": cookie}
        )
        assert refused.status == 429


async def test_begin_requires_an_authenticated_session() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        response = await client.post("/auth/2fa/totp/begin")
        assert response.status == 401


async def test_an_unconfirmed_enrolment_expires_without_activating() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    from wreath._secondfactor import base32_to_secret

    async with TestClient(_app(users, factors, clock, enrolment_ttl=300.0)) as client:
        user = await _seed(users)
        cookie = _cookie(await _login(client))
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        cookie = _cookie(begun) or cookie
        secret = base32_to_secret(begun.json()["secret"])

        clock.now += 301
        late = await client.post(
            "/auth/2fa/totp/confirm",
            json={"code": totp_code(secret, totp_counter(clock.now))},
            headers={"cookie": cookie},
        )
        assert late.status == 400
        assert late.json() == {"error": "enrolment_expired"}
        assert await factors.credentials(user.id) == []


async def test_listing_factors_renders_no_material() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        first = await _login(client)
        secret_b32, codes, cookie = await _enrol(client, clock, _cookie(first))
        listed = await client.get("/auth/2fa", headers={"cookie": cookie})
        assert listed.status == 200
        body = listed.json()
        assert [row["kind"] for row in body["factors"]] == ["totp"]
        assert body["recovery_codes_remaining"] == 10
        text = listed.text if hasattr(listed, "text") else str(body)
        assert secret_b32 not in text
        assert all(code not in text for code in codes)


async def test_the_router_mounts_the_stage_one_routes() -> None:
    users, factors = InMemoryUserStore(), InMemorySecondFactorStore()
    router = second_factor_router(users, factors)
    routes = {(route.path, method) for route in router.routes for method in route.methods}
    assert ("/auth/2fa/totp/begin", "POST") in routes
    assert ("/auth/2fa/totp/confirm", "POST") in routes
    assert ("/auth/2fa/verify", "POST") in routes
    assert ("/auth/2fa", "GET") in routes


def test_the_router_refuses_impossible_parameters() -> None:
    users, factors = InMemoryUserStore(), InMemorySecondFactorStore()
    with pytest.raises(ValueError):
        second_factor_router(users, factors, skew=MAX_SKEW + 1)
    with pytest.raises(ValueError):
        second_factor_router(users, factors, period=0)
    with pytest.raises(ValueError):
        second_factor_router(users, factors, digits=4)


class _RawStatement:
    """What `session.raw(...)` hands back: something with `fetchval`."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def fetchval(self) -> Any:
        return self._session.raw_result


class _FakeSession:
    """A unit-of-work twin: `add`/`delete` stage, `flush` counts.

    **It does not execute SQL, and does not pretend to.** `raw` records the
    statement and answers with whatever `raw_result` is set to, so the tests
    below can assert what `touch` *sends* and how it reads the answer. Whether
    PostgreSQL then admits one racer out of two is a question no fake can
    answer, and it is asked of a real server in the `database`-marked test at
    the end of this file -- a double that decided races itself would be exactly
    the more-capable-than-the-real-thing fake AGENTS.md warns about.
    """

    def __init__(self, model: Any = None) -> None:
        self.rows: list[Any] = []
        self.fetches = 0
        self.flushes = 0
        self.deleted: list[Any] = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.raw_result: Any = 1
        self.registry = Registry(_FakeDatabase(), [model], validate_schema="off")

    def add(self, instance: Any) -> None:
        self.rows.append(instance)

    def delete(self, instance: Any) -> None:
        self.deleted.append(instance)
        self.rows.remove(instance)

    async def flush(self) -> None:
        self.flushes += 1

    async def fetch(self, query: Any) -> list[Any]:
        self.fetches += 1
        wanted = _predicate_value(query)
        return [row for row in self.rows if row.user_id == wanted]

    async def fetch_one(self, query: Any) -> Any:
        wanted = _predicate_value(query)
        return next((row for row in self.rows if row.id == wanted), None)

    def raw(self, sql: str, *args: Any) -> _RawStatement:
        check_for(self, sql, args)
        self.statements.append((sql, args))
        return _RawStatement(self)


class _FakeDatabase:
    """Only ever asked its name: nothing here opens a connection."""

    name = "second-factor-test"


def _predicate_value(query: Any) -> Any:
    """Pull the right-hand side out of the single `where` predicate."""
    right = query.predicates[0].right
    return getattr(right, "value", right)


def _orm_store() -> tuple[Any, _FakeSession, Any]:
    from wreath.users import OrmSecondFactorStore, default_second_factor_model

    model = default_second_factor_model(table=f"factors_{id(object()):x}")
    session = _FakeSession(model)
    return OrmSecondFactorStore(session, model), session, model


async def test_orm_store_round_trips_a_credential() -> None:
    store, session, _ = _orm_store()
    await store.add(_factor())
    assert session.flushes == 1
    rows = await store.credentials("user-1")
    assert len(rows) == 1
    assert rows[0].id == "cred-1" and rows[0].material == SECRET
    assert await store.credentials("user-2") == []


async def test_orm_enrolment_flushes_the_factor_and_recovery_codes_once() -> None:
    store, session, _ = _orm_store()
    moment = 1_700_000_000.0

    confirmed = await confirm_totp_enrolment(
        store,
        "user-1",
        secret=SECRET,
        code=_at(moment),
        at=moment,
    )

    assert confirmed is not None
    assert session.flushes == 1
    assert [row.kind for row in session.rows].count("totp") == 1
    assert [row.kind for row in session.rows].count("recovery") == 10


async def test_in_memory_bulk_add_refuses_a_duplicate_within_the_batch() -> None:
    store = InMemorySecondFactorStore()
    duplicate = _factor(id="duplicate")

    with pytest.raises(ValueError, match="duplicate second-factor id: 'duplicate'"):
        await store.add_many((duplicate, duplicate))

    assert await store.credentials("user-1") == []


async def test_in_memory_bulk_add_refuses_an_existing_id_before_writing() -> None:
    store = InMemorySecondFactorStore()
    existing = _factor(id="existing")
    await store.add(existing)

    with pytest.raises(ValueError, match="duplicate second-factor id: 'existing'"):
        await store.add_many((_factor(id="new"), existing))

    assert await store.credentials("user-1") == [existing]


async def test_orm_last_factor_removal_flushes_all_credentials_once() -> None:
    store, session, _ = _orm_store()
    credentials = (
        _factor(id="totp-1"),
        *(
            _factor(
                id=f"recovery-{index}",
                kind="recovery",
                label="Recovery code",
                material=b"sha256$digest",
            )
            for index in range(10)
        ),
    )
    await store.add_many(credentials)
    session.flushes = 0

    removed = await remove_second_factor(store, "user-1", "totp-1")

    assert removed is not None
    assert session.flushes == 1
    assert await store.credentials("user-1") == []


async def test_orm_empty_bulk_removal_does_no_work() -> None:
    store, session, _ = _orm_store()

    await store.remove_many("user-1", ())

    assert session.fetches == 0
    assert session.flushes == 0


async def test_orm_bulk_removal_keeps_credentials_that_were_not_named() -> None:
    store, session, _ = _orm_store()
    removed = _factor(id="removed")
    kept = _factor(id="kept", kind="webauthn")
    await store.add_many((removed, kept))
    session.flushes = 0

    await store.remove_many("user-1", ("removed",))

    assert session.flushes == 1
    assert await store.credentials("user-1") == [kept]


async def test_orm_bulk_removal_does_not_flush_a_missing_id() -> None:
    store, session, _ = _orm_store()

    await store.remove_many("user-1", ("missing",))

    assert session.fetches == 1
    assert session.flushes == 0


async def test_scalar_store_removal_remains_the_compatibility_path() -> None:
    class ScalarStore:
        def __init__(self) -> None:
            self.inner = InMemorySecondFactorStore()
            self.removals: list[str] = []

        async def credentials(self, user_id: str) -> list[SecondFactor]:
            return await self.inner.credentials(user_id)

        async def add(self, credential: SecondFactor) -> SecondFactor:
            return await self.inner.add(credential)

        async def remove(self, user_id: str, credential_id: str) -> None:
            self.removals.append(credential_id)
            await self.inner.remove(user_id, credential_id)

        async def touch(self, credential_id: str, *, counter: int, at: datetime) -> bool:
            return await self.inner.touch(credential_id, counter=counter, at=at)

    store = ScalarStore()
    await store.add(_factor(id="totp-1"))
    await store.add(
        _factor(
            id="recovery-1",
            kind="recovery",
            label="Recovery code",
            material=b"sha256$digest",
        )
    )

    await remove_second_factor(store, "user-1", "totp-1")

    assert store.removals == ["totp-1", "recovery-1"]


async def test_one_removal_does_not_expand_into_a_bulk_read() -> None:
    class RecordingStore(InMemorySecondFactorStore):
        def __init__(self) -> None:
            super().__init__()
            self.scalar_removals = 0
            self.bulk_removals = 0

        async def remove(self, user_id: str, credential_id: str) -> None:
            self.scalar_removals += 1
            await super().remove(user_id, credential_id)

        async def remove_many(self, user_id: str, credential_ids: Sequence[str]) -> None:
            self.bulk_removals += 1
            await super().remove_many(user_id, credential_ids)

    store = RecordingStore()
    await store.add(_factor(id="totp-1"))

    await remove_second_factor(store, "user-1", "totp-1")

    assert store.scalar_removals == 1
    assert store.bulk_removals == 0
    assert "user-1" not in store._by_user


async def test_orm_store_remove_is_scoped_to_the_owner() -> None:
    store, session, _ = _orm_store()
    await store.add(_factor())
    await store.remove("user-2", "cred-1")
    assert session.deleted == []
    assert len(await store.credentials("user-1")) == 1
    await store.remove("user-1", "cred-1")
    assert len(session.deleted) == 1
    assert await store.credentials("user-1") == []


async def test_orm_store_touch_sends_one_conditional_statement() -> None:
    store, session, model = _orm_store()
    await store.add(_factor(counter=100))
    session.statements.clear()
    at = datetime.now(UTC)

    assert await store.touch("cred-1", counter=101, at=at) is True
    assert len(session.statements) == 1
    sql, args = session.statements[0]
    assert sql.startswith("UPDATE ")
    assert '"counter" = $1' in sql
    assert 'AND "counter" < $1' in sql
    assert sql.endswith("RETURNING 1")
    assert args == (101, at, "cred-1")
    # The table is the model's own, not a name spelled twice.
    assert model.__name__ and f'"{session.registry.spec_for(model).table}"' in sql


async def test_orm_store_touch_reports_losing_the_advance() -> None:
    store, session, _ = _orm_store()
    await store.add(_factor(counter=100))
    session.raw_result = None
    assert await store.touch("cred-1", counter=99, at=datetime.now(UTC)) is False


async def test_orm_store_satisfies_the_protocol() -> None:
    from wreath.users import (
        BulkSecondFactorRemovalStore,
        BulkSecondFactorStore,
        SecondFactorStore,
    )

    store, _, _ = _orm_store()
    assert isinstance(store, SecondFactorStore)
    assert isinstance(InMemorySecondFactorStore(), SecondFactorStore)
    assert isinstance(store, BulkSecondFactorStore)
    assert isinstance(InMemorySecondFactorStore(), BulkSecondFactorStore)
    assert isinstance(store, BulkSecondFactorRemovalStore)
    assert isinstance(InMemorySecondFactorStore(), BulkSecondFactorRemovalStore)


# This is the one that could not be faked. The defect it covers is that two
# requests carrying the same TOTP code both verified, because the counter was
# read on one connection, checked in Python, and written back unconditionally
# on both -- and it is only a defect against a store where the read and the
# write are separated by real I/O. `InMemorySecondFactorStore` never suspends,
# so the same test against it is the check-with-nothing-to-check of
# a check with nothing to check unless the suspension is put back deliberately, which is
# what `_Interleaved` above does.

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

_FACTOR_TABLE_SQL = """
CREATE TABLE public.{table} (
    id varchar PRIMARY KEY,
    user_id varchar NOT NULL,
    kind varchar NOT NULL,
    label varchar NOT NULL,
    secret_material bytea NOT NULL,
    counter bigint NOT NULL,
    created_at timestamptz NOT NULL,
    last_used_at timestamptz
)
"""


class _OneConnectionEach:
    """The database surface `Session` needs: hand out the connections given.

    Two sessions, two connections, because one connection would serialize the
    two statements into a queue and there would be no race left to lose.
    """

    name = "second-factor-race"

    def __init__(self, connections: list[Any]) -> None:
        self._free = list(connections)

    async def acquire(self, workload: str = "read") -> Any:
        return self._free.pop()

    async def release(self, workload: str, connection: Any) -> None:
        self._free.append(connection)


@pytest.mark.database
async def test_postgres_admits_one_of_two_concurrent_verifications() -> None:
    import asyncio
    import uuid

    from wreath.orm.registry import Registry
    from wreath.orm.session import Session
    from wreath.postgres import connect
    from wreath.users import OrmSecondFactorStore, default_second_factor_model

    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for the second-factor race test")
    table = f"second_factors_{uuid.uuid4().hex}"
    first = await connect(_DSN)
    second = await connect(_DSN)
    sessions: list[Any] = []
    try:
        await first.execute(_FACTOR_TABLE_SQL.format(table=table))
        model = default_second_factor_model(table=table)
        registry = Registry(_OneConnectionEach([second, first]), [model], validate_schema="off")
        sessions = [Session(registry, "write"), Session(registry, "write")]
        stores = [OrmSecondFactorStore(session, model) for session in sessions]
        moment = 1_700_000_000.0
        code = _at(moment)
        await stores[0].add(
            SecondFactor(
                id="cred-1",
                user_id="user-1",
                kind="totp",
                label="Authenticator app",
                created_at=datetime.now(UTC),
                last_used_at=None,
                material=SECRET,
                counter=0,
            )
        )
        barrier = asyncio.Barrier(2)
        raced = [_Interleaved(store, barrier) for store in stores]
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                *(verify_second_factor(store, "user-1", code, at=moment) for store in raced)
            ),
            timeout=30,
        )
        assert sum(outcome is not None for outcome in outcomes) == 1
        rows = await stores[0].credentials("user-1")
        assert rows[0].counter == totp_counter(moment)
    finally:
        for session in sessions:
            await session.close()
        await first.execute(f"DROP TABLE IF EXISTS public.{table}")
        await first.close()
        await second.close()


class _MemorySessionStore:
    """A `wreath.session_store.SessionStore` twin: load / save / delete, no TTL clock.

    Enough to prove *where* the unconfirmed secret is, which is the property
    under test. Expiry is the router's own `enrolment_ttl`, checked against its
    injected clock, so nothing here needs to expire on its own.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def load(self, sid: str) -> dict[str, Any] | None:
        return self.rows.get(sid)

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        self.rows[sid] = dict(data)

    async def delete(self, sid: str) -> None:
        self.rows.pop(sid, None)


async def test_a_stored_enrolment_keeps_the_secret_out_of_the_session() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    store = _MemorySessionStore()
    async with TestClient(_app(users, factors, clock, enrolments=store)) as client:
        await _seed(users)
        cookie = _cookie(await _login(client))
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        cookie = _cookie(begun) or cookie
        secret_b32 = begun.json()["secret"]

        session = (await client.get("/session", headers={"cookie": cookie})).json()
        marker = session["pending_totp_enrolment"]
        assert set(marker) == {"id", "at"}
        assert secret_b32 not in str(session)
        # It is somewhere, and that somewhere is the server.
        assert store.rows[f"wreath.2fa.enrolment.{marker['id']}"]["secret"] == secret_b32


async def test_a_stored_enrolment_round_trips_and_is_then_dropped() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    store = _MemorySessionStore()
    async with TestClient(_app(users, factors, clock, enrolments=store)) as client:
        user = await _seed(users)
        cookie = _cookie(await _login(client))
        _, _, cookie = await _enrol(client, clock, cookie)
        assert any(row.kind == "totp" for row in await factors.credentials(user.id))
        # Confirmed enrolments leave nothing behind to be replayed or read.
        assert store.rows == {}


async def test_a_stored_enrolment_that_is_gone_cannot_be_confirmed() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    store = _MemorySessionStore()
    from wreath._secondfactor import base32_to_secret

    async with TestClient(_app(users, factors, clock, enrolments=store)) as client:
        user = await _seed(users)
        cookie = _cookie(await _login(client))
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        cookie = _cookie(begun) or cookie
        secret = base32_to_secret(begun.json()["secret"])
        store.rows.clear()

        late = await client.post(
            "/auth/2fa/totp/confirm",
            json={"code": totp_code(secret, totp_counter(clock.now))},
            headers={"cookie": cookie},
        )
        assert late.status == 400
        assert late.json() == {"error": "enrolment_expired"}
        assert await factors.credentials(user.id) == []


async def test_an_enrolment_is_bound_to_the_user_who_began_it() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    from wreath._secondfactor import base32_to_secret

    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        bob = await _seed(users, "bob@example.test")
        cookie = _cookie(await _login(client))
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        cookie = _cookie(begun) or cookie
        secret = base32_to_secret(begun.json()["secret"])

        # Same browser, same session contents, a different person now signed in.
        adopted = await client.post(f"/adopt/{bob.id}", headers={"cookie": cookie})
        cookie = _cookie(adopted) or cookie
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert "pending_totp_enrolment" in session  # the subject is really there
        stolen = await client.post(
            "/auth/2fa/totp/confirm",
            json={"code": totp_code(secret, totp_counter(clock.now))},
            headers={"cookie": cookie},
        )
        assert stolen.status == 400
        assert stolen.json() == {"error": "no_enrolment_in_progress"}
        assert await factors.credentials(bob.id) == []


async def test_a_begun_enrolment_does_not_survive_a_logout() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    store = _MemorySessionStore()
    async with TestClient(_app(users, factors, clock, enrolments=store)) as client:
        await _seed(users)
        cookie = _cookie(await _login(client))
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        cookie = _cookie(begun) or cookie
        assert len(store.rows) == 1

        gone = await client.post("/users/logout", headers={"cookie": cookie})
        cookie = _cookie(gone) or cookie
        assert store.rows == {}
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert "pending_totp_enrolment" not in session


async def test_a_begun_enrolment_does_not_survive_a_login() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    store = _MemorySessionStore()
    async with TestClient(_app(users, factors, clock, enrolments=store)) as client:
        await _seed(users)
        await _seed(users, "bob@example.test")
        cookie = _cookie(await _login(client))
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        cookie = _cookie(begun) or cookie
        assert len(store.rows) == 1

        cookie = _cookie(await _login(client, "bob@example.test", cookie))
        assert store.rows == {}
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert "pending_totp_enrolment" not in session


async def test_an_unconfirmed_secret_never_rides_in_the_session() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = _cookie(await _login(client))
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        cookie = _cookie(begun) or cookie
        secret = begun.json()["secret"]
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        # A handle, not the secret -- and the secret is nowhere in the session.
        assert isinstance(session["pending_totp_enrolment"]["id"], str)
        assert secret not in str(session)

        gone = await client.post("/users/logout", headers={"cookie": cookie})
        cookie = _cookie(gone) or cookie
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert "pending_totp_enrolment" not in session
        assert secret not in str(session)


def _unwired_app(
    users: InMemoryUserStore, factors: InMemorySecondFactorStore, clock: _Clock
) -> Wreath:
    """The misconfiguration: the 2FA router mounted, `second_factors=` forgotten.

    Nothing refuses this at construction, and nothing can: the two routers are
    built independently, in either order, and a process may legitimately hold
    another application whose login has no second factor at all.
    """
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
    app.include_router(user_router(users, secret="u" * 32, clock=clock))
    router = second_factor_router(users, factors, issuer="Wreath", clock=clock)
    app.include_router(router)

    @app.get("/session")
    async def show(request: Any) -> dict[str, Any]:
        return dict(request.state.session)

    return app


async def test_a_login_that_cannot_check_an_enrolled_factor_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_unwired_app(users, factors, clock)) as client:
        user = await _seed(users)
        cookie = _cookie(await _login(client))
        # Enrolled through the router that *is* wired, which is the whole point.
        _, _, cookie = await _enrol(client, clock, cookie)
        assert any(row.kind == "totp" for row in await factors.credentials(user.id))

        # Same browser, signing in again -- so the session on the way out is
        # the one this login touched rather than a fresh empty one.
        again = await _login(client, cookie=cookie)
        assert again.status == 500
        body = again.json()
        assert body["error"] == "second_factor_not_wired"
        assert "second_factors=" in body["detail"]
        # And nobody is signed in on the way past: the principal the enrolment
        # left is cleared before the refusal, and no new one is written.
        cookie = _cookie(again)
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert "principal" not in session
        assert (await client.get("/users/me", headers={"cookie": cookie})).status == 401


async def test_the_unwired_refusal_is_scoped_to_users_who_have_a_factor() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_unwired_app(users, factors, clock)) as client:
        await _seed(users)
        await _seed(users, "bob@example.test")
        cookie = _cookie(await _login(client))
        await _enrol(client, clock, cookie)

        bob = await _login(client, "bob@example.test")
        assert bob.status == 200
        assert bob.json()["email"] == "bob@example.test"


async def test_recovery_codes_alone_do_not_refuse_a_login() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_unwired_app(users, factors, clock)) as client:
        user = await _seed(users)
        await factors.add(
            SecondFactor(
                id="rec-1",
                user_id=user.id,
                kind="recovery",
                label="Recovery code",
                created_at=datetime.now(UTC),
                last_used_at=None,
                material=hash_recovery_code("abcd-efgh-jkmn-pqrs").encode("utf-8"),
            )
        )
        response = await _login(client)
        assert response.status == 200


async def test_a_login_wired_to_its_own_store_never_asks_the_registry() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = _cookie(await _login(client))
        _, _, cookie = await _enrol(client, clock, cookie)
        pending = await _login(client)
        assert pending.status == 200
        assert pending.json()["status"] == "second_factor_required"


async def test_a_second_enrolment_is_refused_over_http() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        first = await _login(client)
        _, _, cookie = await _enrol(client, clock, _cookie(first))
        again = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        assert again.status == 409


# `wreath mutant --operators guard.remove-raise` reported every one of these
# UNREACHED across all 213 tests of the second-factor suite: the refusal could
# be deleted outright and nothing would execute the line, let alone object.
# None was a defect -- each is a documented `Raises:` contract that holds -- but
# a refusal nobody has watched work is a refusal nobody knows works, which is
# the cheaper half of a mutation report to act on.


def test_a_secret_below_the_rfc_4226_floor_is_refused() -> None:
    from wreath._secondfactor import (
        MIN_SECRET_BYTES,
        base32_to_secret,
        generate_totp_secret,
    )

    assert MIN_SECRET_BYTES == 16
    with pytest.raises(ValueError, match="at least 16 bytes"):
        generate_totp_secret(MIN_SECRET_BYTES - 1)
    with pytest.raises(ValueError, match="at least 16 bytes"):
        base32_to_secret(secret_to_base32(b"\x01" * (MIN_SECRET_BYTES - 1)))
    with pytest.raises(ValueError, match="at least 16 bytes"):
        totp_code(b"\x01" * (MIN_SECRET_BYTES - 1), 1)
    # The floor itself is admitted -- the bound is `<`, not `<=`.
    assert len(generate_totp_secret(MIN_SECRET_BYTES)) == MIN_SECRET_BYTES
    assert len(base32_to_secret(secret_to_base32(b"\x01" * MIN_SECRET_BYTES))) == 16


def test_an_unsupported_totp_algorithm_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unsupported TOTP algorithm"):
        totp_code(b"\x01" * 20, 1, algorithm="md5")


def test_totp_counter_refuses_a_period_or_a_timestamp_it_cannot_count() -> None:
    with pytest.raises(ValueError, match="period must be positive"):
        totp_counter(0, period=0)
    with pytest.raises(ValueError, match="period must be positive"):
        totp_counter(0, period=-30)
    with pytest.raises(ValueError, match="precedes the Unix epoch"):
        totp_counter(-1)
    assert totp_counter(0) == 0  # the epoch itself is countable

    # And the counter `totp_code` is handed is bounded the same way, because a
    # negative step is a `struct.pack` away from a code for some other moment.
    with pytest.raises(ValueError, match="counter must not be negative"):
        totp_code(b"\x01" * 20, -1)
    assert totp_code(b"\x01" * 20, 0)


def test_a_totp_uri_refuses_a_label_it_cannot_delimit() -> None:
    secret = b"\x01" * 20
    with pytest.raises(ValueError, match="account must not be empty"):
        totp_uri(secret, account="")
    with pytest.raises(ValueError, match="must not contain"):
        totp_uri(secret, account="ada:lovelace")
    with pytest.raises(ValueError, match="must not contain"):
        totp_uri(secret, account="ada", issuer="camera:trap")


def test_recovery_code_counts_are_bounded_at_both_ends() -> None:
    from wreath._secondfactor import generate_recovery_codes

    with pytest.raises(ValueError, match="at least 1"):
        generate_recovery_codes(0)
    with pytest.raises(ValueError, match="must not exceed 50"):
        generate_recovery_codes(51)
    assert len(generate_recovery_codes(1)) == 1
    assert len(generate_recovery_codes(50)) == 50


async def test_a_store_refuses_a_duplicate_second_factor_id() -> None:
    def factor(user_id: str, material: bytes) -> SecondFactor:
        return SecondFactor(
            id="f1",
            user_id=user_id,
            kind="totp",
            label="phone",
            created_at=datetime(2026, 7, 30, tzinfo=UTC),
            last_used_at=None,
            material=material,
        )

    store = InMemorySecondFactorStore()
    await store.add(factor("u1", b"\x01" * 20))
    with pytest.raises(ValueError, match="duplicate second-factor id"):
        await store.add(factor("u2", b"\x02" * 20))


# `second_factor_router` opens every handler with the same two refusals -- no session
# at all, then no signed-in user -- and a mutation sweep reported almost all of them
# `survived`: the suite reaches them only through the happy path, where a session
# exists and somebody is signed in. Both guards were therefore deletable on nearly
# every route, and `_signed_in` returning `None` would have been read as a user.
# These are the two cheapest requests an attacker makes, so each endpoint gets both.

_RP = "example.test"

# (method, path, json body, error on an anonymous session) for every route the router
# mounts, webauthn included. The listing route is `/auth/2fa` with no trailing slash --
# `/auth/2fa/` is a 405 -- and `verify` and `webauthn/verify/begin` answer
# `no_pending_second_factor` rather than `not_authenticated`, because they run *before*
# sign-in completes and key on the pending marker instead of a signed-in user.
_SECOND_FACTOR_ROUTES = [
    ("POST", "/auth/2fa/totp/begin", None, "not_authenticated"),
    ("POST", "/auth/2fa/totp/confirm", {"code": "000000"}, "not_authenticated"),
    ("GET", "/auth/2fa", None, "not_authenticated"),
    ("POST", "/auth/2fa/verify", {"code": "000000"}, "no_pending_second_factor"),
    ("DELETE", "/auth/2fa/some-factor-id", None, "not_authenticated"),
    ("POST", "/auth/2fa/webauthn/begin", None, "not_authenticated"),
    (
        "POST",
        "/auth/2fa/webauthn/confirm",
        {"client_data": "e30", "attestation_object": "e30"},
        "not_authenticated",
    ),
    ("POST", "/auth/2fa/webauthn/verify/begin", None, "no_pending_second_factor"),
]


def _router_only_app(
    users: InMemoryUserStore,
    factors: InMemorySecondFactorStore,
    clock: _Clock,
    *,
    sessions: bool,
) -> Wreath:
    """The second-factor router mounted alone, with the session middleware optional.

    `_app` above always installs `SessionPolicy`, which is why the
    `session is None` arm had never run: with the middleware there is always a
    session, and the only way to reach that arm is an application that mounted the
    router and forgot it. That is a real deployment mistake and the router answers
    500 rather than 401, because it is the operator's fault and not the caller's.
    """
    app = Wreath()
    if sessions:
        app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
    app.include_router(
        second_factor_router(
            users,
            factors,
            issuer="Wreath",
            clock=clock,
            enrolments=None,
            rp_id=_RP,
            rp_name="Wreath",
        )
    )
    return app


async def _request(client: Any, method: str, path: str, body: Any) -> Any:
    if method == "GET":
        return await client.get(path)
    if method == "DELETE":
        return await client.delete(path)
    return await client.post(path, json=body) if body else await client.post(path)


@pytest.mark.parametrize(("method", "path", "body", "anonymous_error"), _SECOND_FACTOR_ROUTES)
async def test_a_router_mounted_without_session_middleware_refuses_every_route(
    method: str,
    path: str,
    body: Any,
    anonymous_error: str,
) -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _router_only_app(users, factors, clock, sessions=False)
    async with TestClient(app) as client:
        response = await _request(client, method, path, body)
    assert response.status == 500, (path, response.status)
    assert response.json() == {"error": "session_middleware_required"}


@pytest.mark.parametrize(("method", "path", "body", "anonymous_error"), _SECOND_FACTOR_ROUTES)
async def test_every_second_factor_route_refuses_an_anonymous_session(
    method: str,
    path: str,
    body: Any,
    anonymous_error: str,
) -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _router_only_app(users, factors, clock, sessions=True)
    async with TestClient(app) as client:
        response = await _request(client, method, path, body)
    assert response.status == 401, (path, response.status, response.json())
    assert response.json() == {"error": anonymous_error}


@pytest.mark.parametrize(
    ("principal", "signed_in_if_unguarded"),
    [
        ("not-a-dict", False),
        ({"sub": "1", "type": "User", "pending": True}, True),
        ({"sub": 1, "type": "User"}, True),
        ({"sub": ""}, False),
    ],
    ids=["not-a-mapping", "still-pending", "non-string-subject", "empty-subject"],
)
async def test_a_malformed_principal_is_not_signed_in(
    principal: Any,
    signed_in_if_unguarded: bool,
) -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    user = await _seed(users)
    assert user.id == "1", "the fixture ids changed; the principals below hard-code one"

    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
    router = second_factor_router(users, factors, issuer="Wreath", clock=clock)
    app.include_router(router)

    @app.post("/plant")
    async def plant(request: Any) -> dict[str, Any]:
        request.state.session["principal"] = principal
        return {"status": "planted"}

    async with TestClient(app) as client:
        cookie = _cookie(await client.post("/plant"))
        response = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
    assert response.status == 401, (principal, response.status, response.json())
    assert response.json() == {"error": "not_authenticated"}


async def _signed_in_client(client: Any, users: InMemoryUserStore) -> str:
    await _seed(users)
    return _cookie(await _login(client))


async def test_confirming_with_no_enrolment_in_progress_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        cookie = await _signed_in_client(client, users)
        response = await client.post(
            "/auth/2fa/totp/confirm", json={"code": "000000"}, headers={"cookie": cookie}
        )
    assert response.status == 400
    assert response.json() == {"error": "no_enrolment_in_progress"}


async def test_confirming_an_enrolment_begun_too_long_ago_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, enrolment_ttl=600.0)) as client:
        cookie = await _signed_in_client(client, users)
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        assert begun.status == 200
        cookie = _cookie(begun) or cookie

        clock.now += 601
        response = await client.post(
            "/auth/2fa/totp/confirm", json={"code": "000000"}, headers={"cookie": cookie}
        )
        assert response.status == 400
        assert response.json() == {"error": "enrolment_expired"}

        session = await client.get("/session", headers={"cookie": _cookie(response) or cookie})
        assert "pending_totp_enrolment" not in session.json()


async def test_beginning_a_second_totp_enrolment_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        cookie = await _signed_in_client(client, users)
        _, _, cookie = await _enrol(client, clock, cookie)
        response = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
    assert response.status == 409
    assert response.json() == {"error": "already_enrolled"}


# The three checks below all answer *after* `no_enrolment_in_progress` has been ruled
# out, so each needs a session carrying a marker that is malformed in one specific
# way. Nothing planted one, so every check was deletable while the suite stayed green
# -- and each of them drops the unconfirmed secret on the way out, which is the part
# that matters: a marker left behind is a secret that can still be confirmed later.


def _app_with_planting(
    users: InMemoryUserStore,
    factors: InMemorySecondFactorStore,
    clock: _Clock,
    **options: Any,
) -> Wreath:
    """`_app`, plus a route that plants a malformed enrolment.

    Planting is the only way in: the router writes well-formed markers and
    well-formed records, so neither can come from its own `begin`.

    The record no longer rides in the session -- it lives in the `ChallengeStore`
    -- so planting writes to both halves: the session gets a marker carrying the
    handle and the `at` under test, and the store gets the record under that
    handle. That split is the point of the change these tests now cover.
    """
    challenges = options.pop("challenges", None) or MemoryChallengeStore(clock=clock)
    app = _app(users, factors, clock, challenges=challenges, **options)

    @app.post("/plant-enrolment")
    async def plant(request: Any, planted: Annotated[dict[str, Any], Body()]):
        handle = "planted-handle"
        await challenges.put(
            handle,
            user_id="1",
            kind=CHALLENGE_ENROLMENT,
            payload=planted["record"],
            ttl=600.0,
        )
        request.state.session["pending_totp_enrolment"] = {
            "id": handle,
            "at": planted["at"],
        }
        return {"status": "planted"}

    return app


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (
            {"at": "recently", "record": {"secret": "JBSWY3DPEHPK3PXP", "user": "1"}},
            "enrolment_expired",
        ),
        (
            {"at": 1_700_000_000.0, "record": {"secret": 1234, "user": "1"}},
            "no_enrolment_in_progress",
        ),
        (
            {"at": 1_700_000_000.0, "record": {"secret": "not-base32-!!", "user": "1"}},
            "no_enrolment_in_progress",
        ),
    ],
    ids=["non-numeric-timestamp", "non-string-secret", "undecodable-secret"],
)
async def test_a_malformed_enrolment_marker_is_refused_and_dropped(
    marker: dict[str, Any],
    expected: str,
) -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app_with_planting(users, factors, clock)) as client:
        cookie = await _signed_in_client(client, users)
        planted = await client.post("/plant-enrolment", json=marker, headers={"cookie": cookie})
        cookie = _cookie(planted) or cookie
        response = await client.post(
            "/auth/2fa/totp/confirm", json={"code": "000000"}, headers={"cookie": cookie}
        )
        assert response.status == 400, (marker, response.json())
        assert response.json() == {"error": expected}

        session = await client.get("/session", headers={"cookie": _cookie(response) or cookie})
        assert "pending_totp_enrolment" not in session.json()


async def test_confirming_after_another_session_already_enrolled_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        cookie = await _signed_in_client(client, users)
        begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        assert begun.status == 200
        cookie = _cookie(begun) or cookie
        secret_b32 = begun.json()["secret"]

        # Somebody else finished first, on another session.
        await factors.add(_factor(user_id="1", kind="totp"))

        code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
        response = await client.post(
            "/auth/2fa/totp/confirm", json={"code": code}, headers={"cookie": cookie}
        )
    assert response.status == 409
    assert response.json() == {"error": "already_enrolled"}


# The whole endpoint had never been called. A mutation sweep reported its
# step-up guard UNREACHED, which is the worse of the two findings: not "the
# tests would not notice", but "nothing has ever watched this refusal work" --
# and it is the refusal standing between a stolen session and the removal of
# the second factor that session could not have produced.


async def _stepped_up(
    client: Any,
    users: InMemoryUserStore,
    factors: InMemorySecondFactorStore,
    clock: _Clock,
) -> str:
    """Enrol, sign back in through the second factor, and return that cookie.

    Going the long way round is the point: `second_factor_at` has to be written
    by `POST /auth/2fa/verify` rather than by the test, or the step-up guard is
    being handed the very value it exists to check.
    """
    first = await _login(client)
    secret_b32, _, cookie = await _enrol(client, clock, _cookie(first))
    await client.post("/users/logout", headers={"cookie": cookie})
    clock.now += 60
    pending = _cookie(await _login(client))
    code = totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))
    promoted = await client.post(
        "/auth/2fa/verify", json={"code": code}, headers={"cookie": pending}
    )
    assert promoted.status == 200, promoted.json()
    return _cookie(promoted) or pending


async def _only_factor_id_or_none(client: Any, cookie: str) -> str | None:
    listed = await client.get("/auth/2fa", headers={"cookie": cookie})
    assert listed.status == 200, listed.json()
    rows = listed.json()["factors"]
    assert len(rows) <= 1
    return None if not rows else str(rows[0]["id"])


async def _only_factor_id(client: Any, cookie: str) -> str:
    factor_id = await _only_factor_id_or_none(client, cookie)
    assert factor_id is not None
    return factor_id


async def test_a_recently_proved_factor_may_be_removed() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = await _stepped_up(client, users, factors, clock)
        factor_id = await _only_factor_id(client, cookie)

        response = await client.delete(f"/auth/2fa/{factor_id}", headers={"cookie": cookie})

        assert response.status == 200
        assert response.json() == {"status": "removed", "id": factor_id}
        # Gone from the store, not merely reported gone.
        assert await _only_factor_id_or_none(client, cookie) is None


async def test_removing_a_factor_without_a_recent_second_factor_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = await _stepped_up(client, users, factors, clock)
        factor_id = await _only_factor_id(client, cookie)

        adopted = await client.post(f"/adopt/{users._by_email['ann@example.test']}")
        response = await client.delete(
            f"/auth/2fa/{factor_id}", headers={"cookie": _cookie(adopted)}
        )

        assert response.status == 403
        assert response.json() == {"error": "second_factor_required"}
        # And the factor is still there.
        assert await _only_factor_id(client, cookie) == factor_id


async def test_a_second_factor_proved_too_long_ago_no_longer_authorises_removal() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, step_up_ttl=300.0)) as client:
        await _seed(users)
        cookie = await _stepped_up(client, users, factors, clock)
        factor_id = await _only_factor_id(client, cookie)

        clock.now += 301
        response = await client.delete(f"/auth/2fa/{factor_id}", headers={"cookie": cookie})

        assert response.status == 403
        assert response.json() == {"error": "second_factor_required"}


async def test_a_boolean_second_factor_stamp_does_not_authorise_removal() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _app(users, factors, clock)

    @app.post("/forge/{user_id}")
    async def forge(request: Any, user_id: str) -> dict[str, Any]:
        request.state.session["principal"] = {
            "sub": user_id,
            "type": "User",
            "roles": [],
            "second_factor_at": True,
        }
        return {"status": "forged"}

    async with TestClient(app) as client:
        await _seed(users)
        cookie = await _stepped_up(client, users, factors, clock)
        factor_id = await _only_factor_id(client, cookie)

        forged = await client.post(f"/forge/{users._by_email['ann@example.test']}")
        response = await client.delete(
            f"/auth/2fa/{factor_id}", headers={"cookie": _cookie(forged)}
        )

        assert response.status == 403
        assert response.json() == {"error": "second_factor_required"}


async def test_removing_a_factor_id_that_is_not_yours_is_a_404() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = await _stepped_up(client, users, factors, clock)
        # Somebody else's credential, present in the store under another user.
        await factors.add(_factor(id="not-mine", user_id="somebody-else"))

        response = await client.delete("/auth/2fa/not-mine", headers={"cookie": cookie})

        assert response.status == 404
        assert response.json() == {"error": "not_found"}
        # Still there: a 404 that had actually deleted it would be worse than a 200.
        assert [row.id for row in await factors.credentials("somebody-else")] == ["not-mine"]


async def _pending_cookie(
    client: Any,
    users: InMemoryUserStore,
    factors: InMemorySecondFactorStore,
    clock: _Clock,
) -> tuple[str, str]:
    """Enrol, log out, log back in, and stop at the pending marker.

    Returns the pending cookie and the TOTP code that would complete it, so a
    test can change the account in between and still submit a *correct* code --
    otherwise a refusal proves only that the code was wrong.
    """
    first = await _login(client)
    secret_b32, _, cookie = await _enrol(client, clock, _cookie(first))
    await client.post("/users/logout", headers={"cookie": cookie})
    clock.now += 60
    pending = _cookie(await _login(client))
    return pending, totp_code(base32_to_secret(secret_b32), totp_counter(clock.now))


async def test_a_correct_code_for_an_account_that_has_gone_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        user = await _seed(users)
        pending, code = await _pending_cookie(client, users, factors, clock)

        users._by_id.pop(user.id)

        response = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": pending}
        )

        assert response.status == 401
        assert response.json() == {"error": "invalid_code"}
        # And the half-finished login is not left open to be retried.
        cookie = _cookie(response) or pending
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert "pending_second_factor" not in session
        assert "principal" not in session


async def test_a_correct_code_for_a_deactivated_account_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        user = await _seed(users)
        pending, code = await _pending_cookie(client, users, factors, clock)

        user.is_active = False
        await users.update(user)

        response = await client.post(
            "/auth/2fa/verify", json={"code": code}, headers={"cookie": pending}
        )

        assert response.status == 401
        assert response.json() == {"error": "invalid_code"}
