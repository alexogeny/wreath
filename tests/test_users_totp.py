"""Second factors, stage one: TOTP, recovery codes, and pending sessions.

One test per bullet of the plan's "Security requirements, stated as
requirements" section, deliberately not merged: a single test that exercises
replay, skew and throttling together passes with any one of the three checks
missing, which is exactly how a second factor ships broken.

Run under `python -O` as well as normally -- that is the interpreter mode where
a check written as an `assert` would silently not exist.
"""

from __future__ import annotations

import os
import time
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


# --- RFC 6238 ---------------------------------------------------------------


@pytest.mark.parametrize(("moment", "expected"), RFC6238_SHA1_VECTORS)
def test_rfc6238_sha1_test_vectors(moment: int, expected: str) -> None:
    """The standard's own table, including the vector past a 32-bit time_t."""
    assert totp_code(RFC_SECRET, totp_counter(moment), digits=8) == expected


def test_rfc6238_vectors_verify_as_well_as_generate() -> None:
    """Verification agrees with generation at the same instants."""
    for moment, expected in RFC6238_SHA1_VECTORS:
        matched = verify_totp(
            RFC_SECRET, expected, at=moment, digits=8, skew=0
        )
        assert matched == totp_counter(moment)


# --- replay -----------------------------------------------------------------


async def test_a_code_that_verified_once_never_verifies_again() -> None:
    """The single most-botched requirement: one accepted code, one use."""
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
    """Credential kinds remain domains even when their material is usable elsewhere."""
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    await store.add(_factor(id="rec-1", kind="recovery", material=SECRET))

    assert await verify_second_factor(store, "user-1", _at(moment), at=moment) is None


async def test_replay_survives_the_skew_window_of_a_later_step() -> None:
    """Skew must not re-open a step that has already been spent.

    A code accepted at step N is inside the -1 skew window of step N+1, so an
    implementation that stores the counter but forgets to compare against it
    when skew widens the search accepts it a second time thirty seconds later.
    """
    store = InMemorySecondFactorStore()
    await store.add(_factor())
    moment = 1_700_000_000.0
    code = _at(moment)
    assert await verify_second_factor(store, "user-1", code, at=moment) is not None
    assert await verify_second_factor(store, "user-1", code, at=moment + 30) is None


def test_verify_totp_requires_a_strictly_greater_counter() -> None:
    """The primitive refuses its own last-accepted step, not just earlier ones."""
    moment = 1_700_000_000.0
    counter = totp_counter(moment)
    code = totp_code(SECRET, counter)
    assert verify_totp(SECRET, code, at=moment, skew=0) == counter
    assert verify_totp(SECRET, code, at=moment, skew=0, last_counter=counter) is None
    assert verify_totp(SECRET, code, at=moment, skew=0, last_counter=counter + 1) is None


async def test_the_confirming_code_cannot_also_sign_in() -> None:
    """Enrolment consumes the step it confirmed with, so it is spent already."""
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    enrolment = begin_totp_enrolment(account="a@b.co", secret=SECRET)
    code = totp_code(enrolment.secret, totp_counter(moment))
    confirmed = await confirm_totp_enrolment(
        store, "user-1", secret=enrolment.secret, code=code, at=moment,
        recovery_codes=1,
    )
    assert confirmed is not None
    assert await verify_second_factor(store, "user-1", code, at=moment) is None


async def test_a_store_refuses_to_move_a_counter_backwards() -> None:
    """The replay defence is only as good as the counter's monotonicity."""
    store = InMemorySecondFactorStore()
    await store.add(_factor(counter=100))
    assert await store.touch("cred-1", counter=99, at=datetime.now(UTC)) is False
    assert (await store.credentials("user-1"))[0].counter == 100


