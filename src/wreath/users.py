"""User-management lifecycle flows — the `fastapi-users` equivalent.

Builds registration / login / logout / email-verification / password-reset / me
*on top of* wreath's existing auth: login writes the same signed-session principal
(`{"sub", "type", "roles"}`) that `wreath.auth.SessionIdentityBackend` already
reads, so this integrates with the auth stack rather than reinventing it. It
writes no `exp` and no `permissions`, so a session minted here lasts the session
cookie's own lifetime and carries roles only.

The security-sensitive core (scrypt hashing, HMAC action tokens, flow logic) lives
in the stdlib-only `wreath._userkit`; this module is the thin wreath glue.

    store = InMemoryUserStore()            # or an ORM-backed UserStore
    sessions = PostgresSessionStore(database)
    app.include_router(
        user_router(store, sessions=sessions, secret=SECRET, base_url="https://app")
    )

`SessionPolicy` is required for login: without it `POST /login` answers 500
rather than signing anyone in. `POST /logout` and `GET /me` degrade instead --
logout reports success having cleared nothing, and `/me` answers 401.

Email delivery is a pluggable `EmailSender`. The default `LogEmailSender` prints
the link rather than sending it; `SmtpEmailSender` (stdlib `smtplib`, STARTTLS or
implicit TLS on port 465) is the shipped production transport.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from math import isfinite
from time import monotonic, time
from typing import Annotated, Any, cast

from . import _secondfactor, _userkit
from ._capability_map import CapabilityMap
from ._secondfactor import (  # re-export the stdlib-only second-factor surface
    CHALLENGE_ENROLMENT,
    CHALLENGE_WEBAUTHN_ASSERT,
    CHALLENGE_WEBAUTHN_REGISTER,
    DEFAULT_DIGITS,
    DEFAULT_PERIOD,
    DEFAULT_RECOVERY_CODES,
    DEFAULT_SKEW,
    BulkSecondFactorRemovalStore,
    BulkSecondFactorStore,
    DiscoverableSecondFactorStore,
    InMemorySecondFactorStore,
    MemoryChallengeStore,
    SecondFactor,
    SecondFactorStore,
    TotpEnrolment,
    WebAuthnAssertion,
    WebAuthnCeremony,
    WebAuthnError,
    generate_recovery_codes,
    totp_uri,
)
from ._userkit import (  # re-export the stdlib-only surface
    CapturingEmailSender,
    EmailSender,
    InMemoryUserStore,
    LogEmailSender,
    SmtpEmailSender,
    UserRecord,
    UserStore,
    hash_password,
    verify_password,
)
from ._webauthn import (
    b64url_decode,
    b64url_encode,
    default_origins,
    unpack_credential,
)
from .binding import Body, Path
from .policy.sessions import rotate_session
from .response import JSONResponse
from .router import Router

__all__ = [
    "BulkSecondFactorRemovalStore",
    "BulkSecondFactorStore",
    "CapturingEmailSender",
    "DiscoverableSecondFactorStore",
    "EmailSender",
    "InMemorySecondFactorStore",
    "InMemoryUserStore",
    "LogEmailSender",
    "OrmSecondFactorStore",
    "OrmUserStore",
    "SecondFactor",
    "SecondFactorStore",
    "SmtpEmailSender",
    "TotpEnrolment",
    "UserRecord",
    "UserStore",
    "WebAuthnAssertion",
    "WebAuthnCeremony",
    "WebAuthnError",
    "default_second_factor_model",
    "default_user_model",
    "generate_recovery_codes",
    "hash_password",
    "second_factor_router",
    "totp_uri",
    "user_router",
    "verify_password",
]

#: Namespace for a begun-but-unconfirmed enrolment held in a `SessionStore`.
#: Session ids from `SessionPolicy` are random tokens with no prefix, so a
#: namespaced key cannot collide with one and an enrolment row can never be
#: loaded as if it were somebody's session.
_ENROLMENT_PREFIX = "wreath.2fa.enrolment."

#: The same idea for a begun WebAuthn ceremony's challenge. A separate namespace
#: rather than a shared one, so a registration marker can never be loaded as an
#: assertion challenge or the other way about.
_WEBAUTHN_PREFIX = "wreath.2fa.webauthn."

#: The session keys a begun-but-unfinished ceremony parks itself under. They are
#: the defaults of `second_factor_router`'s `enrolment_key` and `webauthn_key`,
#: and they are named here rather than only there because `user_router` clears
#: them at both ends of a session and must not have to be told they exist.
_ENROLMENT_KEY = "pending_totp_enrolment"
_WEBAUTHN_KEY = "pending_webauthn"


@dataclass(frozen=True, slots=True)
class _SecondFactorWiring:
    """One mounted `second_factor_router`, as `user_router` needs to see it.

    `users` is the identity that scopes it. A `user_router` consults only the
    wirings built over **the same users** it serves, and "the same" means the
    same object *or* the same declared `store_id` -- see `_same_store`.

    Object identity alone was the original rule and it was too narrow: it is
    exact for a store that holds its own data, and silently false for one that
    does not. `OrmUserStore(session, model)` holds nothing, so two of them over
    the same model are two objects serving one table -- which is what a
    deployment gets by building a store inline for each router, the mistake
    `_factor_another_store_holds` was written for one field over. Under `is`
    that login matched no wiring at all, concluded there was no factor it could
    not check, and signed the caller in on a password alone with 2FA enrolled.

    Matching on the *user id* instead would be a coincidence waiting to happen,
    since `InMemoryUserStore` numbers its users from one -- which is why the
    widening is a declared identity rather than a guess.

    `anchor` is a weak reference to one of that router's own handler functions,
    which is what scopes this record's *lifetime*: the router holds the handler,
    an application that included the router holds it too, and when the last of
    them is dropped the reference dies and `_mounted_second_factors` forgets the
    entry. A strong reference here would keep every store any process ever built
    alive for as long as it ran.
    """

    anchor: Any
    users: UserStore
    factors: SecondFactorStore
    enrolments: Any
    challenges: Any
    enrolment_key: str
    webauthn_key: str


#: Every `second_factor_router` built in this process that is still reachable.
#:
#: This is the only link between the two routers, and it exists because there is
#: no other: they are built separately, `include_router` copies routes rather
#: than retaining the router, and a request carries no handle on its
#: application. A `user_router` built *without* `second_factors=` reads this at
#: login to find out whether the account it is about to sign in has a factor it
#: cannot check -- see `user_router`'s refusal. Nothing else reads it, and a
#: correctly wired application never reaches it at all.
_MOUNTED_SECOND_FACTORS: list[_SecondFactorWiring] = []


def _same_store(left: Any, right: Any) -> bool:
    """Whether two store objects serve the same rows.

    The same object always does. Two *different* objects do when both declare
    the same `store_id` -- an optional attribute a store sets when its identity
    is something other than itself. `OrmUserStore` and `OrmSecondFactorStore`
    return their model, database, and tenant namespace; a store that holds its
    own data (`InMemoryUserStore`) declares none, and `is` remains the right and
    only answer for it.

    `None` on either side falls back to identity rather than matching, so a
    store that declares nothing is never fused with another that declares
    nothing. Two applications in one process would otherwise be treated as one.
    """
    if left is right:
        return True
    key = getattr(left, "store_id", None)
    return key is not None and key == getattr(right, "store_id", None)


def _mounted_second_factors(users: UserStore | None = None) -> list[_SecondFactorWiring]:
    """The live wirings, dropping any whose router has been collected.

    With `users`, only the ones serving those same users: see
    `_SecondFactorWiring` for why a declared store identity is the right scope
    and a user id is not.
    """
    live = [wiring for wiring in _MOUNTED_SECOND_FACTORS if wiring.anchor() is not None]
    if len(live) != len(_MOUNTED_SECOND_FACTORS):
        _MOUNTED_SECOND_FACTORS[:] = live
    if users is None:
        return live
    return [wiring for wiring in live if _same_store(wiring.users, users)]


def _ceremony_slots(users: UserStore, sessions: Any) -> list[tuple[str, str, Any]]:
    """Where a half-finished second-factor ceremony can be, as (key, prefix, store).

    The two defaults are always included, because `user_router` is built without
    any knowledge of a `second_factor_router` and must still clear what one
    would have left. Any router actually mounted in this process contributes its
    own key names and its own `enrolments` store on top, so a deployment that
    renamed either key, or parked its ceremonies somewhere other than the store
    `user_router` was given, is cleared correctly rather than silently missed.
    """
    slots: dict[tuple[str, str], Any] = {}
    for wiring in _mounted_second_factors(users):
        store = wiring.enrolments if wiring.enrolments is not None else sessions
        slots[(wiring.enrolment_key, _ENROLMENT_PREFIX)] = store
    slots.setdefault((_ENROLMENT_KEY, _ENROLMENT_PREFIX), sessions)
    return [(key, prefix, store) for (key, prefix), store in slots.items()]


def _challenge_slots(users: UserStore) -> list[tuple[str, Any]]:
    """Where a begun WebAuthn ceremony can be, as (session key, store).

    Separate from `_ceremony_slots` because a `ChallengeStore` is a different
    shape: it is keyed by a bare handle and spent with `discard`, where a
    `SessionStore` takes a prefixed id and `delete`. Forcing one call shape onto
    both would mean one of them lying about what it does.

    Only mounted routers contribute. A `user_router` with no
    `second_factor_router` beside it can still drop the session key -- and does,
    in `_forget_ceremonies` -- but there is no store for it to reach, because
    the challenge belongs to a router this process has not built.
    """
    slots: dict[str, Any] = {}
    for wiring in _mounted_second_factors(users):
        if wiring.challenges is not None:
            slots[wiring.webauthn_key] = wiring.challenges
    return list(slots.items())


async def _forget_ceremonies(session: dict[str, Any], users: UserStore, sessions: Any) -> None:
    """Drop every begun-but-unfinished ceremony from `session`, rows included.

    A pending TOTP enrolment is an unconfirmed shared secret and a pending
    WebAuthn marker is a live challenge. Both belong to the ceremony that began
    them and to nothing else, so a sign-in or a sign-out ends them: an
    unconfirmed secret that outlives a logout on a shared machine is state
    outliving its ceremony, which is the shape that produced the account
    takeover this whole area was hardened against.

    **The server-side row goes too, not just the cookie key.** The marker in the
    session is only a handle; dropping it alone leaves the secret or the
    challenge sitting in the store until its TTL runs out, which is the part a
    later holder of the same handle could still reach.
    """
    for key, prefix, store in _ceremony_slots(users, sessions):
        marker = session.pop(key, None)
        if store is None or not isinstance(marker, dict):
            continue
        handle = marker.get("id")
        if isinstance(handle, str) and handle:
            await store.delete(prefix + handle)
    # The WebAuthn half, whose challenge lives in a `ChallengeStore`. The
    # default key is popped even when no router is mounted to name it, so a
    # session never keeps a marker for a ceremony nothing can finish.
    handled = set()
    for key, store in _challenge_slots(users):
        handled.add(key)
        marker = session.pop(key, None)
        if not isinstance(marker, dict):
            continue
        handle = marker.get("id")
        if isinstance(handle, str) and handle:
            await store.discard(handle)
    if _WEBAUTHN_KEY not in handled:
        session.pop(_WEBAUTHN_KEY, None)


async def _factor_this_login_cannot_check(users: UserStore, user_id: str) -> str | None:
    """The kind of an enrolled factor this login path cannot check, or None.

    Asked only by a `user_router` that has no `second_factors=` of its own, of
    the routers mounted over the same `UserStore`. Recovery credentials do not
    count: they exist only alongside a real factor, they are what is left when
    one is removed, and no login prompt ever asks for one on its own -- so
    refusing on them would lock out an account nothing is protecting.
    """
    for wiring in _mounted_second_factors(users):
        for row in await wiring.factors.credentials(user_id):
            if row.kind != "recovery":
                return row.kind
    return None


async def _factor_another_store_holds(
    users: UserStore, user_id: str, consulted: SecondFactorStore
) -> str | None:
    """A factor enrolled in a store this login path did not consult, or None.

    The sibling of `_factor_this_login_cannot_check`, for the other way of
    half-wiring the two routers. That one covers `second_factors=None`; this one
    covers `second_factors=<the wrong store>` -- a `user_router` that was given
    a `SecondFactorStore` which is not the one the `second_factor_router` over
    the same `UserStore` writes to. The login then reads an empty store, finds
    nothing, and signs the caller in on a password alone: every signal saying
    protected, nothing being so, which is exactly the failure
    `second_factor_not_wired` exists to refuse. It is the likelier of the two
    mistakes, because building a store inline for each router reads as correct
    at the call site and only the object identity says otherwise.

    Wirings over the store that has **already been consulted** are skipped, so a
    correctly wired application asks no extra question of any store and the
    check costs one scan of a list that is usually one entry long.

    Recovery credentials do not count, for the reason
    `_factor_this_login_cannot_check` gives: they exist only alongside a real
    factor and no login prompt asks for one on its own.
    """
    for wiring in _mounted_second_factors(users):
        if _same_store(wiring.factors, consulted):
            # Already read by the login itself. `_same_store` rather than `is`
            # for the same reason as the user store: two `OrmSecondFactorStore`s
            # over one model are one store, and asking the second would cost a
            # query to re-read what the login has already seen.
            continue
        for row in await wiring.factors.credentials(user_id):
            if row.kind != "recovery":
                return row.kind
    return None


@dataclass(slots=True)
class RegisterInput:
    """The JSON body of `POST /register`.

    Bound with `Annotated[RegisterInput, Body()]`, which is the wreath
    convention throughout: a marker rides inside `Annotated` and a default stays
    an ordinary Python default (`limit: Annotated[int, Query()] = 20`, never
    `Query(20)`). Both members are required and must be JSON strings; a missing
    member, a wrong type, or any member not named here is refused with a 422
    `application/problem+json` whose `errors` list carries one entry per failure.

    No format or length rule is applied at binding time -- the email is not
    checked for shape at all, and the password's only bound is
    `wreath._userkit.MAX_PASSWORD_BYTES` (1024 UTF-8 bytes), which
    `hash_password` enforces later in the flow along with its refusal of an
    empty password.
    """

    email: str
    password: str


@dataclass(slots=True)
class LoginInput:
    """The JSON body of `POST /login`. Same binding rules as `RegisterInput`.

    Both shipped stores normalize the email to `strip().lower()`, and the
    router lower-cases it again before keying the login throttle, so
    `Ann@Example.com` and `ann@example.com` are one account and one bucket.
    """

    email: str
    password: str


@dataclass(slots=True)
class TokenInput:
    """The JSON body of `POST /verify`: one HMAC-signed verification token.

    Nothing about the token is validated at binding time beyond it being a
    string; `wreath._userkit.verify_token` checks the signature, the purpose,
    and the expiry, and an unusable token yields 400 with
    `{"status": "invalid_token"}` rather than a distinguishing error.
    """

    token: str


@dataclass(slots=True)
class ForgotInput:
    """The JSON body of `POST /forgot-password`: the address to send a link to.

    The address is not checked for shape or existence. The flow answers the same
    200 `{"status": "reset_email_sent"}` whether or not an account matches, so
    the response tells an attacker nothing. The work is kept comparable rather
    than identical: a miss still mints and discards a token, and the mail send
    on a hit remains a timing asymmetry the flow acknowledges in place.
    """

    email: str


@dataclass(slots=True)
class ResetInput:
    """The JSON body of `POST /reset-password`: a reset token and the new password.

    Both members are required strings. The token is single-use by construction --
    it is bound to a fingerprint of the password hash that was current when it
    was minted, so it stops working the moment the password changes.

    The new password carries the same 1024-byte bound and non-empty rule as
    registration, checked when it is hashed. The flow does not distinguish that
    refusal from a bad token: both answer 400 `{"status": "invalid_token"}`.
    """

    token: str
    password: str


@dataclass(slots=True)
class CodeInput:
    """The JSON body of the second-factor routes: one code the user typed.

    A single required string, bound the same way as every other body here. No
    shape is enforced at binding time on purpose -- the same member carries a
    six-digit TOTP code and a hyphenated recovery code, and the flows normalize
    whitespace, hyphens and case themselves before comparing. A code that is the
    wrong length is refused by `wreath._secondfactor.verify_totp` before any
    HMAC is computed, and answers exactly as a wrong code does.
    """

    code: str


@dataclass(slots=True)
class WebAuthnRegistrationInput:
    """The JSON body of `POST /auth/2fa/webauthn/confirm`.

    What a browser hands back from `navigator.credentials.create()`, with the
    two `ArrayBuffer`s base64url-encoded by the page's own script -- JSON has no
    byte string, and base64url is what WebAuthn already uses for every other
    binary member. `label` is what the user calls this key in the list.

    The credential id is deliberately **not** a member: it is inside the signed
    attestation object, and reading it from there rather than from a member the
    caller controls means the two cannot disagree.
    """

    client_data: str
    attestation_object: str
    label: str = ""


@dataclass(slots=True)
class WebAuthnAssertionInput:
    """The JSON body of `POST /auth/2fa/webauthn/verify`.

    What `navigator.credentials.get()` hands back, base64url-encoded the same
    way. `id` is the credential's own id, which is how the assertion says *which*
    of the caller's registered keys answered; the lookup it drives is scoped to
    the caller, so an id belonging to somebody else finds nothing.
    """

    id: str
    client_data: str
    authenticator_data: str
    signature: str


class LoginLimiter:
    """Counts failed sign-ins per identifier and refuses past a budget.

    The policy is a fixed window, not a sliding one: the window opens at the
    first counted failure, later failures increment the count without moving the
    start, and the identifier is refused once the count reaches `max_attempts`
    until `window` seconds have passed since that first failure. `allow` records
    nothing -- a caller must report the outcome with `record_failure` or
    `record_success` itself.

    Deliberately here rather than in `wreath._userkit`: that module is
    stdlib-only on purpose, and a limiter wants a bounded store. The primitive
    it guards -- `_userkit.authenticate` -- stays unthrottled, so an application
    calling it directly owns this decision itself.

    Keyed per identifier, so failing against one account cannot lock another
    out. `wreath.users.user_router` passes the submitted email, lower-cased and
    stripped, which makes the budget per account rather than per client: it
    slows password guessing against one account, and does nothing about one
    client spraying one password across many accounts. While an identifier is
    refused its legitimate owner is refused too, so an attacker who knows an
    address can keep that person out for one window.

    **Per-process and in-memory.** The counts live in a `CapabilityMap` inside
    this object, shared with nothing. Across N workers the real budget is
    N x `max_attempts`, a restart clears every count, and only `max_tracked`
    identifiers are counted at once. At the ceiling, unknown identifiers are
    refused for one window; live counters are never evicted by a key spray.

    Args:
        max_attempts: failed attempts allowed per identifier within one window.
        window: seconds the window lasts, timed from the first counted failure.
        max_tracked: identifiers counted at once, least-recently-used evicted first.
        clock: monotonic seconds source, injectable for deterministic tests.

    Raises:
        ValueError: max_attempts below 1, or a window that is not positive.
    """

    __slots__ = ("_attempts", "_clock", "_max", "_saturated_until", "_window")

    def __init__(
        self,
        *,
        max_attempts: int = 10,
        window: float = 300.0,
        max_tracked: int = 4096,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be a positive integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if (
            isinstance(window, bool)
            or not isinstance(window, (int, float))
            or not isfinite(window)
            or window <= 0
        ):
            raise ValueError("window must be positive and finite")
        self._max = max_attempts
        self._window = window
        self._clock = clock
        self._saturated_until = 0.0
        self._attempts = CapabilityMap(
            max_entries=max_tracked, ttl=window, clock=clock, overflow="refuse"
        )

    def allow(self, identifier: str) -> bool:
        """Whether another attempt may be made for `identifier` right now.

        A pure question: it counts nothing and starts no window. An identifier
        that has never failed, or whose window has elapsed, is allowed and its
        expired entry is dropped in passing.
        """
        entry = self._attempts.peek(identifier)
        if entry is None:
            return self._clock() >= self._saturated_until
        count, first = entry
        if self._clock() - first >= self._window:
            self._attempts.discard(identifier)
            return True
        return count < self._max

    def record_failure(self, identifier: str) -> None:
        """Count one failed attempt against `identifier`.

        The first failure, and the first after a window elapsed, opens a new
        window at the current clock reading. Every later failure inside that
        window increments the count and keeps the original start, which is what
        makes the window fixed rather than sliding.
        """
        entry = self._attempts.peek(identifier)
        now = self._clock()
        if entry is None or now - entry[1] >= self._window:
            if not self._attempts.put(identifier, (1, now)):
                self._saturated_until = max(self._saturated_until, now + self._window)
            return
        self._attempts.put(identifier, (entry[0] + 1, entry[1]), keep_deadline=True)

    def record_success(self, identifier: str) -> None:
        """Clear the failure count and window for `identifier`.

        The entry is dropped outright rather than decremented, so a user who
        mistypes twice and then signs in starts from a full budget. Nothing else
        is stored per identifier, so this leaves no state behind.
        """
        self._attempts.discard(identifier)


async def reset_password_endpoint(
    store: UserStore,
    sessions: Any = None,
    *,
    secret: str,
    token: str,
    new_password: str,
    session_key: str = "principal",
) -> bool:
    """Reset a password and end that user's other sessions.

    The second half is the point. A user resets a password *because* somebody
    else is in the account, and changing the credential only stops the next
    sign-in -- whoever is already holding a session keeps it. `sessions` is any
    session store. Missing `delete_for` wiring refuses before the password is
    changed, because reporting recovery while an attacker session survives is
    unsafe.

    Sessions are dropped only for the subject named in the token, and only after
    the reset succeeded: a token that fails verification changes no password and
    ends no session.

    `session_key` is the session key login wrote the principal under, and it is
    handed to the store rather than assumed: enumeration has to look where the
    principal actually is. It must match the key configured on the session
    store.

    Args:
        session_key: the session key holding the principal. Passed
            **positionally** to `sessions.delete_for`, and only when it is not
            the default, so a store written against the older one-argument
            signature is unaffected and one that named the parameter differently
            still works.

    Returns:
        True when the password changed, False for a bad token or a refused new password.
    """
    delete_for = None if sessions is None else getattr(sessions, "delete_for", None)
    if delete_for is None:
        raise RuntimeError(
            "password reset requires a session store with delete_for(subject) revocation"
        )
    subject = _userkit._token_subject(token)
    ok = await _userkit.reset_password(store, secret=secret, token=token, new_password=new_password)
    if not ok or subject is None:
        return ok
    if session_key == "principal":
        # The historical call, byte for byte, so every store that predates the
        # second parameter keeps working on the default wiring.
        await delete_for(subject)
    else:
        await delete_for(subject, session_key)
    return ok


def _profile(user: UserRecord) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "is_verified": user.is_verified}


def _default_link(base_url: str, prefix: str) -> Callable[[str, str], str]:
    root = base_url.rstrip("/") + prefix

    def build(purpose: str, token: str) -> str:
        if purpose == "verify":
            return f"{root}/verify/{token}"
        return f"{root}/reset-password?token={token}"

    return build


def user_router(
    store: UserStore,
    *,
    secret: str,
    email_sender: EmailSender | None = None,
    base_url: str = "",
    prefix: str = "/users",
    session_key: str = "principal",
    verify_ttl: int = 24 * 3600,
    reset_ttl: int = 3600,
    link_builder: Callable[[str, str], str] | None = None,
    sessions: Any = None,
    max_login_attempts: int = 10,
    login_window: float = 300.0,
    max_registration_attempts: int = 20,
    registration_window: float = 60.0,
    max_reset_requests: int = 3,
    reset_window: float = 15 * 60.0,
    second_factors: SecondFactorStore | None = None,
    pending_key: str = "pending_second_factor",
    clock: Callable[[], float] = time,
) -> Router:
    """Build a mountable `Router` with the full user lifecycle.

    Mounts, under `prefix`, `POST /register`, `/login`, `/logout`, `/verify`,
    `/forgot-password` and `/reset-password`, plus `GET /verify/{token}` and
    `GET /me`. Login writes the session principal the auth stack already reads,
    so it requires `SessionPolicy`; without it `/login` answers 500 with
    `{"error": "session_middleware_required"}` and signs nobody in.

    `secret` signs the action tokens (use a stable app secret). `link_builder`
    maps `(purpose, token) -> URL` for the emailed links; the default points at
    this router's own verify route and a `reset-password?token=` query.

    `sessions` is the required server-side session store. Its `delete_for`
    method lets a password reset end the sessions somebody else is already
    holding; construction refuses without that revocation seam. A
    non-default `session_key` is handed to `sessions.delete_for` so it
    enumerates by the key this router writes -- give the store the same key
    (`PostgresSessionStore(..., session_key=...)`) so a caller of `delete_for`
    outside this router agrees too. A store whose `delete_for` takes only a
    subject works unchanged on the default key and raises on a renamed one. It
    is also where login and logout delete a half-finished second-factor
    ceremony's row, though a `second_factor_router` mounted with its own
    `enrolments=` store is cleared there too, whichever store that is.

    `max_login_attempts`/`login_window` throttle failed sign-ins per identifier
    -- in this router only. `max_reset_requests`/`reset_window` independently
    bound reset-email issuance per normalized identifier while preserving the
    uniform response. `wreath._userkit.authenticate` stays unguarded for direct
    callers, and `LoginLimiter` documents what the throttle does and does not
    protect (in particular that it is per-process).

    No response here reveals whether an account exists. Register answers 202 and
    forgot-password answers 200 either way, a failed login answers 401
    `{"error": "invalid_credentials"}` whether the email is unknown or the
    password is wrong, and a throttled login reuses that same body with 429 plus
    a `Retry-After` of `login_window` seconds rather than saying the account is
    locked. The timing is kept comparable too — an unknown email still pays a
    dummy scrypt verify, and registration hashes before it checks for an
    existing account — but the emails these flows send are not queued, so a mail
    send remains an observable difference.

    Pass `second_factors` -- any `SecondFactorStore` -- and login grows a third
    outcome. A user with an enrolled factor is *authenticated but incomplete*:
    the response is 200 `{"status": "second_factor_required"}` carrying no
    profile, and the session holds a **pending** marker under `pending_key`
    rather than a principal. `SessionIdentityBackend` reads the principal, so
    that session is not an identity anywhere in the framework and no protected
    route will admit it. `wreath.users.second_factor_router`'s `POST /verify`
    is what turns it into one, and both routers must agree on `pending_key`.

    **Forgetting `second_factors` fails the login closed.** Mounting
    `second_factor_router` without passing its store here would otherwise let a
    user enrol a factor, see it listed, and then sign in with a password alone
    -- protected everywhere except where it counts. When this router has no
    store of its own and the account being signed in has a non-recovery factor
    enrolled in a `second_factor_router` built over **this same `UserStore`**,
    the login answers 500 `{"error": "second_factor_not_wired", "detail": ...}`
    and writes no session. The detail names what to pass. Yes, that locks out a
    misconfigured deployment; refusing rather than half-wiring is why that is the right way
    round, and a user with nothing enrolled is unaffected either way.

    **Signing in and signing out both end a half-finished ceremony.** A begun
    TOTP enrolment and a live WebAuthn challenge are cleared from the session,
    and the rows behind them deleted, at both ends -- a session is a sitting,
    and an unconfirmed secret that outlives one on a shared browser is state
    outliving the ceremony it belonged to.

    These handlers answer with plain JSON `{"status": ...}` / `{"error": ...}`
    bodies. Failures raised by the framework around them keep the standard
    shape -- a malformed or invalid body is an RFC 9457
    `application/problem+json`, 400 for unparseable JSON and 422 for a body that
    fails validation, the latter carrying an `errors` list of one entry per
    field.
    """
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("user_router secret must contain at least 32 bytes")
    if sessions is None or not callable(getattr(sessions, "delete_for", None)):
        raise ValueError(
            "user_router sessions must provide delete_for(subject) so password reset "
            "can revoke existing sessions"
        )
    if not callable(getattr(store, "compare_and_set_password", None)):
        raise ValueError(
            "user_router store must provide compare_and_set_password(user_id, expected, "
            "replacement) for single-use password reset tokens"
        )
    mailer = email_sender if email_sender is not None else LogEmailSender()
    links = link_builder if link_builder is not None else _default_link(base_url, prefix)
    router = Router(prefix=prefix, tags=("users",))
    limiter = LoginLimiter(max_attempts=max_login_attempts, window=login_window)
    reset_limiter = LoginLimiter(
        max_attempts=max_reset_requests,
        window=reset_window,
    )
    registration_limiter = LoginLimiter(
        max_attempts=max_registration_attempts,
        window=registration_window,
        max_tracked=1,
    )

    def _session(request: Any) -> dict[str, Any] | None:
        return getattr(request.state, "session", None)

    @router.post("/register")
    async def register(request: Any, data: Annotated[RegisterInput, Body()]):
        if not registration_limiter.allow("registration"):
            refused = JSONResponse({"status": "registration_received"}, status=429)
            refused.headers.append((b"retry-after", str(int(registration_window)).encode("ascii")))
            return refused
        registration_limiter.record_failure("registration")
        await _userkit.register(
            store,
            mailer,
            secret=secret,
            email=data.email,
            password=data.password,
            link_builder=links,
            ttl=verify_ttl,
        )
        # Uniform response — never reveals whether the email already existed.
        return JSONResponse({"status": "registration_received"}, status=202)

    @router.post("/login")
    async def login(request: Any, data: Annotated[LoginInput, Body()]):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        identifier = data.email.strip().lower()
        if not limiter.allow(identifier):
            # Deliberately the same shape as a wrong password plus a
            # Retry-After: saying "too many attempts for *this* account" would
            # confirm the account exists.
            refused = JSONResponse({"error": "invalid_credentials"}, status=429)
            refused.headers.append((b"retry-after", str(int(login_window)).encode("ascii")))
            return refused
        user = await _userkit.authenticate(store, data.email, data.password)
        if user is None:
            limiter.record_failure(identifier)
            return JSONResponse({"error": "invalid_credentials"}, status=401)
        limiter.record_success(identifier)
        rotate_session(request)
        # Whatever this session was before, it is not that any more. Both keys
        # are cleared before either is written: rotation mints a new id but
        # carries the contents over, so a principal left behind from an earlier
        # sign-in would survive a pending login and answer for the new user.
        session.pop(session_key, None)
        session.pop(pending_key, None)
        # A begun enrolment or a live WebAuthn challenge belongs to whoever
        # began it, and this session has just changed hands even if it is the
        # same person: signing in ends anything that was half-done before it.
        await _forget_ceremonies(session, store, sessions)
        if second_factors is not None:
            enrolled = await second_factors.credentials(user.id)
            if enrolled:
                methods = sorted({row.kind for row in enrolled})
                session[pending_key] = {
                    "sub": user.id,
                    "needs": methods,
                    "at": clock(),
                    # Belt and braces: if an application copies this payload to
                    # the principal key, `SessionIdentityBackend` still refuses
                    # to build an identity from it.
                    "pending": True,
                }
                return JSONResponse(
                    {"status": "second_factor_required", "methods": methods},
                    status=200,
                )
            # The store this login was given holds nothing for this account --
            # but a `second_factor_router` over the same `UserStore` may hold a
            # factor in a *different* store, which is the same half-wiring as
            # the `else` branch below reached by passing the argument rather
            # than by forgetting it. Refused for the same reason and with the
            # same answer: the account demonstrably has a factor and this login
            # path cannot check it.
            unseen = await _factor_another_store_holds(store, user.id, second_factors)
            if unseen is not None:
                return JSONResponse(
                    {
                        "error": "second_factor_not_wired",
                        "detail": (
                            f"this account has an enrolled {unseen} second "
                            "factor in a SecondFactorStore this login path does "
                            "not read: user_router was given a different store "
                            "from the one second_factor_router writes to. Build "
                            "the store once and pass that same object to both."
                        ),
                    },
                    status=500,
                )
        else:
            # Fail closed. The two routers are wired independently, so mounting
            # `second_factor_router` and forgetting `second_factors=` here gives
            # a user who enrols a factor, sees it listed, and is then signed in
            # by a password alone -- every signal saying protected, nothing
            # being so. This is the one moment where the mistake is knowable:
            # the account in hand demonstrably has a factor, and this login path
            # has nothing to check it with.
            # It locks out a misconfigured deployment, which is the trade
            # the refuse-rather-than-half-wire rule makes deliberately: a door that refuses and
            # names its own misconfiguration beats one that opens quietly. 500,
            # because the fault is the server's, and the same code as
            # `session_middleware_required` above, which is the same kind of
            # missing wiring.
            unchecked = await _factor_this_login_cannot_check(store, user.id)
            if unchecked is not None:
                return JSONResponse(
                    {
                        "error": "second_factor_not_wired",
                        "detail": (
                            f"this account has an enrolled {unchecked} second "
                            "factor and this login path cannot check it: "
                            "user_router was built without second_factors=. "
                            "Pass second_factors=<the same SecondFactorStore "
                            "given to second_factor_router> to user_router(), "
                            "and pending_key= if you changed it there."
                        ),
                    },
                    status=500,
                )
        session[session_key] = {"sub": user.id, "type": "User", "roles": []}
        return JSONResponse(_profile(user), status=200)

    @router.post("/logout")
    async def logout(request: Any):
        session = _session(request)
        if session is not None:
            session.pop(session_key, None)
            # A half-finished login is state too: leaving the pending marker
            # behind would let the next caller on this session finish somebody
            # else's sign-in by posting a code.
            session.pop(pending_key, None)
            # So is a half-finished enrolment. An unconfirmed TOTP secret that
            # survives a logout on a shared browser is a secret the next person
            # at that keyboard can confirm onto their own account.
            await _forget_ceremonies(session, store, sessions)
        return JSONResponse({"status": "logged_out"}, status=200)

    @router.post("/verify")
    async def verify(request: Any, data: Annotated[TokenInput, Body()]):
        ok = await _userkit.verify_email(store, secret=secret, token=data.token)
        return JSONResponse(
            {"status": "verified" if ok else "invalid_token"}, status=200 if ok else 400
        )

    @router.get("/verify/{token}")
    async def verify_link(request: Any, token: Annotated[str, Path()]):
        ok = await _userkit.verify_email(store, secret=secret, token=token)
        return JSONResponse(
            {"status": "verified" if ok else "invalid_token"}, status=200 if ok else 400
        )

    @router.post("/forgot-password")
    async def forgot(request: Any, data: Annotated[ForgotInput, Body()]):
        identifier = data.email.strip().lower()
        if reset_limiter.allow(identifier):
            # Count issuance attempts whether or not the account exists, keeping
            # both the response and the work decision free of enumeration clues.
            reset_limiter.record_failure(identifier)
            await _userkit.start_password_reset(
                store,
                mailer,
                secret=secret,
                email=data.email,
                link_builder=links,
                ttl=reset_ttl,
            )
        # Uniform response for absent accounts and exhausted issuance budgets.
        return JSONResponse({"status": "reset_email_sent"}, status=200)

    @router.post("/reset-password")
    async def reset(request: Any, data: Annotated[ResetInput, Body()]):
        ok = await reset_password_endpoint(
            store,
            sessions,
            secret=secret,
            token=data.token,
            new_password=data.password,
            session_key=session_key,
        )
        return JSONResponse(
            {"status": "password_reset" if ok else "invalid_token"}, status=200 if ok else 400
        )

    @router.get("/me")
    async def me(request: Any):
        session = _session(request)
        principal = session.get(session_key) if session else None
        if not principal:
            return JSONResponse({"error": "not_authenticated"}, status=401)
        user = await store.get_by_id(str(principal.get("sub")))
        if user is None:
            return JSONResponse({"error": "not_authenticated"}, status=401)
        return JSONResponse(_profile(user), status=200)

    return router


@dataclass(frozen=True, slots=True)
class _SecondFactorBuildContext:
    values: dict[str, Any]


def _make_second_factor_context(
    *,
    router: Router,
    users: UserStore,
    factors: SecondFactorStore,
    issuer: str,
    session_key: str,
    pending_key: str,
    enrolment_key: str,
    label: str,
    digits: int,
    period: int,
    skew: int,
    recovery_codes: int,
    enrolment_ttl: float,
    pending_ttl: float,
    step_up_ttl: float,
    verify_window: float,
    enrolments: Any,
    challenges: Any,
    rp_id: str,
    rp_name: str,
    accepted_origins: tuple[str, ...],
    webauthn_key: str,
    webauthn_label: str,
    webauthn_ttl: float,
    user_verification: str,
    passkey_login: bool,
    clock: Callable[[], float],
    limiter: LoginLimiter,
    discoverable_factors: DiscoverableSecondFactorStore,
    ceremony_kinds: dict[str, str],
) -> _SecondFactorBuildContext:
    _CEREMONY_KINDS = ceremony_kinds

    def _session(request: Any) -> dict[str, Any] | None:
        return getattr(request.state, "session", None)

    def _throttled() -> JSONResponse:
        refused = JSONResponse({"error": "invalid_code"}, status=429)
        refused.headers.append((b"retry-after", str(int(verify_window)).encode("ascii")))
        return refused

    def _principal(session: dict[str, Any]) -> dict[str, Any] | None:
        principal = session.get(session_key)
        if not isinstance(principal, dict) or principal.get("pending"):
            return None
        subject = principal.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        return principal

    async def _signed_in(session: dict[str, Any]) -> UserRecord | None:
        principal = _principal(session)
        if principal is None:
            return None
        return await users.get_by_id(str(principal["sub"]))

    def _stale_step_up(session: dict[str, Any]) -> bool:
        """Whether no factor has been proved on this session within the window.

        The one reading of `second_factor_at`, so `DELETE` and the enrolment
        routes cannot drift apart on what counts as recent. A `bool` is refused
        before the numeric test because `True` is an `int` and would otherwise
        read as one second past the epoch -- ancient, but the arithmetic below
        would still run on it.
        """
        principal = _principal(session)
        stamp = None if principal is None else principal.get("second_factor_at")
        return (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or clock() - stamp > step_up_ttl
        )

    def _step_up_required() -> JSONResponse:
        return JSONResponse({"error": "second_factor_required"}, status=403)

    def _may_enrol(session: dict[str, Any], enrolled: Any) -> bool:
        """Whether this session may add a factor, given what the account holds.

        **Adding a factor to an account that already has one is a step-up.**
        Declining to *stamp* such an enrolment is not enough on its own: the
        caller can register a passkey of their own and then prove it at
        `POST /webauthn/verify` a request later, which does stamp, and walk
        through `DELETE /{factor_id}` with the victim's real factor. The detour
        costs one round trip, so the enrolment itself is what has to be refused.

        The *first* factor is exempt, because there is nothing to step up from
        and the account would otherwise be unable to enrol at all. Recovery
        codes do not count as a factor here: they are minted alongside TOTP and
        never on their own, so an account holding only them is in the same
        position as an account holding nothing.
        """
        return not any(row.kind != "recovery" for row in enrolled) or not _stale_step_up(session)

    def _stamp(session: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
        """Record on the principal that a second factor was just proved.

        An integer of Unix seconds, because it is serialized into the session
        and then read back as a Cedar context value, and Cedar has no floats.
        The principal is reassigned rather than mutated in place so that the
        session middleware sees a changed session and rewrites the cookie.

        `extra` carries whatever else the factor that was proved has to say --
        for WebAuthn, `second_factor_uv`, the user-verification outcome.
        """
        principal = session.get(session_key)
        if isinstance(principal, dict):
            stamped = {**principal, "second_factor_at": int(clock())}
            if extra:
                stamped.update(extra)
            session[session_key] = stamped

    async def _save_record(
        session: dict[str, Any], key: str, prefix: str, record: dict[str, Any], ttl: float
    ) -> None:
        """Park a begun enrolment server-side, under an opaque handle.

        **Never in the session.** With `enrolments=` the unconfirmed record goes
        there; otherwise it goes to the `ChallengeStore`. The cookie carries an
        opaque handle and no TOTP secret in both cases.
        """
        handle = _secondfactor.new_credential_id()
        if enrolments is not None:
            await enrolments.save(prefix + handle, record, int(ttl))
        else:
            await challenges.put(
                handle,
                user_id=str(record.get("user", "")),
                kind=CHALLENGE_ENROLMENT,
                payload=record,
                ttl=ttl,
            )
        session[key] = {"id": handle, "at": record["at"]}

    async def _load_record(marker: dict[str, Any], prefix: str) -> dict[str, Any] | None:
        """The begun enrolment behind a session marker, wherever it is kept.

        A *read*, deliberately, and not a consume. An enrolment's property is
        "confirmed exactly once", not "read exactly once": the digits are typed
        by a human off an authenticator, and spending the secret on a mistyped
        one would send them back to scanning a fresh QR code. `peek` is the
        non-consuming read that says so in its own name.
        """
        handle = marker.get("id")
        if not isinstance(handle, str) or not handle:
            return None
        if enrolments is not None:
            loaded = await enrolments.load(prefix + handle)
            return loaded if isinstance(loaded, dict) else None
        row = await challenges.peek(handle)
        if row is None or row.kind != CHALLENGE_ENROLMENT:
            return None
        return row.payload

    async def _forget_record(
        session: dict[str, Any], key: str, prefix: str, marker: dict[str, Any]
    ) -> None:
        """Drop a begun ceremony, from the session and from the store behind it.

        Called on every exit from a `confirm` -- success, expiry, and each
        refusal -- because state that outlives the attempt it belonged to is
        exactly what moving it server-side was meant to bound. For a TOTP
        enrolment that is an unconfirmed secret; for WebAuthn it is the
        challenge, and there it is the single-use property itself.
        """
        session.pop(key, None)
        handle = marker.get("id")
        if not isinstance(handle, str) or not handle:
            return
        if enrolments is not None:
            await enrolments.delete(prefix + handle)
        else:
            await challenges.discard(handle)

    async def _load_enrolment(marker: dict[str, Any]) -> dict[str, Any] | None:
        return await _load_record(marker, _ENROLMENT_PREFIX)

    async def _forget_enrolment(session: dict[str, Any], marker: dict[str, Any]) -> None:
        await _forget_record(session, enrolment_key, _ENROLMENT_PREFIX, marker)

    return _SecondFactorBuildContext(
        {
            "_session": _session,
            "_throttled": _throttled,
            "_principal": _principal,
            "_signed_in": _signed_in,
            "_stale_step_up": _stale_step_up,
            "_step_up_required": _step_up_required,
            "_may_enrol": _may_enrol,
            "_stamp": _stamp,
            "_save_record": _save_record,
            "_load_record": _load_record,
            "_forget_record": _forget_record,
            "_load_enrolment": _load_enrolment,
            "_forget_enrolment": _forget_enrolment,
            "router": router,
            "users": users,
            "factors": factors,
            "issuer": issuer,
            "session_key": session_key,
            "pending_key": pending_key,
            "enrolment_key": enrolment_key,
            "label": label,
            "digits": digits,
            "period": period,
            "skew": skew,
            "recovery_codes": recovery_codes,
            "enrolment_ttl": enrolment_ttl,
            "pending_ttl": pending_ttl,
            "step_up_ttl": step_up_ttl,
            "verify_window": verify_window,
            "enrolments": enrolments,
            "challenges": challenges,
            "rp_id": rp_id,
            "rp_name": rp_name,
            "accepted_origins": accepted_origins,
            "webauthn_key": webauthn_key,
            "webauthn_label": webauthn_label,
            "webauthn_ttl": webauthn_ttl,
            "user_verification": user_verification,
            "passkey_login": passkey_login,
            "clock": clock,
            "limiter": limiter,
            "discoverable_factors": discoverable_factors,
            "_CEREMONY_KINDS": _CEREMONY_KINDS,
        }
    )


def _mount_totp_enrolment_routes(
    context: _SecondFactorBuildContext,
) -> None:
    values = context.values
    (
        _session,
        _throttled,
        _principal,
        _signed_in,
        _stale_step_up,
        _step_up_required,
        _may_enrol,
        _stamp,
        _save_record,
        _load_record,
        _forget_record,
        _load_enrolment,
        _forget_enrolment,
        router,
        _users,
        factors,
        issuer,
        _session_key,
        _pending_key,
        enrolment_key,
        label,
        digits,
        period,
        skew,
        recovery_codes,
        enrolment_ttl,
        _pending_ttl,
        _step_up_ttl,
        _verify_window,
        _enrolments,
        _challenges,
        _rp_id,
        _rp_name,
        _accepted_origins,
        _webauthn_key,
        _webauthn_label,
        _webauthn_ttl,
        _user_verification,
        _passkey_login,
        clock,
        limiter,
        _discoverable_factors,
        _CEREMONY_KINDS,
    ) = (
        values["_session"],
        values["_throttled"],
        values["_principal"],
        values["_signed_in"],
        values["_stale_step_up"],
        values["_step_up_required"],
        values["_may_enrol"],
        values["_stamp"],
        values["_save_record"],
        values["_load_record"],
        values["_forget_record"],
        values["_load_enrolment"],
        values["_forget_enrolment"],
        values["router"],
        values["users"],
        values["factors"],
        values["issuer"],
        values["session_key"],
        values["pending_key"],
        values["enrolment_key"],
        values["label"],
        values["digits"],
        values["period"],
        values["skew"],
        values["recovery_codes"],
        values["enrolment_ttl"],
        values["pending_ttl"],
        values["step_up_ttl"],
        values["verify_window"],
        values["enrolments"],
        values["challenges"],
        values["rp_id"],
        values["rp_name"],
        values["accepted_origins"],
        values["webauthn_key"],
        values["webauthn_label"],
        values["webauthn_ttl"],
        values["user_verification"],
        values["passkey_login"],
        values["clock"],
        values["limiter"],
        values["discoverable_factors"],
        values["_CEREMONY_KINDS"],
    )

    @router.post("/totp/begin")
    async def totp_begin(request: Any):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        user = await _signed_in(session)
        if user is None:
            return JSONResponse({"error": "not_authenticated"}, status=401)
        enrolled = await factors.credentials(user.id)
        if any(row.kind == "totp" for row in enrolled):
            # Enrolling twice would mint a second secret and a second set of
            # recovery codes, invalidating neither -- so it is refused rather
            # than quietly leaving the user with two of everything.
            # Ahead of the step-up check below because it is the more specific
            # answer and costs nothing to give: `GET /auth/2fa` already lists
            # this account's factors to any signed-in session, so naming the
            # collision tells a caller nothing they could not already read.
            return JSONResponse({"error": "already_enrolled"}, status=409)
        if not _may_enrol(session, enrolled):
            return _step_up_required()
        enrolment = _secondfactor.begin_totp_enrolment(
            account=user.email,
            issuer=issuer,
            label=label,
            digits=digits,
            period=period,
        )
        # The unconfirmed secret has to survive one round trip, and where it
        # waits is a real choice. With `enrolments`, it waits server-side and
        # the cookie carries an opaque id: the response below hands the same
        # secret to the same browser to be scanned, but a response body is
        # transient where a cookie is written down, and a cookie can outlive the
        # tab on a shared machine. Without a store it stays in the session,
        # which is the stage-one behaviour and is only as server-side as the
        # session middleware is.
        # It is not a credential in either case: nothing in the store refers to
        # it, and nothing ever will unless a code generated from it verifies.
        # `user` is recorded with it so a session that changes hands -- signed
        # out and signed in as somebody else -- cannot confirm an enrolment that
        # was begun for a different account.
        record = {"secret": enrolment.secret_base32, "user": user.id, "at": clock()}
        await _save_record(session, enrolment_key, _ENROLMENT_PREFIX, record, enrolment_ttl)
        return JSONResponse(
            {
                "uri": enrolment.uri,
                "secret": enrolment.secret_base32,
                "digits": enrolment.digits,
                "period": enrolment.period,
            },
            status=200,
        )

    @router.post("/totp/confirm")
    async def totp_confirm(request: Any, data: Annotated[CodeInput, Body()]):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        user = await _signed_in(session)
        if user is None:
            return JSONResponse({"error": "not_authenticated"}, status=401)
        marker = session.get(enrolment_key)
        if not isinstance(marker, dict):
            return JSONResponse({"error": "no_enrolment_in_progress"}, status=400)
        started = marker.get("at")
        if not isinstance(started, (int, float)) or clock() - started > enrolment_ttl:
            await _forget_enrolment(session, marker)
            return JSONResponse({"error": "enrolment_expired"}, status=400)
        pending = await _load_enrolment(marker)
        if pending is None:
            # The stored row is gone or was never written: expired under the
            # store's own TTL, or revoked. Same answer as an expired marker.
            await _forget_enrolment(session, marker)
            return JSONResponse({"error": "enrolment_expired"}, status=400)
        if pending.get("user") != user.id:
            # This session began an enrolment for somebody else and has since
            # changed hands. Confirming it would enrol a secret the previous
            # holder chose onto this account.
            await _forget_enrolment(session, marker)
            return JSONResponse({"error": "no_enrolment_in_progress"}, status=400)
        if not isinstance(pending.get("secret"), str):
            await _forget_enrolment(session, marker)
            return JSONResponse({"error": "no_enrolment_in_progress"}, status=400)
        if not limiter.allow(user.id):
            return _throttled()
        try:
            secret = _secondfactor.base32_to_secret(pending["secret"])
        except ValueError:
            # Only reachable from a session written by an older or different
            # build of this router: this one signs what it wrote. Drop it and
            # make the user start again rather than answering 500.
            await _forget_enrolment(session, marker)
            return JSONResponse({"error": "no_enrolment_in_progress"}, status=400)
        enrolled = await factors.credentials(user.id)
        if any(row.kind == "totp" for row in enrolled):
            await _forget_enrolment(session, marker)
            return JSONResponse({"error": "already_enrolled"}, status=409)
        if not _may_enrol(session, enrolled):
            # Re-checked at the write, not just at `begin`: a ceremony begun
            # while the stamp was fresh must not be confirmable after it has
            # gone stale, and the session may have changed hands in between.
            await _forget_enrolment(session, marker)
            return _step_up_required()
        # Whether this is the *first* real factor decides whether confirming it
        # may stamp the session. See `_stamp`'s caller below.
        first_factor = not any(row.kind != "recovery" for row in enrolled)
        confirmed = await _secondfactor.confirm_totp_enrolment(
            factors,
            user.id,
            secret=secret,
            code=data.code,
            label=label,
            digits=digits,
            period=period,
            skew=skew,
            recovery_codes=recovery_codes,
            at=clock(),
        )
        if confirmed is None:
            limiter.record_failure(user.id)
            # The enrolment survives a wrong code -- a mistyped digit should not
            # send the user back to scanning a new QR code -- but the throttle
            # above bounds how many times that is worth trying.
            return JSONResponse({"error": "invalid_code"}, status=400)
        limiter.record_success(user.id)
        await _forget_enrolment(session, marker)
        credential, codes = confirmed
        # Confirming the user's **first** factor stamps the session: a code out
        # of the authenticator was just checked, and without this a user who has
        # only ever enrolled would have to step up separately before they could
        # remove what they just added.
        # Enrolling an *additional* factor stamps nothing, and that is the whole
        # point of the condition. A stamp is the answer to "did this caller
        # prove a factor the account already had", and a factor the caller has
        # just chosen answers it with a secret they brought themselves --
        # so somebody holding a stolen session could enrol their own
        # authenticator, be stamped for it, and then satisfy `DELETE /{id}` and
        # every `@second_factor(...)` route with the guard they were supposed
        # not to be able to pass.
        if first_factor:
            _stamp(session)
        # The only time the plaintext recovery codes exist outside the user's
        # own hands. They are not stored, not logged, and not retrievable.
        return JSONResponse(
            {"status": "enrolled", "id": credential.id, "recovery_codes": codes},
            status=200,
        )


def _mount_recovery_routes(
    context: _SecondFactorBuildContext,
) -> Callable[[dict[str, Any]], str | None]:
    values = context.values
    (
        _session,
        _throttled,
        _principal,
        _signed_in,
        _stale_step_up,
        _step_up_required,
        _may_enrol,
        _stamp,
        _save_record,
        _load_record,
        _forget_record,
        _load_enrolment,
        _forget_enrolment,
        router,
        users,
        factors,
        _issuer,
        session_key,
        pending_key,
        _enrolment_key,
        _label,
        digits,
        period,
        skew,
        _recovery_codes,
        _enrolment_ttl,
        pending_ttl,
        _step_up_ttl,
        _verify_window,
        _enrolments,
        _challenges,
        _rp_id,
        _rp_name,
        _accepted_origins,
        _webauthn_key,
        _webauthn_label,
        _webauthn_ttl,
        _user_verification,
        _passkey_login,
        clock,
        limiter,
        _discoverable_factors,
        _CEREMONY_KINDS,
    ) = (
        values["_session"],
        values["_throttled"],
        values["_principal"],
        values["_signed_in"],
        values["_stale_step_up"],
        values["_step_up_required"],
        values["_may_enrol"],
        values["_stamp"],
        values["_save_record"],
        values["_load_record"],
        values["_forget_record"],
        values["_load_enrolment"],
        values["_forget_enrolment"],
        values["router"],
        values["users"],
        values["factors"],
        values["issuer"],
        values["session_key"],
        values["pending_key"],
        values["enrolment_key"],
        values["label"],
        values["digits"],
        values["period"],
        values["skew"],
        values["recovery_codes"],
        values["enrolment_ttl"],
        values["pending_ttl"],
        values["step_up_ttl"],
        values["verify_window"],
        values["enrolments"],
        values["challenges"],
        values["rp_id"],
        values["rp_name"],
        values["accepted_origins"],
        values["webauthn_key"],
        values["webauthn_label"],
        values["webauthn_ttl"],
        values["user_verification"],
        values["passkey_login"],
        values["clock"],
        values["limiter"],
        values["discoverable_factors"],
        values["_CEREMONY_KINDS"],
    )

    @router.get("/")
    async def factor_list(request: Any):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        user = await _signed_in(session)
        if user is None:
            return JSONResponse({"error": "not_authenticated"}, status=401)
        rows = await factors.credentials(user.id)
        return JSONResponse(
            {
                # Labels and dates only. `material` is the shared secret for a
                # TOTP factor and a password hash for a recovery code; neither
                # is ever rendered, by this route or any other.
                "factors": [
                    {
                        "id": row.id,
                        "kind": row.kind,
                        "label": row.label,
                        "created_at": row.created_at.isoformat(),
                        "last_used_at": (
                            None if row.last_used_at is None else row.last_used_at.isoformat()
                        ),
                    }
                    for row in rows
                    if row.kind != "recovery"
                ],
                # Counted rather than listed: an individual recovery code has no
                # label worth showing, and how many are left is the useful fact.
                "recovery_codes_remaining": sum(1 for row in rows if row.kind == "recovery"),
            },
            status=200,
        )

    @router.post("/verify")
    async def verify_factor(request: Any, data: Annotated[CodeInput, Body()]):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        pending = session.get(pending_key)
        if not isinstance(pending, dict):
            # No half-finished login, so this is step-up: an already
            # authenticated caller proving a factor again before something that
            # demands a recent one.
            return await _step_up(request, session, data.code)
        subject = pending.get("sub")
        if not isinstance(subject, str) or not subject:
            session.pop(pending_key, None)
            return JSONResponse({"error": "no_pending_second_factor"}, status=401)
        started = pending.get("at")
        if not isinstance(started, (int, float)) or clock() - started > pending_ttl:
            # A pending login is a password already accepted, waiting. Left
            # open it is a standing invitation to brute-force the second half,
            # so it expires on its own.
            session.pop(pending_key, None)
            return JSONResponse({"error": "second_factor_expired"}, status=401)
        if not limiter.allow(subject):
            return _throttled()
        matched = await _secondfactor.verify_second_factor(
            factors,
            subject,
            data.code,
            at=clock(),
            period=period,
            digits=digits,
            skew=skew,
        )
        if matched is None:
            limiter.record_failure(subject)
            return JSONResponse({"error": "invalid_code"}, status=401)
        user = await users.get_by_id(subject)
        if user is None or not user.is_active:
            session.pop(pending_key, None)
            return JSONResponse({"error": "invalid_code"}, status=401)
        limiter.record_success(subject)
        session.pop(pending_key, None)
        # Promotion from pending to full is a privilege change, and so carries
        # exactly the fixation risk login does; `user_router` rotates there and
        # this rotates here.
        rotate_session(request)
        session[session_key] = {
            "sub": user.id,
            "type": "User",
            "roles": [],
            "second_factor_at": int(clock()),
        }
        return JSONResponse(_profile(user), status=200)

    async def _step_up(request: Any, session: dict[str, Any], code: str) -> JSONResponse:
        """Re-prove a factor on a session that is already signed in.

        The same code check as promotion, the same per-user throttle, and the
        same rotation: gaining the right to perform a destructive action is a
        privilege change too, so the id that holds it is not the id that was
        being watched a moment earlier.

        A caller with no factors enrolled is told so plainly rather than being
        given a code prompt it can never satisfy -- there is nothing to guess
        at, so this reveals nothing a `GET /auth/2fa` would not.
        """
        principal = _principal(session)
        if principal is None:
            return JSONResponse({"error": "no_pending_second_factor"}, status=401)
        subject = str(principal["sub"])
        if not limiter.allow(subject):
            return _throttled()
        if not await factors.credentials(subject):
            return JSONResponse({"error": "no_second_factor_enrolled"}, status=400)
        matched = await _secondfactor.verify_second_factor(
            factors,
            subject,
            code,
            at=clock(),
            period=period,
            digits=digits,
            skew=skew,
        )
        if matched is None:
            limiter.record_failure(subject)
            return JSONResponse({"error": "invalid_code"}, status=401)
        limiter.record_success(subject)
        rotate_session(request)
        _stamp(session)
        return JSONResponse({"status": "second_factor_verified"}, status=200)

    def _pending_subject(session: dict[str, Any]) -> str | None:
        """The subject of a live pending login on this session, or None.

        None covers three cases on purpose -- no pending login, one with no
        subject, and one that has waited longer than `pending_ttl` -- because
        every caller treats all three the same way: there is nobody here whose
        half-finished sign-in may be completed.
        """
        pending = session.get(pending_key)
        if not isinstance(pending, dict):
            return None
        subject = pending.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        started = pending.get("at")
        if not isinstance(started, (int, float)) or clock() - started > pending_ttl:
            return None
        return subject

    return _pending_subject


def _mount_webauthn_routes(
    context: _SecondFactorBuildContext,
    pending_subject: Callable[[dict[str, Any]], str | None],
) -> None:
    values = context.values
    (
        _session,
        _throttled,
        _principal,
        _signed_in,
        _stale_step_up,
        _step_up_required,
        _may_enrol,
        _stamp,
        _save_record,
        _load_record,
        _forget_record,
        _load_enrolment,
        _forget_enrolment,
        _router,
        _users,
        _factors,
        _issuer,
        _session_key,
        _pending_key,
        _enrolment_key,
        _label,
        _digits,
        _period,
        _skew,
        _recovery_codes,
        _enrolment_ttl,
        _pending_ttl,
        _step_up_ttl,
        _verify_window,
        _enrolments,
        challenges,
        _rp_id,
        _rp_name,
        _accepted_origins,
        webauthn_key,
        _webauthn_label,
        webauthn_ttl,
        _user_verification,
        passkey_login,
        clock,
        _limiter,
        _discoverable_factors,
        _CEREMONY_KINDS,
    ) = (
        values["_session"],
        values["_throttled"],
        values["_principal"],
        values["_signed_in"],
        values["_stale_step_up"],
        values["_step_up_required"],
        values["_may_enrol"],
        values["_stamp"],
        values["_save_record"],
        values["_load_record"],
        values["_forget_record"],
        values["_load_enrolment"],
        values["_forget_enrolment"],
        values["router"],
        values["users"],
        values["factors"],
        values["issuer"],
        values["session_key"],
        values["pending_key"],
        values["enrolment_key"],
        values["label"],
        values["digits"],
        values["period"],
        values["skew"],
        values["recovery_codes"],
        values["enrolment_ttl"],
        values["pending_ttl"],
        values["step_up_ttl"],
        values["verify_window"],
        values["enrolments"],
        values["challenges"],
        values["rp_id"],
        values["rp_name"],
        values["accepted_origins"],
        values["webauthn_key"],
        values["webauthn_label"],
        values["webauthn_ttl"],
        values["user_verification"],
        values["passkey_login"],
        values["clock"],
        values["limiter"],
        values["discoverable_factors"],
        values["_CEREMONY_KINDS"],
    )
    _pending_subject = pending_subject
    # Mounted only when there is a relying party to be. See the docstring:
    # an application that does not want passkeys gets no passkey routes,
    # rather than routes that answer 500 to the first person who finds them.

    async def _take_ceremony(
        session: dict[str, Any], marker: dict[str, Any], *, subject: str, kind: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """Spend a begun ceremony, or refuse it without spending anything.

        `subject` is a **precondition** of the consume, never something read
        back out of the record. Consuming in order to learn who you are
        makes the record its own authority: whoever holds a handle would
        define who they are. So the caller derives the candidate from the
        session first and the statement matches on it, which means a
        mismatched attempt matches no row and costs the rightful user
        nothing.

        The returned payload *is* the consumption -- one DELETE ...
        RETURNING, so two concurrent completions cannot both proceed. A
        challenge that survived a failed attempt would let a recorded
        assertion be posted again until it expired, so this spends on sight;
        a caller whose ceremony failed begins another one.

        Returns `(record, mismatched)`. `mismatched` says the row was there
        and belonged to somebody else, which is the only reason the two
        refusals answer differently: an expired ceremony is a 400, and
        somebody else's is the binding refusing.
        """
        handle = marker.get("id")
        session.pop(webauthn_key, None)
        if not isinstance(handle, str) or not handle:
            return None, False
        record = await challenges.consume(handle, user_id=subject, kind=kind)
        if record is not None:
            return record, False
        # Diagnostic only, and only after the refusal has already happened:
        # it chooses which *error code* answers, never whether the caller
        # may proceed. The row may still be reachable from the rightful
        # session: a copied marker must not let this caller discard it.
        row = await challenges.peek(handle)
        # The consume has already refused, so a live row under this handle
        # means either the wrong ceremony or the wrong person. Only the
        # second is the *binding* refusing; a registration challenge posted
        # to `verify` is simply not the ceremony that was begun, and
        # `ceremony_expired` says so. Testing the kind as well would be a
        # clause no reachable state can distinguish.
        return None, row is not None and row.user_id != subject

    async def _forget_ceremony(session: dict[str, Any]) -> None:
        """Drop whatever ceremony this session had, row included."""
        previous = session.pop(webauthn_key, None)
        if isinstance(previous, dict):
            handle = previous.get("id")
            if isinstance(handle, str) and handle:
                await challenges.discard(handle)

    async def _begin_ceremony(
        session: dict[str, Any], ceremony: WebAuthnCeremony, subject: str
    ) -> JSONResponse:
        # One live challenge per session. Beginning a second ceremony
        # abandons the first rather than leaving its row to sit out its TTL,
        # so a caller who reloads the page ten times leaves one challenge
        # behind instead of ten.
        await _forget_ceremony(session)
        handle = _secondfactor.new_credential_id()
        # Bound to whoever began it, in the row rather than beside it. A
        # session that changes hands -- signed out and signed in as somebody
        # else -- carries its contents across rotation, so without this the
        # new holder could finish the previous one's ceremony. Because the
        # binding is a condition of the consuming statement, that attempt
        # now matches no row at all instead of being caught afterwards.
        await challenges.put(
            handle,
            user_id=subject,
            kind=_CEREMONY_KINDS[ceremony.ceremony],
            payload={"challenge": b64url_encode(ceremony.challenge)},
            ttl=webauthn_ttl,
        )
        # The cookie carries an opaque handle and nothing else: no challenge,
        # no subject. There is nothing in it worth replaying.
        session[webauthn_key] = {"id": handle, "at": clock()}
        return JSONResponse(ceremony.options, status=200)

    ceremony_context = _SecondFactorBuildContext(
        values
        | {
            "_begin_ceremony": _begin_ceremony,
            "_forget_ceremony": _forget_ceremony,
            "_pending_subject": pending_subject,
            "_take_ceremony": _take_ceremony,
        }
    )
    _mount_webauthn_registration(ceremony_context)
    if passkey_login:
        _mount_passkey_login(ceremony_context)
    _mount_webauthn_verification(ceremony_context)


def _second_factor_context_values(
    context: _SecondFactorBuildContext, names: tuple[str, ...]
) -> tuple[Any, ...]:
    return tuple(context.values[name] for name in names)


def _mount_webauthn_registration(context: _SecondFactorBuildContext) -> None:
    (
        router,
        _session,
        _signed_in,
        factors,
        _may_enrol,
        _step_up_required,
        rp_id,
        rp_name,
        passkey_login,
        user_verification,
        _begin_ceremony,
        webauthn_key,
        _take_ceremony,
        _stamp,
        accepted_origins,
        webauthn_label,
        clock,
        recovery_codes,
    ) = _second_factor_context_values(
        context,
        (
            "router",
            "_session",
            "_signed_in",
            "factors",
            "_may_enrol",
            "_step_up_required",
            "rp_id",
            "rp_name",
            "passkey_login",
            "user_verification",
            "_begin_ceremony",
            "webauthn_key",
            "_take_ceremony",
            "_stamp",
            "accepted_origins",
            "webauthn_label",
            "clock",
            "recovery_codes",
        ),
    )

    @router.post("/webauthn/begin")
    async def webauthn_begin(request: Any):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        user = await _signed_in(session)
        if user is None:
            return JSONResponse({"error": "not_authenticated"}, status=401)
        existing = await factors.credentials(user.id)
        if not _may_enrol(session, existing):
            return _step_up_required()
        ceremony = _secondfactor.begin_webauthn_registration(
            user_id=user.id,
            account=user.email,
            rp_id=rp_id,
            rp_name=rp_name,
            existing=existing,
            user_verification=("required" if passkey_login else user_verification),
            discoverable=passkey_login,
        )
        return await _begin_ceremony(session, ceremony, user.id)

    @router.post("/webauthn/confirm")
    async def webauthn_confirm(request: Any, data: Annotated[WebAuthnRegistrationInput, Body()]):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        user = await _signed_in(session)
        if user is None:
            return JSONResponse({"error": "not_authenticated"}, status=401)
        marker = session.get(webauthn_key)
        if not isinstance(marker, dict):
            return JSONResponse({"error": "no_ceremony_in_progress"}, status=400)
        # The signed-in user is the precondition. A ceremony begun for
        # somebody else matches no row, so it is refused by the statement
        # rather than after the record has already been read back.
        record, mismatched = await _take_ceremony(
            session, marker, subject=user.id, kind=CHALLENGE_WEBAUTHN_REGISTER
        )
        if mismatched:
            # Begun for somebody else; this session has changed hands since.
            return JSONResponse({"error": "no_ceremony_in_progress"}, status=400)
        if record is None:
            return JSONResponse({"error": "ceremony_expired"}, status=400)
        # Read before the registration writes, for the same reason
        # `totp/confirm` reads before its own: only the *first* factor a
        # user has may stamp the session.
        existing = await factors.credentials(user.id)
        if not _may_enrol(session, existing):
            # Re-checked at the write, as `totp/confirm` re-checks its own:
            # a ceremony begun on a fresh stamp must not land on a stale one.
            return _step_up_required()
        first_factor = not any(row.kind != "recovery" for row in existing)
        try:
            credential, codes = await _secondfactor.confirm_webauthn_registration(
                factors,
                user.id,
                challenge=b64url_decode(str(record.get("challenge", ""))),
                client_data=b64url_decode(data.client_data),
                attestation_object=b64url_decode(data.attestation_object),
                rp_id=rp_id,
                origins=accepted_origins,
                label=data.label or webauthn_label,
                require_user_verification=passkey_login,
                at=clock(),
                recovery_codes=recovery_codes,
            )
        except WebAuthnError:
            # One opaque answer for every way a ceremony fails to verify.
            # The message names the check for the server's own operator; the
            # caller learns only that it did not work.
            return JSONResponse({"error": "invalid_registration"}, status=400)
        verified = unpack_credential(credential.material).user_verified
        # Registering the user's **first** factor stamps the session,
        # exactly as confirming a first TOTP code does -- and registering an
        # additional one stamps nothing, because a key the caller has just
        # produced proves possession of their own authenticator and not of
        # the account's. See `totp/confirm` for the attack that closes.
        if first_factor:
            _stamp(session, {"second_factor_uv": verified})
        return JSONResponse(
            {
                "status": "enrolled",
                "id": credential.id,
                "user_verified": verified,
                # Present and non-empty only when this was the user's first
                # factor. They exist nowhere else, ever again.
                "recovery_codes": codes,
            },
            status=200,
        )


def _mount_passkey_login(context: _SecondFactorBuildContext) -> None:
    (
        router,
        _session,
        rp_id,
        _begin_ceremony,
        webauthn_key,
        _take_ceremony,
        discoverable_factors,
        limiter,
        _throttled,
        factors,
        accepted_origins,
        clock,
        users,
        pending_key,
        session_key,
    ) = _second_factor_context_values(
        context,
        (
            "router",
            "_session",
            "rp_id",
            "_begin_ceremony",
            "webauthn_key",
            "_take_ceremony",
            "discoverable_factors",
            "limiter",
            "_throttled",
            "factors",
            "accepted_origins",
            "clock",
            "users",
            "pending_key",
            "session_key",
        ),
    )
    _PASSKEY_LOGIN_SUBJECT = "wreath:passkey-login"

    @router.post("/webauthn/login/begin")
    async def webauthn_login_begin(request: Any):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        ceremony = _secondfactor.begin_webauthn_assertion(
            (), rp_id=rp_id, user_verification="required"
        )
        return await _begin_ceremony(session, ceremony, _PASSKEY_LOGIN_SUBJECT)

    @router.post("/webauthn/login")
    async def webauthn_login(request: Any, data: Annotated[WebAuthnAssertionInput, Body()]):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        marker = session.get(webauthn_key)
        if not isinstance(marker, dict):
            return JSONResponse({"error": "no_ceremony_in_progress"}, status=400)
        record, _ = await _take_ceremony(
            session,
            marker,
            subject=_PASSKEY_LOGIN_SUBJECT,
            kind=CHALLENGE_WEBAUTHN_ASSERT,
        )
        if record is None:
            return JSONResponse({"error": "ceremony_expired"}, status=400)
        subject: str | None = None
        try:
            public_id = b64url_decode(data.id)
            credential = await discoverable_factors.credential(
                _secondfactor.discoverable_credential_id(public_id)
            )
            if credential is None or credential.kind != "webauthn":
                raise WebAuthnError("no such discoverable credential")
            subject = credential.user_id
            if not limiter.allow(subject):
                return _throttled()
            result = await _secondfactor.verify_webauthn_assertion(
                factors,
                subject,
                challenge=b64url_decode(str(record.get("challenge", ""))),
                credential_id=public_id,
                client_data=b64url_decode(data.client_data),
                authenticator_data=b64url_decode(data.authenticator_data),
                signature=b64url_decode(data.signature),
                rp_id=rp_id,
                origins=accepted_origins,
                require_user_verification=True,
                at=clock(),
            )
        except ValueError, WebAuthnError:
            if subject is not None:
                limiter.record_failure(subject)
            return JSONResponse({"error": "invalid_assertion"}, status=401)
        user = await users.get_by_id(subject)
        if user is None or not user.is_active:
            limiter.record_failure(subject)
            return JSONResponse({"error": "invalid_assertion"}, status=401)
        limiter.record_success(subject)
        rotate_session(request)
        session.pop(pending_key, None)
        session[session_key] = {
            "sub": user.id,
            "type": "User",
            "roles": [],
            "second_factor_at": int(clock()),
            "second_factor_uv": result.user_verified,
        }
        return JSONResponse(_profile(user), status=200)


def _mount_webauthn_verification(context: _SecondFactorBuildContext) -> None:
    (
        router,
        _session,
        _pending_subject,
        _principal,
        factors,
        rp_id,
        user_verification,
        _begin_ceremony,
        webauthn_key,
        _forget_ceremony,
        limiter,
        _throttled,
        _take_ceremony,
        accepted_origins,
        clock,
        _stamp,
        users,
        pending_key,
        session_key,
    ) = _second_factor_context_values(
        context,
        (
            "router",
            "_session",
            "_pending_subject",
            "_principal",
            "factors",
            "rp_id",
            "user_verification",
            "_begin_ceremony",
            "webauthn_key",
            "_forget_ceremony",
            "limiter",
            "_throttled",
            "_take_ceremony",
            "accepted_origins",
            "clock",
            "_stamp",
            "users",
            "pending_key",
            "session_key",
        ),
    )

    @router.post("/webauthn/verify/begin")
    async def webauthn_verify_begin(request: Any):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        subject = _pending_subject(session)
        if subject is None:
            principal = _principal(session)
            if principal is None:
                return JSONResponse({"error": "no_pending_second_factor"}, status=401)
            subject = str(principal["sub"])
        rows = [row for row in await factors.credentials(subject) if row.kind == "webauthn"]
        if not rows:
            return JSONResponse({"error": "no_second_factor_enrolled"}, status=400)
        ceremony = _secondfactor.begin_webauthn_assertion(
            rows, rp_id=rp_id, user_verification=user_verification
        )
        return await _begin_ceremony(session, ceremony, subject)

    @router.post("/webauthn/verify")
    async def webauthn_verify(request: Any, data: Annotated[WebAuthnAssertionInput, Body()]):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        marker = session.get(webauthn_key)
        if not isinstance(marker, dict):
            return JSONResponse({"error": "no_ceremony_in_progress"}, status=400)
        # Who this session may be, derived exactly as `verify/begin` derived
        # it when the challenge was bound -- a half-finished login first,
        # then whoever is signed in. This is the *precondition*; the record
        # never gets to say who the caller is.
        pending = _pending_subject(session)
        principal = _principal(session)
        subject = (
            pending
            if pending is not None
            else (None if principal is None else str(principal["sub"]))
        )
        if subject is None:
            await _forget_ceremony(session)
            return JSONResponse({"error": "no_pending_second_factor"}, status=401)
        promoting = pending is not None
        # Before the consume, because the limiter keys on a subject already
        # known from the session: a throttled caller must not be able to
        # spend a challenge on their way to being refused.
        if not limiter.allow(subject):
            return _throttled()
        record, mismatched = await _take_ceremony(
            session, marker, subject=subject, kind=CHALLENGE_WEBAUTHN_ASSERT
        )
        if mismatched:
            # The challenge belongs to somebody this session is not.
            return JSONResponse({"error": "no_pending_second_factor"}, status=401)
        if record is None:
            return JSONResponse({"error": "ceremony_expired"}, status=400)
        try:
            result = await _secondfactor.verify_webauthn_assertion(
                factors,
                subject,
                challenge=b64url_decode(str(record.get("challenge", ""))),
                credential_id=b64url_decode(data.id),
                client_data=b64url_decode(data.client_data),
                authenticator_data=b64url_decode(data.authenticator_data),
                signature=b64url_decode(data.signature),
                rp_id=rp_id,
                origins=accepted_origins,
                at=clock(),
            )
        except WebAuthnError:
            limiter.record_failure(subject)
            return JSONResponse({"error": "invalid_assertion"}, status=401)
        limiter.record_success(subject)
        if not promoting:
            rotate_session(request)
            _stamp(session, {"second_factor_uv": result.user_verified})
            return JSONResponse({"status": "second_factor_verified"}, status=200)
        user = await users.get_by_id(subject)
        if user is None or not user.is_active:
            session.pop(pending_key, None)
            return JSONResponse({"error": "invalid_assertion"}, status=401)
        session.pop(pending_key, None)
        # Promotion is a privilege change and carries login's fixation risk.
        rotate_session(request)
        session[session_key] = {
            "sub": user.id,
            "type": "User",
            "roles": [],
            "second_factor_at": int(clock()),
            "second_factor_uv": result.user_verified,
        }
        return JSONResponse(_profile(user), status=200)


def second_factor_router(
    users: UserStore,
    factors: SecondFactorStore,
    *,
    issuer: str = "",
    prefix: str = "/auth/2fa",
    session_key: str = "principal",
    pending_key: str = "pending_second_factor",
    enrolment_key: str = _ENROLMENT_KEY,
    label: str = "Authenticator app",
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    skew: int = DEFAULT_SKEW,
    recovery_codes: int = DEFAULT_RECOVERY_CODES,
    enrolment_ttl: float = 600.0,
    pending_ttl: float = 300.0,
    step_up_ttl: float = 300.0,
    max_verify_attempts: int = 5,
    verify_window: float = 300.0,
    enrolments: Any = None,
    challenges: Any = None,
    rp_id: str = "",
    rp_name: str = "",
    origins: Sequence[str] = (),
    webauthn_key: str = _WEBAUTHN_KEY,
    webauthn_label: str = "Security key",
    webauthn_ttl: float = 300.0,
    user_verification: str = "preferred",
    passkey_login: bool = False,
    clock: Callable[[], float] = time,
) -> Router:
    """Build a mountable `Router` for TOTP enrolment, verification, and step-up.

    Mounts, under `prefix`, `POST /totp/begin`, `POST /totp/confirm`,
    `GET /` (the prefix itself, listing what is enrolled), `POST /verify` and
    `DELETE /{factor_id}`. It is opt-in and separate from `user_router`: an
    application that does not want a second factor mounts neither this router
    nor its table.

    **Separate, but not independent.** Building this router records it against
    the `users` store it was given, and a
    `user_router` over that same store with no `second_factors=` of its own
    refuses the login of anybody who has a factor here rather than issuing a
    full session. Pass the same `SecondFactorStore` to both and none of that
    machinery is reached; the coupling exists for the deployment that did not.

    Pass `rp_id` and four routes appear: `POST /webauthn/begin` and
    `POST /webauthn/confirm` register a passkey or security key, and
    `POST /webauthn/verify/begin` and `POST /webauthn/verify` are the assertion
    half, serving a pending login and a step-up with one pair exactly as
    `POST /verify` does for codes. Without `rp_id` the router is TOTP-only, so
    an application gets passkey endpoints only when it asks for them.

    Set `passkey_login=True` to add `POST /webauthn/login/begin` and
    `POST /webauthn/login`. These are a discoverable, usernameless first factor:
    begin sends no `allowCredentials`, the authenticator chooses a resident
    passkey, and completion resolves its owner with one indexed store lookup.
    Enrolments made by this router then require a resident credential and user
    verification; existing non-discoverable second-factor keys continue to work
    on the verify routes but cannot identify an account at login.

    A WebAuthn ceremony's challenge is minted by `begin`, held wherever
    `enrolments` says, **bound to the user who began it**, and deleted on every
    exit from `confirm` -- success, failure, expiry, and a session that changed
    hands alike. Single-use is the property the whole ceremony rests on: a
    challenge that survives a failed attempt is a challenge a recorded assertion
    can be replayed against.

    Enrolment is deliberately two-phase. `begin` mints a secret, returns the
    `otpauth://` URI, and enrols **nothing**; `confirm` takes one code from the
    user's authenticator and only then writes the credential and issues the
    recovery codes. A secret that was displayed but never confirmed simply
    expires out of the session, so a scan that went wrong cannot lock anybody
    out. Both require an already-authenticated session, since enrolling a factor
    is something a signed-in user does to their own account.

    A begun enrolment, and a begun WebAuthn ceremony, also end when the session
    does: `user_router`'s login and logout clear `enrolment_key` and
    `webauthn_key` and delete the rows behind them, wherever `enrolments` puts
    them. Renaming either key here is safe -- the names travel with the
    registration described above -- but an application that signs users in
    without `user_router` (an OAuth2 callback writing the principal itself)
    clears nothing, and there the ceremony's own record of who began it is what
    refuses a session that has changed hands.

    **Adding a factor to an account that already has one is itself a step-up**,
    and it is refused with `403 second_factor_required` unless a factor was
    proved within `step_up_ttl` -- the same predicate `DELETE /{factor_id}`
    uses, read in one place so the two cannot drift. `/totp/begin`,
    `/webauthn/begin` and both `confirm` routes carry it; the confirms re-check
    at the write, so a ceremony begun on a fresh stamp cannot land on a stale
    one. The **first** factor is exempt, because there is nothing to step up
    from, and recovery codes do not count as one -- they are minted alongside
    TOTP and never alone.

    **Enrolling a factor stamps the session only when it is the first one.** A
    stamp says the caller proved a factor the account already had; a factor the
    caller has just chosen proves possession of their own authenticator instead.
    Enrolment is refused for an already signed-in session that has not completed
    step-up, including when the caller intends to prove the new factor next.

    `POST /verify` is the other end, and it serves two moments with one route.
    Given a **pending** login it finishes it, rotating the session id before
    promoting it, so an id an attacker planted before the password step is not
    the id that ends up authenticated. Given an **already signed-in** session it
    is step-up: the same code check, the same rotation, and the principal is
    re-stamped with the moment it happened. It accepts a TOTP code or a recovery
    code in the same field; a recovery code is deleted when it is redeemed.

    That stamp -- `second_factor_at`, Unix seconds on the session principal and
    so on `Identity.claims` -- is what `wreath.auth.second_factor(max_age=...)`
    reads, and what the Cedar context exposes as `second_factor_age`. A route
    guarded by either asks *when*, not *whether*, so the application never
    threads a flag through a handler to find out.

    `DELETE /{factor_id}` un-enrols one factor, and **requires a factor proved
    within `step_up_ttl` seconds** -- turning off a second factor is exactly the
    act somebody who has stolen a session wants to perform, so it is guarded by
    the thing they do not have. It is owner-scoped twice over: the id is looked
    up among the caller's own credentials, and `SecondFactorStore.remove` is
    scoped to the user as well. Removing the last real factor removes the
    recovery codes with it, so "off" means off rather than a login that still
    demands a code the user no longer has; see
    `wreath._secondfactor.remove_second_factor` for why a recovery credential
    cannot be deleted by id at all.

    Failed verifications are counted per user, not per client address, which is
    the granularity that matters: a six-digit code is a million guesses and an
    attacker with a botnet has as many addresses as they like.
    `max_verify_attempts` (5) within `verify_window` (300s) applies to `confirm`
    and `verify` alike, answering 429 with a `Retry-After`. The counter lives in
    memory in this process, exactly as `LoginLimiter` documents.

    Args:
        users: the same `UserStore` `user_router` was built with.
        factors: where credentials live; `InMemorySecondFactorStore` for
            development, `OrmSecondFactorStore` or your own for production.
        issuer: your application's name, as the authenticator app displays it.
        pending_key: must match the `user_router` this pairs with.
        enrolment_ttl: seconds a begun-but-unconfirmed enrolment stays offerable.
        pending_ttl: seconds a pending login may wait for its second factor.
        step_up_ttl: seconds a proved factor counts as *recent* for `DELETE`.
        enrolments: a `wreath.session_store.SessionStore` -- pass the same one
            `SessionPolicy` was given. With it, a begun enrolment lives
            server-side under an opaque id and the cookie carries only that id;
            without it the unconfirmed secret rides in the session itself, which
            is a cookie unless the session middleware also has a store. The
            secret is not a credential either way -- nothing refers to it until
            a code verifies -- but a cookie persists where a response body does
            not, and can reach a disk, a log, or a shared machine's profile.
            **A WebAuthn challenge is only genuinely single-use with a store**:
            without one it lives in the cookie, and a caller who kept an older
            copy of that cookie has kept the challenge with it. Building the
            router without one raises no error -- an in-memory development app
            is a legitimate configuration -- but it does emit a `UserWarning`
            naming what is degraded, because the alternative is a deployment
            discovering it by being wrong.
        rp_id: the relying party id the passkey routes are scoped to -- the
            site's registrable domain, `example.com` rather than
            `login.example.com`, because a credential is bound to it for as long
            as it exists. **Passing it is what mounts the WebAuthn routes**; the
            router is TOTP-only without it, so an application that does not want
            passkeys gets no passkey endpoints.
        rp_name: the name an authenticator shows for this site; defaults to
            `rp_id`.
        origins: the origins a ceremony may be collected at, matched exactly.
            Defaults to `https://{rp_id}` -- **and `http://{rp_id}` as well when
            `rp_id` is loopback** (`localhost`, `::1`, 127.0.0.0/8), where a
            browser grants a secure context over plain HTTP and WebAuthn really
            works. A loopback origin may also carry any port, so
            `http://localhost:8000` matches without being named. That exception
            is loopback and nothing else: no other host is ever admitted over
            `http://`, and no host at all is admitted by a wildcard.
        user_verification: what the ceremonies ask of the authenticator.
            `preferred` for a second factor: verify the user when the device
            can, and let a security key with no PIN still work. The outcome is
            recorded on the session as `second_factor_uv` either way, so a
            policy that cares can read it rather than guess.
        passkey_login: mount discoverable first-factor passkey login. Requires
            `rp_id`, a `DiscoverableSecondFactorStore.credential`
            implementation, resident credentials at enrolment, and user
            verification on every login.
        clock: Unix-seconds source. It is what TOTP steps are counted from as
            well as what the two timeouts are measured with, so a server whose
            clock has drifted more than `skew` steps from its users' phones
            rejects every correct code. Injectable for deterministic tests.

    Raises:
        ValueError: a skew, digit count or period the code generator refuses.
    """
    # Validate the parameters once, here, rather than on the first request: a
    # router built with an impossible period should fail at import, not answer
    # 500 to the first person trying to sign in.
    _secondfactor.totp_code(b"\x00" * _secondfactor.MIN_SECRET_BYTES, 0, digits=digits)
    if skew < 0 or skew > _secondfactor.MAX_SKEW:
        raise ValueError(f"skew must be between 0 and {_secondfactor.MAX_SKEW} steps")
    if period <= 0:
        raise ValueError("period must be positive")
    for name, window in (
        ("enrolment_ttl", enrolment_ttl),
        ("pending_ttl", pending_ttl),
        ("step_up_ttl", step_up_ttl),
        ("verify_window", verify_window),
        ("webauthn_ttl", webauthn_ttl),
    ):
        if (
            isinstance(window, bool)
            or not isinstance(window, (int, float))
            or not isfinite(window)
            or window <= 0
        ):
            raise ValueError(f"{name} must be positive and finite")
    if origins and not rp_id:
        # Origins without an RP ID would configure routes that are not mounted,
        # which reads as "WebAuthn is on" while it is off.
        raise ValueError("origins are only meaningful together with rp_id")
    if passkey_login and not rp_id:
        raise ValueError("passkey_login requires a non-empty rp_id")
    if passkey_login and not isinstance(factors, DiscoverableSecondFactorStore):
        raise TypeError(
            "passkey_login requires DiscoverableSecondFactorStore.credential(credential_id)"
        )
    discoverable_factors = cast(DiscoverableSecondFactorStore, factors)
    accepted_origins = tuple(origins) if origins else (default_origins(rp_id) if rp_id else ())
    if rp_id and any(not origin for origin in accepted_origins):
        raise ValueError("every accepted origin must be non-empty")
    if rp_id:
        # Checked here rather than on the first ceremony, for the same reason
        # the TOTP parameters are: a router built with an impossible setting
        # should fail where it is written, not where somebody signs in.
        _secondfactor.begin_webauthn_assertion((), rp_id=rp_id, user_verification=user_verification)
    # A ceremony's state is never held in the session, whatever the deployment
    # passed. It goes to a `ChallengeStore`, and the default is a real one.
    # This replaces a `UserWarning` that could only name *half* its condition:
    # it fired on `enrolments=None` and then had to say "if your
    # SessionPolicy also has no store=, that means a cookie", because a
    # router built before any application exists cannot see the middleware. A
    # warning nobody can act on with certainty is a warning people learn to
    # ignore. The property now simply holds: single-use is enforced by an
    # atomic consume against a store, not by wherever the cookie went.
    # `MemoryChallengeStore` bounds a single worker. Behind more than one, a
    # ceremony begun on one worker is not spendable on another -- which fails
    # *closed* (refused, begin again) rather than open, and is what
    # `challenges=PostgresChallengeStore(db)` is for.
    # The default store shares this router's clock, so a ceremony's TTL is
    # measured on the same time source as everything else the router bounds.
    # A `PostgresChallengeStore` measures it in the database instead --
    # `clock_timestamp()`, so workers whose wall clocks disagree cannot disagree
    # about whether a challenge is still live.
    challenges = challenges if challenges is not None else MemoryChallengeStore(clock=clock)
    router = Router(prefix=prefix, tags=("users",))
    # `WebAuthnCeremony.ceremony` is the vocabulary the ceremony builders
    # already speak; this is the one place it becomes a store kind.
    _CEREMONY_KINDS = {
        "register": CHALLENGE_WEBAUTHN_REGISTER,
        "authenticate": CHALLENGE_WEBAUTHN_ASSERT,
    }
    limiter = LoginLimiter(max_attempts=max_verify_attempts, window=verify_window)

    context = _make_second_factor_context(
        router=router,
        users=users,
        factors=factors,
        issuer=issuer,
        session_key=session_key,
        pending_key=pending_key,
        enrolment_key=enrolment_key,
        label=label,
        digits=digits,
        period=period,
        skew=skew,
        recovery_codes=recovery_codes,
        enrolment_ttl=enrolment_ttl,
        pending_ttl=pending_ttl,
        step_up_ttl=step_up_ttl,
        verify_window=verify_window,
        enrolments=enrolments,
        challenges=challenges,
        rp_id=rp_id,
        rp_name=rp_name,
        accepted_origins=accepted_origins,
        webauthn_key=webauthn_key,
        webauthn_label=webauthn_label,
        webauthn_ttl=webauthn_ttl,
        user_verification=user_verification,
        passkey_login=passkey_login,
        clock=clock,
        limiter=limiter,
        discoverable_factors=discoverable_factors,
        ceremony_kinds=_CEREMONY_KINDS,
    )
    _mount_totp_enrolment_routes(context)
    pending_subject = _mount_recovery_routes(context)
    if rp_id:
        _mount_webauthn_routes(context, pending_subject)
    _session = context.values["_session"]
    _signed_in = context.values["_signed_in"]
    _stale_step_up = context.values["_stale_step_up"]
    _step_up_required = context.values["_step_up_required"]

    @router.delete("/{factor_id}")
    async def remove_factor(request: Any, factor_id: Annotated[str, Path()]):
        session = _session(request)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        user = await _signed_in(session)
        if user is None:
            return JSONResponse({"error": "not_authenticated"}, status=401)
        # Turning a second factor off is what somebody holding a stolen session
        # wants to do first, so it is guarded by the thing they do not have. A
        # 403 rather than a 401: the caller is signed in, and re-entering a
        # password would not help -- `POST /auth/2fa/verify` is the remediation.
        # The same predicate guards the enrolment routes, because registering a
        # factor and then proving it is otherwise a two-request way around this.
        if _stale_step_up(session):
            return _step_up_required()
        removed = await _secondfactor.remove_second_factor(factors, user.id, factor_id)
        if removed is None:
            # One answer for "no such id", "not yours", and "that is a recovery
            # code", so this cannot be used to probe which ids exist.
            return JSONResponse({"error": "not_found"}, status=404)
        return JSONResponse({"status": "removed", "id": removed.id}, status=200)

    # Announce this router to any `user_router` that was built without a store
    # of its own, so a login it cannot complete is refused rather than granted.
    # `remove_factor` is the anchor because it is registered unconditionally --
    # the WebAuthn handlers are not -- and because the decorator hands the
    # function back unchanged, so the object weakly referenced here is the one
    # the router (and any application that includes it) holds.
    _mounted_second_factors()  # prune what has been collected since the last one
    _MOUNTED_SECOND_FACTORS.append(
        _SecondFactorWiring(
            anchor=weakref.ref(remove_factor),
            users=users,
            factors=factors,
            enrolments=enrolments,
            challenges=challenges,
            enrolment_key=enrolment_key,
            webauthn_key=webauthn_key,
        )
    )
    return router


# Optional convenience. The store is a thin adapter over any user model with the
# expected columns; the app may supply its own model instead.


def default_user_model(table: str = "users") -> type[Any]:
    """Build a reference ORM user model. Import-lazy so importing `users` needs no DB.

    Declares exactly the columns the flows use -- `id`, `email`,
    `hashed_password`, `is_active`, `is_verified` -- plus nullable `created_at`
    and `updated_at` that nothing in this module writes. `email` is unique and
    indexed, which is what makes a duplicate registration a database refusal
    rather than a second row.

    The uuid primary key auto-generates via `default=uuid.uuid4` (wreath's
    client-side default convention, applied when the instance is constructed);
    the app may supply its own model instead. Calling this twice builds two
    distinct classes, so build the model once and pass it around.
    """
    import uuid

    from .orm import Mapped, Model, column
    from .orm.types import Bool, TimestampTz, Uuid, Varchar

    class User(Model, table=table):
        id: Mapped[uuid.UUID] = column(Uuid, primary_key=True, default=uuid.uuid4)
        email: Mapped[str] = column(Varchar, unique=True, index=True)
        hashed_password: Mapped[str] = column(Varchar)
        is_active: Mapped[bool] = column(Bool, default=True)
        is_verified: Mapped[bool] = column(Bool, default=False)
        created_at: Mapped[Any] = column(TimestampTz, nullable=True)
        updated_at: Mapped[Any] = column(TimestampTz, nullable=True)

    return User


def _orm_store_identity(session: Any, model: type[Any]) -> tuple[type[Any], object, object]:
    registry = getattr(session, "registry", None)
    database = getattr(registry, "database", session)
    tenant = getattr(session, "_tenant", None)
    return model, database, tenant


class OrmUserStore:
    """Reference `UserStore` over a wreath ORM session and a user model.

    The model must carry `id`, `email`, `hashed_password`, `is_active` and
    `is_verified`; `default_user_model()` builds one, and any model naming those
    columns works. Reads use `Model.select().where(...)` with `session.fetch_one`
    and `session.get`; writes use wreath's unit-of-work API,
    `session.add(instance)` then `await session.flush()` (there is no
    `session.update` — a loaded row is mutated and flushed). Nothing here
    commits: the transaction belongs to whoever opened the session.

    **It hashes nothing.** `create` stores the string it is handed, and the flows
    in `wreath._userkit` hash before calling it. Handing it a plaintext password
    stores a plaintext password.

    Emails are normalized with `strip().lower()` on `create` and on
    `get_by_email`, so a lookup matches rows this store wrote whatever case the
    caller used. The comparison is a plain SQL `=` rather than `lower(email)` or
    a citext column, so a row some other writer inserted with upper-case
    characters is not found — the case-insensitivity is normalization on both
    sides, not a case-insensitive column.

    `created_at`/`updated_at` are a stated gap: no method writes them and
    `_to_record` does not read them, so every `UserRecord` this store returns
    carries 0.0 for both.

    Args:
        session: an open ORM session; every method awaits it and none commits.
        model: the ORM model class carrying the columns above.
    """

    __slots__ = ("_model", "_session", "_store_id")

    # The model is whatever ORM class the application supplied, so its columns --
    # `select()`, `email`, and the rest -- exist only at runtime. `type[Any]` says
    # that honestly; a bare `type` claims the attributes are absent.
    def __init__(self, session: Any, model: type[Any]) -> None:
        self._session = session
        self._model = model
        self._store_id = _orm_store_identity(session, model)

    @property
    def store_id(self) -> object:
        """The model, database, and tenant that decide the rows served.

        Two of these over one model are one store wearing two objects, and a
        deployment that builds one inline for each router has exactly that. See
        `UserStore` for the convention and `_same_store` for what reads it; the
        session itself is deliberately not part of it, since two sessions over
        one database and tenant still see the same table.
        """
        return self._store_id

    def _to_record(self, row: Any) -> UserRecord:
        return UserRecord(
            id=str(row.id),
            email=row.email,
            hashed_password=row.hashed_password,
            is_active=bool(row.is_active),
            is_verified=bool(row.is_verified),
        )

    def _found(self, row: Any) -> UserRecord | None:
        """`_to_record` for the lookups, where a miss is a legitimate answer."""
        return None if row is None else self._to_record(row)

    async def get_by_email(self, email: str) -> UserRecord | None:
        """Return the user with this email, or None when no row matches.

        The argument is normalized with `strip().lower()` before the comparison,
        which is what makes the lookup case-insensitive for rows `create` wrote.
        A miss is an answer, not an error.
        """
        query = self._model.select().where(self._model.email == email.strip().lower())
        return self._found(await self._session.fetch_one(query))

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        """Return the user with this primary key, or None when no row matches.

        `user_id` reaches `session.get` unconverted, so the model's primary-key
        column has to accept a `str`. The `Uuid` key `default_user_model`
        declares does not: the ORM coerces a comparison value against the column
        type and raises `TypeError: expected UUID, got str`. Pair a uuid model
        with a store that converts, or give the model a text primary key.
        """
        return self._found(await self._session.get(self._model, user_id))

    async def get_many_by_id(self, user_ids: Iterable[str]) -> list[UserRecord | None]:
        """Return ordered users and misses after one query for all supplied ids.

        Repeated ids repeat their result without repeating the database query.
        An empty input returns without crossing into the session. Primary-key
        values follow the same typing rule as `get_by_id`.
        """
        ordered = tuple(user_ids)
        if not ordered:
            return []
        query = self._model.select().where(self._model.id.in_(ordered))
        rows = await self._session.fetch(query)
        by_id = {str(row.id): self._to_record(row) for row in rows}
        return [by_id.get(user_id) for user_id in ordered]

    async def create(self, email: str, hashed_password: str) -> UserRecord:
        """Insert a user from an already-hashed password and return the record.

        `hashed_password` is stored verbatim. The email is normalized with
        `strip().lower()`; `is_active` and `is_verified` take the model's own
        defaults. Uniqueness belongs to the database — the reference model marks
        `email` unique, so a duplicate surfaces as an error from the flush rather
        than as an existing record.

        The row is added and flushed, not committed, so it is visible to later
        statements in the same transaction and lands when that transaction does.
        """
        instance = self._model(email=email.strip().lower(), hashed_password=hashed_password)
        self._session.add(instance)
        await self._session.flush()
        # The instance was just constructed, so this is never a miss -- which is
        # why it calls the total `_to_record` and needs no ignore.
        return self._to_record(instance)

    async def update(self, user: UserRecord) -> UserRecord:
        """Write `user` back over its row and flush. Returns the argument.

        Not a partial update. The row is loaded by primary key and then `email`,
        `hashed_password`, `is_active` and `is_verified` are all assigned from
        `user`, so a record that was read, changed in one field, and passed back
        rewrites the other three with whatever it was still holding. Loading it
        by primary key carries the same typing rule as `get_by_id`.

        `created_at`/`updated_at` are left untouched, the email is written as
        given rather than normalized, and the returned record is the argument
        rather than a re-read of the row. A `user.id` with no row raises
        `AttributeError` on the first assignment — this never inserts.
        """
        row = await self._session.get(self._model, user.id)
        row.email = user.email
        row.hashed_password = user.hashed_password
        row.is_active = user.is_active
        row.is_verified = user.is_verified
        await self._session.flush()  # unit-of-work: mutate loaded row, then flush
        return user

    async def compare_and_set_password(self, user_id: str, expected: str, replacement: str) -> bool:
        async with self._session.begin():
            query = self._model.select().where(self._model.id == user_id).for_update()
            row = await self._session.fetch_one(query)
            if row is None or not _userkit.hmac.compare_digest(row.hashed_password, expected):
                return False
            row.hashed_password = replacement
            await self._session.flush()
        return True


def default_second_factor_model(table: str = "user_second_factors") -> type[Any]:
    """Build a reference ORM model for second-factor credentials.

    A companion table rather than columns on the user model, which is what makes
    this additive for a deployment that already has users: adding second factors
    is a `CREATE TABLE`, not an `ALTER` on the hottest table in the schema. A
    user has zero or many credentials, so a table is also simply the right
    shape.

    Declares `id`, `user_id` (indexed, since every read is "this user's
    factors"), `kind`, `label`, `secret_material`, `counter`, `created_at` and
    `last_used_at`.

    `secret_material` is `bytea` and holds the TOTP shared secret or a recovery
    code's scrypt hash. It is named for what it is rather than for the
    `SecondFactor.material` field it maps to, and that is load-bearing: the
    column name is what `wreath.crud.SENSITIVE_FIELD` and the GraphQL schema
    builder match on to hide a column, and `material` on its own matches
    nothing. A model of your own that calls it something innocuous will have
    that something rendered into a generated API response.

    `counter` is the newest accepted TOTP step, and it is what makes a code
    single-use.

    The primary key is **text**, not `uuid`, unlike `default_user_model`. That
    is deliberate: `SecondFactor.id` is a string everywhere else, and the ORM
    coerces a comparison value against its column's type, so a uuid key would
    make `store.remove(user_id, "…")` raise `TypeError: expected UUID, got str`
    on the id the router just handed out. The default is
    `wreath._secondfactor.new_credential_id`, 128 bits of hex.

    Import-lazy, like `default_user_model`, and likewise a fresh class per
    call -- build it once and pass it around.
    """
    from .orm import Mapped, Model, column
    from .orm.types import Bytea, Int64, TimestampTz, Varchar

    class SecondFactorCredential(Model, table=table):
        id: Mapped[str] = column(Varchar, primary_key=True, default=_secondfactor.new_credential_id)
        user_id: Mapped[str] = column(Varchar, index=True)
        kind: Mapped[str] = column(Varchar)
        label: Mapped[str] = column(Varchar)
        secret_material: Mapped[bytes] = column(Bytea)
        counter: Mapped[int] = column(Int64, default=0)
        created_at: Mapped[Any] = column(TimestampTz)
        last_used_at: Mapped[Any] = column(TimestampTz, nullable=True)

    return SecondFactorCredential


class OrmSecondFactorStore:
    """Reference `SecondFactorStore` over a wreath ORM session and a model.

    The model must carry the columns `default_second_factor_model()` declares.
    Reads use `Model.select().where(...)` with `session.fetch`; writes use the
    unit-of-work API -- `session.add(instance)` or `session.delete(instance)`
    then `await session.flush()`. Nothing here commits: the transaction belongs
    to whoever opened the session.

    Two refusals in here are security properties rather than tidiness, and both
    are `raise`s so that `python -O` cannot delete them:

    * `remove` deletes only when the row's `user_id` matches the one passed. The
      credential id is the only thing an HTTP caller supplies, so without the
      check any user could delete any other user's second factor by guessing or
      observing an id.
    * `touch` advances the counter in **one conditional statement**, and says
      whether it won. That counter is the replay defence, and a read-then-write
      cannot enforce it: verification reads the credential, awaits the code
      check, and only then writes, so two requests carrying the same observed
      code both read the same counter and a plain `UPDATE ... SET counter = $1`
      lets both of them through. `WHERE counter < $1 RETURNING 1` is the whole
      fix -- PostgreSQL decides, once, and the loser is told it lost.

    Args:
        session: an open ORM session; every method awaits it and none commits.
        model: the ORM model class carrying the columns above.
    """

    __slots__ = ("_advance_sql", "_model", "_session", "_store_id")

    def __init__(self, session: Any, model: type[Any]) -> None:
        self._session = session
        self._model = model
        self._store_id = _orm_store_identity(session, model)
        self._advance_sql: str | None = None

    @property
    def store_id(self) -> object:
        """The model, database, and tenant. `OrmUserStore.store_id` says why."""
        return self._store_id

    def _to_record(self, row: Any) -> SecondFactor:
        return SecondFactor(
            id=str(row.id),
            user_id=str(row.user_id),
            kind=row.kind,
            label=row.label,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            material=bytes(row.secret_material),
            counter=int(row.counter),
        )

    async def _row(self, credential_id: str) -> Any:
        query = self._model.select().where(self._model.id == credential_id)
        return await self._session.fetch_one(query)

    async def credentials(self, user_id: str) -> list[SecondFactor]:
        """Every credential belonging to `user_id`. An empty list is an answer."""
        query = self._model.select().where(self._model.user_id == user_id)
        return [self._to_record(row) for row in await self._session.fetch(query)]

    async def credential(self, credential_id: str) -> SecondFactor | None:
        """Return one credential by indexed primary key, or `None`."""
        row = await self._row(credential_id)
        return None if row is None else self._to_record(row)

    async def add(self, credential: SecondFactor) -> SecondFactor:
        """Insert one credential and flush; returns the argument.

        The row is added and flushed, not committed, so it is visible to later
        statements in the same transaction and lands when that transaction does.
        `credential.id` is written through, so the id the caller minted is the
        id the database holds -- which is what lets `confirm_totp_enrolment`
        return a credential that can be looked up again.
        """
        self._session.add(self._instance(credential))
        await self._session.flush()
        return credential

    def _instance(self, credential: SecondFactor) -> Any:
        return self._model(
            id=credential.id,
            user_id=credential.user_id,
            kind=credential.kind,
            label=credential.label,
            secret_material=credential.material,
            counter=credential.counter,
            created_at=credential.created_at,
            last_used_at=credential.last_used_at,
        )

    async def add_many(self, credentials: Sequence[SecondFactor]) -> tuple[SecondFactor, ...]:
        """Insert one credential set with a single unit-of-work flush."""
        batch = tuple(credentials)
        for credential in batch:
            self._session.add(self._instance(credential))
        if batch:
            await self._session.flush()
        return batch

    async def remove(self, user_id: str, credential_id: str) -> None:
        """Delete one credential, but only when it belongs to `user_id`.

        A row that does not exist is not an error -- the outcome asked for is
        that it is gone. A row belonging to somebody else is silently not
        deleted, which is the same observable answer, so this cannot be used to
        probe which ids exist.
        """
        row = await self._row(credential_id)
        if row is None or str(row.user_id) != user_id:
            return
        self._session.delete(row)
        await self._session.flush()

    async def remove_many(self, user_id: str, credential_ids: Sequence[str]) -> None:
        """Delete one user's named credentials with one read and one flush."""
        wanted = frozenset(credential_ids)
        if not wanted:
            return
        query = self._model.select().where(self._model.user_id == user_id)
        rows = [row for row in await self._session.fetch(query) if str(row.id) in wanted]
        for row in rows:
            self._session.delete(row)
        if rows:
            await self._session.flush()

    def _advance_statement(self) -> str:
        """`UPDATE ... WHERE counter < $1 RETURNING 1`, built once per store.

        Rendered from the model's own spec rather than from the column names
        spelled here, so a model that maps `counter` to a differently named
        column updates the column it actually has. `qualified` and `quote` are
        the compiler's own, which is what keeps this statement and the compiled
        `select()` above naming the same table under an isolated tenant.
        """
        from .orm.compiler import qualified, quote

        spec = self._session.registry.spec_for(self._model)
        counter_column = quote(spec.by_name["counter"].database_name)
        used_column = quote(spec.by_name["last_used_at"].database_name)
        id_column = quote(spec.by_name["id"].database_name)
        return (
            f"UPDATE {qualified(spec)} SET {counter_column} = $1, {used_column} = $2 "
            f"WHERE {id_column} = $3 AND {counter_column} < $1 RETURNING 1"
        )

    async def touch(self, credential_id: str, *, counter: int, at: Any) -> bool:
        """Record a use -- the newest accepted step and when -- if it is newer.

        One statement, no read first: the counter is compared and set inside
        PostgreSQL, so of two requests replaying the same code exactly one
        updates a row. Written as a read, a check and a write, both would.

        This is deliberately *not* the unit-of-work path the other writers here
        use. A loaded row mutated and flushed carries the value this session read
        before it awaited anything, which is the stale value the race is made of.

        Returns:
            True when this call advanced the counter. False when it did not --
            the stored counter is already at or past `counter`, or the
            credential is gone. `wreath._secondfactor.verify_second_factor`
            reads False as a replay and refuses.
        """
        if self._advance_sql is None:
            self._advance_sql = self._advance_statement()
        won = await self._session.raw(self._advance_sql, counter, at, credential_id).fetchval()
        return won is not None