async def test_a_store_advance_is_conditional_and_says_whether_it_won() -> None:
    """`touch` is a compare-and-set: the same step twice wins exactly once.

    The return value is the whole protocol change. A caller that reads the
    counter, checks a code, and then writes cannot tell a first use from a
    second one by looking -- both read the same number -- so the store has to
    answer it.
    """
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
    """The race the counter exists to lose, run rather than described.

    Two tasks verify the same code against the same credential and both finish
    reading before either writes. Exactly one may be admitted: the loser is a
    replay that happened to arrive at the same moment rather than a moment
    later, and an attacker racing the legitimate user with an observed code is
    the reason the single-use guarantee exists.
    """
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


# --- skew -------------------------------------------------------------------


@pytest.mark.parametrize("code", ["١٢٣٤٥٦", "²²²²²²", "12345٦"])
def test_a_non_ascii_digit_is_refused_rather_than_compared(code: str) -> None:
    """`str.isdigit()` is true of these, and `compare_digest` raises on them.

    Six Arabic-Indic digits are six digits as far as `isdigit` is concerned, and
    `hmac.compare_digest` refuses a `str` with a non-ASCII character in it by
    raising `TypeError` -- so a length-and-digits gate written without
    `isascii()` hands the comparison something it cannot compare.
    """
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
    """Configurable, but not unboundedly: a huge skew is a long-lived password."""
    moment = 1_700_000_000.0
    assert verify_totp(SECRET, _at(moment - 60), at=moment, skew=2) is not None
    assert verify_totp(SECRET, _at(moment - 60), at=moment, skew=0) is None
    with pytest.raises(ValueError):
        verify_totp(SECRET, _at(moment), at=moment, skew=-1)
    with pytest.raises(ValueError):
        verify_totp(SECRET, _at(moment), at=moment, skew=MAX_SKEW + 1)


# --- constant-time comparison ----------------------------------------------


def test_codes_are_compared_with_hmac_compare_digest(monkeypatch) -> None:
    """A `==` on a code leaks its correct prefix through timing."""
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
    """A `==` on a stored digest leaks how much of a guess was right."""
    import wreath._secondfactor as module

    store = InMemorySecondFactorStore()
    await store.add(
        _factor(
            id="rec-1", kind="recovery",
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


async def test_a_stage_one_scrypt_recovery_code_still_verifies() -> None:
    """Moving off the password hasher must not invalidate issued codes.

    Nothing mints one of these any more; a deployment that ran stage one has a
    table full of them, and an upgrade that silently stopped honouring them
    would take away the safety net at the moment somebody reaches for it.
    """
    store = InMemorySecondFactorStore()
    await store.add(
        _factor(
            id="rec-1", kind="recovery",
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


# --- two-phase enrolment ----------------------------------------------------


async def test_begin_enrols_nothing() -> None:
    """A secret shown but never confirmed must never become a factor."""
    store = InMemorySecondFactorStore()
    enrolment = begin_totp_enrolment(account="a@b.co", issuer="Wreath")
    assert enrolment.uri.startswith("otpauth://totp/")
    assert await store.credentials("user-1") == []


async def test_a_wrong_code_at_confirm_enrols_nothing() -> None:
    store = InMemorySecondFactorStore()
    enrolment = begin_totp_enrolment(account="a@b.co", secret=SECRET)
    confirmed = await confirm_totp_enrolment(
        store, "user-1", secret=enrolment.secret, code="000000", at=1_700_000_000.0,
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
        store, "user-1", secret=enrolment.secret,
        code=totp_code(enrolment.secret, totp_counter(moment)),
        at=moment, recovery_codes=3,
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
        store, "user-1", secret=SECRET,
        code=totp_code(SECRET, totp_counter(moment)), at=moment, recovery_codes=1,
    )
    with pytest.raises(ValueError):
        await confirm_totp_enrolment(
            store, "user-1", secret=SECRET,
            code=totp_code(SECRET, totp_counter(moment + 30)),
            at=moment + 30, recovery_codes=1,
        )


# --- recovery codes ---------------------------------------------------------


async def test_recovery_codes_are_hashed_never_stored_in_plaintext() -> None:
    """Hashed, and with the hash the entropy warrants: one SHA-256 pass.

    A recovery code carries ~78 bits, so there is no guessing list a slow KDF
    would be defending against -- and stage one's scrypt cost ~0.4s of a worker
    thread per enrolment and a ten-hash walk per wrong code. What must not
    change is that the plaintext is nowhere in the row.
    """
    store = InMemorySecondFactorStore()
    moment = 1_700_000_000.0
    confirmed = await confirm_totp_enrolment(
        store, "user-1", secret=SECRET,
        code=totp_code(SECRET, totp_counter(moment)), at=moment, recovery_codes=2,
    )
    assert confirmed is not None
    _, codes = confirmed
    stored = [
        row.material for row in await store.credentials("user-1")
        if row.kind == "recovery"
    ]
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
        store, "user-1", secret=SECRET,
        code=totp_code(SECRET, totp_counter(moment)), at=moment, recovery_codes=2,
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
        store, "user-1", secret=SECRET,
        code=totp_code(SECRET, totp_counter(moment)), at=moment, recovery_codes=1,
    )
    assert confirmed is not None
    _, codes = confirmed
    assert await verify_second_factor(store, "user-2", codes[0], at=moment) is None


# --- material never leaves ---------------------------------------------------


def test_a_credential_repr_does_not_carry_its_material() -> None:
    """A dataclass repr reaches tracebacks and log lines nobody wrote."""
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


# --- no invariant depends on assert -----------------------------------------


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
    """`python -O` deletes `assert`; every check in these must be a `raise`."""
    import ast
    import importlib

    module = importlib.import_module(module_name)
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert [node for node in ast.walk(tree) if isinstance(node, ast.Assert)] == []


# --- the router: throttling, pending sessions, promotion --------------------


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
    app.include_router(
        user_router(users, secret="u" * 32, second_factors=factors, clock=clock)
    )
    # No `pytest.warns` wrapper: building without `enrolments=` no longer warns,
    # because it no longer degrades. See `test_users_webauthn.py`.
    router = second_factor_router(
        users, factors, issuer="Wreath", clock=clock, **options
    )
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
        request.state.session["principal"] = {
            "sub": user_id, "type": "User", "roles": []
        }
        return {"status": "adopted"}

    return app


async def _login(
    client: Any, email: str = "ann@example.test", cookie: str = ""
) -> Any:
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
    """The whole point: password accepted, but nobody is signed in yet."""
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
    """Second line of defence, in case a pending payload reaches the principal."""
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
    """A six-digit code is a million guesses; unthrottled it is a brute force."""
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
    """The same input where it lands: an unhandled `TypeError` was a 500 that
    the throttle never counted, because the failure never returned to it."""
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
    """At build time, not on the first person trying to sign in."""
    users, factors = InMemoryUserStore(), InMemorySecondFactorStore()
    with pytest.raises(ValueError):
        second_factor_router(users, factors, skew=MAX_SKEW + 1)
    with pytest.raises(ValueError):
        second_factor_router(users, factors, period=0)
    with pytest.raises(ValueError):
        second_factor_router(users, factors, digits=4)


# --- the ORM-backed store ---------------------------------------------------


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


async def test_orm_store_remove_is_scoped_to_the_owner() -> None:
    """The id is the only thing an HTTP caller supplies."""
    store, session, _ = _orm_store()
    await store.add(_factor())
    await store.remove("user-2", "cred-1")
    assert session.deleted == []
    assert len(await store.credentials("user-1")) == 1
    await store.remove("user-1", "cred-1")
    assert len(session.deleted) == 1
    assert await store.credentials("user-1") == []


async def test_orm_store_touch_sends_one_conditional_statement() -> None:
    """The comparison travels with the write, and reads no row first.

    A read, a check in Python, and an unconditional `UPDATE` is the shape that
    lets two requests carrying one code both through -- both read the same
    counter while the other is still checking its code. So what is asserted here
    is the statement itself: one round trip, the guard in its `WHERE`, and a
    `RETURNING` to say whether it matched anything.
    """
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
    """No row updated is not "nothing to do"; it is a replay.

    PostgreSQL answers a conditional `UPDATE` that matched nothing with no row
    at all, and this is the translation of that answer into the one word the
    caller acts on.
    """
    store, session, _ = _orm_store()
    await store.add(_factor(counter=100))
    session.raw_result = None
    assert await store.touch("cred-1", counter=99, at=datetime.now(UTC)) is False


async def test_orm_store_satisfies_the_protocol() -> None:
    from wreath.users import SecondFactorStore

    store, _, _ = _orm_store()
    assert isinstance(store, SecondFactorStore)
    assert isinstance(InMemorySecondFactorStore(), SecondFactorStore)


# --- the race, against a real PostgreSQL ------------------------------------
#
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
    """Two connections, one code, one admission. The rest is a replay."""
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
        registry = Registry(
            _OneConnectionEach([second, first]), [model], validate_schema="off"
        )
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
                *(
                    verify_second_factor(store, "user-1", code, at=moment)
                    for store in raced
                )
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


# --- the unconfirmed enrolment, held server-side ----------------------------


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
    """A response body is transient; a cookie is written down."""
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
    """Deleting the row revokes the enrolment, cookie or no cookie."""
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
    """A session that changes hands must not confirm somebody else's secret.

    Through `/adopt` rather than through logout-and-login: those clear the
    enrolment outright now, so a version of this test that went that way would
    watch the confirmation fail because there was nothing there -- and would
    stay green with the binding deleted. It did, and this is that test fixed.
    """
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


# --- state that outlives its ceremony ---------------------------------------


async def test_a_begun_enrolment_does_not_survive_a_logout() -> None:
    """An unconfirmed secret belongs to the sitting a user began it in.

    Both halves matter: the marker leaves the session, and the row behind it
    leaves the store. Clearing only the cookie key would leave the secret
    server-side until its TTL, which is the half a later holder of the handle
    could still reach.
    """
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
    """Signing in ends anything half-done, even without a sign-out first.

    A shared browser is the case: the next person types their password on a
    session that still holds the previous one's unconfirmed secret. It is not
    cross-account exploitable -- the record names its user and every consumer
    checks -- but state that outlives the ceremony it belonged to is the shape
    the account takeover came in.
    """
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
    """The half of the old warning that was about privacy, now simply untrue.

    This test used to assert the opposite -- that without an `enrolments=` store
    the secret rides in the session, so a fix that only knew how to delete rows
    would miss it. There is no such deployment any more: with no store named,
    the enrolment goes to the `ChallengeStore` instead of the session, so the
    cookie carries an opaque handle and never the secret. What the session holds
    is a marker; what logout clears is the marker *and* the row behind it.
    """
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


# --- a login that cannot check the factor the account has -------------------


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
    """Fail closed: a password alone must not complete a login with MFA on it.

    Wired this way the user enrols a factor, sees it listed, and is then signed
    in by the password alone -- every signal saying protected, nothing being so.
    Refusing locks out a misconfigured deployment, which is the trade
    the refuse-rather-than-half-wire rule makes: a door that names its own misconfiguration beats
    one that opens quietly.
    """
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
    """It answers about the account in hand, not about the deployment.

    A user with nothing enrolled has nothing that needs checking, so refusing
    their login would be an outage in exchange for no security at all.
    """
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
    """They are the residue of a factor, not a factor: no code prompt exists.

    A user whose last real factor was removed keeps nothing that a login could
    have asked for, so refusing them would lock out an account that is not
    protected by anything.
    """
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
    """The refusal is for the unwired case only, and costs a wired one nothing.

    A `user_router` that was given `second_factors=` answers from that store and
    reaches none of the machinery above -- which is also why an application with
    two of these, one with MFA and one without, is unaffected by the other.
    """
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


# --- the refusals nothing had ever made fire ---------------------------------
#
# `wreath mutant --operators guard.remove-raise` reported every one of these
# UNREACHED across all 213 tests of the second-factor suite: the refusal could
# be deleted outright and nothing would execute the line, let alone object.
# None was a defect -- each is a documented `Raises:` contract that holds -- but
# a refusal nobody has watched work is a refusal nobody knows works, which is
# the cheaper half of a mutation report to act on.


def test_a_secret_below_the_rfc_4226_floor_is_refused() -> None:
    """`MIN_SECRET_BYTES` is 128 bits, and it is a security bound, not a hint.

    All three doors are checked, because a short secret let in through any one
    of them is the same weak secret: minting one, decoding one a user typed,
    and computing a code from one.
    """
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
    """Not silently downgraded to SHA-1, which is the failure worth refusing."""
    with pytest.raises(ValueError, match="unsupported TOTP algorithm"):
        totp_code(b"\x01" * 20, 1, algorithm="md5")


def test_totp_counter_refuses_a_period_or_a_timestamp_it_cannot_count() -> None:
    """A zero period divides by zero; a pre-epoch stamp counts backwards."""
    with pytest.raises(ValueError, match="period must be positive"):
        totp_counter(0, period=0)
    with pytest.raises(ValueError, match="period must be positive"):
        totp_counter(0, period=-30)
    with pytest.raises(ValueError, match="precedes the Unix epoch"):
        totp_counter(-1)
    assert totp_counter(0) == 0            # the epoch itself is countable

    # And the counter `totp_code` is handed is bounded the same way, because a
    # negative step is a `struct.pack` away from a code for some other moment.
    with pytest.raises(ValueError, match="counter must not be negative"):
        totp_code(b"\x01" * 20, -1)
    assert totp_code(b"\x01" * 20, 0)


def test_a_totp_uri_refuses_a_label_it_cannot_delimit() -> None:
    """The `issuer:account` label has one separator, so neither half may carry one."""
    secret = b"\x01" * 20
    with pytest.raises(ValueError, match="account must not be empty"):
        totp_uri(secret, account="")
    with pytest.raises(ValueError, match="must not contain"):
        totp_uri(secret, account="ada:lovelace")
    with pytest.raises(ValueError, match="must not contain"):
        totp_uri(secret, account="ada", issuer="camera:trap")


def test_recovery_code_counts_are_bounded_at_both_ends() -> None:
    """Zero codes is a lockout; fifty thousand is a response nobody reads."""
    from wreath._secondfactor import generate_recovery_codes

    with pytest.raises(ValueError, match="at least 1"):
        generate_recovery_codes(0)
    with pytest.raises(ValueError, match="must not exceed 50"):
        generate_recovery_codes(51)
    assert len(generate_recovery_codes(1)) == 1
    assert len(generate_recovery_codes(50)) == 50


async def test_a_store_refuses_a_duplicate_second_factor_id() -> None:
    """Two credentials under one id would make removal ambiguous."""
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


# --- every second-factor endpoint, with nothing signed in -------------------------
#
# `second_factor_router` opens every handler with the same two refusals -- no session
# at all, then no signed-in user -- and a mutation sweep reported almost all of them
# `survived`: the suite reaches them only through the happy path, where a session
# exists and somebody is signed in. Both guards were therefore deletable on nearly
# every route, and `_signed_in` returning `None` would have been read as a user.
#
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
    method: str, path: str, body: Any, anonymous_error: str,
) -> None:
    """500 `session_middleware_required`, on every route, not just the first.

    Parametrised per route rather than looped in one test so that a route which
    stops answering is named by the failure -- and so adding a route without this
    refusal shows up here.
    """
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _router_only_app(users, factors, clock, sessions=False)
    async with TestClient(app) as client:
        response = await _request(client, method, path, body)
    assert response.status == 500, (path, response.status)
    assert response.json() == {"error": "session_middleware_required"}


@pytest.mark.parametrize(("method", "path", "body", "anonymous_error"), _SECOND_FACTOR_ROUTES)
async def test_every_second_factor_route_refuses_an_anonymous_session(
    method: str, path: str, body: Any, anonymous_error: str,
) -> None:
    """401 on every route — a session exists, nobody is signed in.

    The expected error is in the table rather than asserted uniformly, because the
    two `verify` routes genuinely answer a different one and flattening that to
    "some 401" would let either guard be deleted in favour of the other.
    """
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _router_only_app(users, factors, clock, sessions=True)
    async with TestClient(app) as client:
        response = await _request(client, method, path, body)
    assert response.status == 401, (path, response.status, response.json())
    assert response.json() == {"error": anonymous_error}


# --- _principal: the four ways a session fails to name a signed-in user -----------


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
    principal: Any, signed_in_if_unguarded: bool,
) -> None:
    """Each clause of `_principal` needs a session that isolates it.

    The subject has to be a **real** user id, which is the part that makes this
    test work at all: with a made-up id, deleting a guard still ends in
    `users.get_by_id` returning `None` and the same 401, so every clause stayed
    deletable. The seeded store hands out `"1"`.

    That is also what makes `{"sub": 1}` the sharp case rather than a contrived
    one. `_signed_in` calls `users.get_by_id(str(principal["sub"]))`, so an integer
    id — what most databases hand an application that writes the principal itself,
    the way `wreath._auth.oauth2` does — becomes `"1"` and signs in as a real user
    if `not isinstance(subject, str)` is dropped.

    `pending` is the clause that matters most: that principal has proved a password
    and *not* a second factor, and treating it as signed in is the bypass this
    router exists to prevent.

    `{"sub": ""}` is marked as *not* signed in even unguarded, and is here as a
    recorded non-finding: `not subject` is defence in depth, because an empty id
    matches no row either way. It stays because the guard reads better as the
    complete statement of what a subject must be.
    """
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
        response = await client.post(
            "/auth/2fa/totp/begin", headers={"cookie": cookie}
        )
    assert response.status == 401, (principal, response.status, response.json())
    assert response.json() == {"error": "not_authenticated"}


# --- totp/confirm: the four states a begun enrolment can be in --------------------


async def _signed_in_client(client: Any, users: InMemoryUserStore) -> str:
    await _seed(users)
    return _cookie(await _login(client))


async def test_confirming_with_no_enrolment_in_progress_is_refused() -> None:
    """`if not isinstance(marker, dict)` — a confirm that was never begun.

    Signed in, so the two guards above cannot answer for this one.
    """
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        cookie = await _signed_in_client(client, users)
        response = await client.post(
            "/auth/2fa/totp/confirm", json={"code": "000000"}, headers={"cookie": cookie}
        )
    assert response.status == 400
    assert response.json() == {"error": "no_enrolment_in_progress"}


async def test_confirming_an_enrolment_begun_too_long_ago_is_refused() -> None:
    """`clock() - started > enrolment_ttl`, and the unconfirmed secret is dropped.

    The secret is the reason the second assertion is here: an expired enrolment
    that stayed in the session would let the same code be confirmed later, which
    is the whole point of parking it with a TTL.
    """
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
    """`if any(row.kind == "totp" for row in enrolled)`.

    Enrolling twice mints a second secret and a second set of recovery codes and
    invalidates neither, so the user ends up with two of everything and no way to
    tell which is live.
    """
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock)) as client:
        cookie = await _signed_in_client(client, users)
        _, _, cookie = await _enrol(client, clock, cookie)
        response = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
    assert response.status == 409
    assert response.json() == {"error": "already_enrolled"}


# --- totp/confirm: a marker that is present but not usable ------------------------
#
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
            "id": handle, "at": planted["at"],
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
    marker: dict[str, Any], expected: str,
) -> None:
    """A timestamp that is not a number, a secret that is not a string, and a
    string that is not base32.

    The first is the interesting one: `not isinstance(started, (int, float))` shares
    its `if` with the TTL comparison, and without it `clock() - "recently"` raises
    `TypeError` and the endpoint answers 500 instead of asking the user to start
    again. The third is reachable only from a session written by a different build
    of the router, which is exactly why nothing had ever produced it.
    """
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app_with_planting(users, factors, clock)) as client:
        cookie = await _signed_in_client(client, users)
        planted = await client.post(
            "/plant-enrolment", json=marker, headers={"cookie": cookie}
        )
        cookie = _cookie(planted) or cookie
        response = await client.post(
            "/auth/2fa/totp/confirm", json={"code": "000000"}, headers={"cookie": cookie}
        )
        assert response.status == 400, (marker, response.json())
        assert response.json() == {"error": expected}

        session = await client.get(
            "/session", headers={"cookie": _cookie(response) or cookie}
        )
        assert "pending_totp_enrolment" not in session.json()


async def test_confirming_after_another_session_already_enrolled_is_refused() -> None:
    """The race `begin` cannot see: two enrolments begun, one confirmed first.

    `begin` refuses a second enrolment, so the only way to reach the *same* check
    inside `confirm` is for the factor to appear between this session's `begin` and
    its `confirm`. Without the check the second confirm mints a second secret and a
    second set of recovery codes, invalidating neither — the exact outcome `begin`'s
    refusal exists to prevent, reached by going around it.
    """
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


# --- DELETE /auth/2fa/{factor_id} -------------------------------------------
#
# The whole endpoint had never been called. A mutation sweep reported its
# step-up guard UNREACHED, which is the worse of the two findings: not "the
# tests would not notice", but "nothing has ever watched this refusal work" --
# and it is the refusal standing between a stolen session and the removal of
# the second factor that session could not have produced.


async def _stepped_up(
    client: Any, users: InMemoryUserStore, factors: InMemorySecondFactorStore,
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
    """The permitting arm, and the one that keeps the guard from being a ban."""
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
    """A session signed in by something that never proved a factor.

    `/adopt` is how an application that is not `user_router` signs somebody in --
    an OAuth2 callback, an SSO bridge -- and it writes no `second_factor_at`. A
    session obtained that way (or stolen from a browser that had one) must not be
    able to switch the second factor off, which is precisely what the caller who
    stole it wants.
    """
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
    """`step_up_ttl` is what makes this *recent* rather than *ever*.

    Without the age clause a session that proved a factor once at sign-in could
    remove it a week later, which is the same standing authority the step-up was
    introduced to replace.
    """
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, step_up_ttl=300.0)) as client:
        await _seed(users)
        cookie = await _stepped_up(client, users, factors, clock)
        factor_id = await _only_factor_id(client, cookie)

        clock.now += 301
        response = await client.delete(
            f"/auth/2fa/{factor_id}", headers={"cookie": cookie}
        )

        assert response.status == 403
        assert response.json() == {"error": "second_factor_required"}


async def test_a_boolean_second_factor_stamp_does_not_authorise_removal() -> None:
    """`True` is an `int` in Python, and `clock() - True` is a number.

    So a principal carrying `second_factor_at: True` -- from a store that
    round-tripped the field through something JSON-ish, or an application
    writing a flag where a time belongs -- would otherwise read as a factor
    proved one second after the epoch, which on a test clock and on a machine
    whose clock is anything but enormous is *recent*. The `isinstance(stamp,
    bool)` clause is the only thing between that and a permitted removal, and
    nothing had ever run it.
    """
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = _app(users, factors, clock)

    @app.post("/forge/{user_id}")
    async def forge(request: Any, user_id: str) -> dict[str, Any]:
        request.state.session["principal"] = {
            "sub": user_id, "type": "User", "roles": [], "second_factor_at": True,
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
    """One answer for "no such id" and "not yours", so neither can be probed."""
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


# --- the account behind a pending login can change under it ------------------


async def _pending_cookie(
    client: Any, users: InMemoryUserStore, factors: InMemorySecondFactorStore,
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
    """`user is None` after the code verified, which is the awkward ordering.

    The factor is checked against the *subject on the pending marker*, so a
    deleted account still has credentials that verify -- the store row outlives
    the user row, and in a real deployment so does the window between the two
    deletes. Without this clause the endpoint would go on to read `.is_active`
    off `None` and answer 500, or, one line further, mint a principal for an
    account that no longer exists.
    """
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
    """Deactivation has to bite between the password and the code, too.

    Login checks `is_active`; a pending login has already passed that check, so
    an account deactivated during the seconds somebody spends reading their
    phone would otherwise complete and be signed in.
    """
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
