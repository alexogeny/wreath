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
    app.include_router(user_router(store, secret=SECRET, base_url="https://app"))

`SessionMiddleware` is required for login: without it `POST /login` answers 500
rather than signing anyone in. `POST /logout` and `GET /me` degrade instead --
logout reports success having cleared nothing, and `/me` answers 401.

Email delivery is a pluggable `EmailSender`. The default `LogEmailSender` prints
the link rather than sending it; `SmtpEmailSender` (stdlib `smtplib`, STARTTLS or
implicit TLS on port 465) is the shipped production transport.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Annotated, Any

from . import _userkit
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
from .binding import Body, Path
from .cache import BoundedCache
from .middleware.sessions import rotate_session
from .response import JSONResponse
from .router import Router

__all__ = [
    "CapturingEmailSender",
    "EmailSender",
    "InMemoryUserStore",
    "LogEmailSender",
    "OrmUserStore",
    "SmtpEmailSender",
    "UserRecord",
    "UserStore",
    "default_user_model",
    "hash_password",
    "user_router",
    "verify_password",
]


# --- request models (validated by wreath binding) ---------------------------


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

    **Per-process and in-memory.** The counts live in a `BoundedCache` inside
    this object, shared with nothing. Across N workers the real budget is
    N x `max_attempts`, a restart clears every count, and only `max_tracked`
    identifiers are counted at once -- failures against more identifiers than
    that evict the least recently used counts. This is a limitation of the
    design, not a defect: it needs no Redis and no coordination.

    Args:
        max_attempts: failed attempts allowed per identifier within one window.
        window: seconds the window lasts, timed from the first counted failure.
        max_tracked: identifiers counted at once, least-recently-used evicted first.
        clock: monotonic seconds source, injectable for deterministic tests.

    Raises:
        ValueError: max_attempts below 1, or a window that is not positive.
    """

    __slots__ = ("_attempts", "_clock", "_max", "_window")

    def __init__(
        self,
        *,
        max_attempts: int = 10,
        window: float = 300.0,
        max_tracked: int = 4096,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if window <= 0:
            raise ValueError("window must be positive")
        self._max = max_attempts
        self._window = window
        self._clock = clock
        self._attempts: BoundedCache = BoundedCache(
            max_entries=max_tracked, ttl=window, clock=clock
        )

    def allow(self, identifier: str) -> bool:
        """Whether another attempt may be made for `identifier` right now.

        A pure question: it counts nothing and starts no window. An identifier
        that has never failed, or whose window has elapsed, is allowed and its
        expired entry is dropped in passing.
        """
        entry = self._attempts.get(identifier)
        if entry is None:
            return True
        count, first = entry
        if self._clock() - first >= self._window:
            self._attempts.delete(identifier)
            return True
        return count < self._max

    def record_failure(self, identifier: str) -> None:
        """Count one failed attempt against `identifier`.

        The first failure, and the first after a window elapsed, opens a new
        window at the current clock reading. Every later failure inside that
        window increments the count and keeps the original start, which is what
        makes the window fixed rather than sliding.
        """
        entry = self._attempts.get(identifier)
        now = self._clock()
        if entry is None or now - entry[1] >= self._window:
            self._attempts.set(identifier, (1, now))
            return
        self._attempts.set(identifier, (entry[0] + 1, entry[1]))

    def record_success(self, identifier: str) -> None:
        """Clear the failure count and window for `identifier`.

        The entry is dropped outright rather than decremented, so a user who
        mistypes twice and then signs in starts from a full budget. Nothing else
        is stored per identifier, so this leaves no state behind.
        """
        self._attempts.delete(identifier)


async def reset_password_endpoint(
    store: UserStore,
    sessions: Any = None,
    *,
    secret: str,
    token: str,
    new_password: str,
) -> bool:
    """Reset a password and end that user's other sessions.

    The second half is the point. A user resets a password *because* somebody
    else is in the account, and changing the credential only stops the next
    sign-in -- whoever is already holding a session keeps it. `sessions` is any
    session store; one that cannot enumerate (no `delete_for`) is used for
    nothing and does not fail the reset.

    Sessions are dropped only for the subject named in the token, and only after
    the reset succeeded: a token that fails verification changes no password and
    ends no session.

    Returns:
        True when the password changed, False for a bad token or a refused new password.
    """
    subject = _userkit._token_subject(token)
    ok = await _userkit.reset_password(
        store, secret=secret, token=token, new_password=new_password
    )
    if not ok or sessions is None or subject is None:
        return ok
    delete_for = getattr(sessions, "delete_for", None)
    if delete_for is not None:
        await delete_for(subject)
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
    max_reset_requests: int = 3,
    reset_window: float = 15 * 60.0,
) -> Router:
    """Build a mountable `Router` with the full user lifecycle.

    Mounts, under `prefix`, `POST /register`, `/login`, `/logout`, `/verify`,
    `/forgot-password` and `/reset-password`, plus `GET /verify/{token}` and
    `GET /me`. Login writes the session principal the auth stack already reads,
    so it requires `SessionMiddleware`; without it `/login` answers 500 with
    `{"error": "session_middleware_required"}` and signs nobody in.

    `secret` signs the action tokens (use a stable app secret). `link_builder`
    maps `(purpose, token) -> URL` for the emailed links; the default points at
    this router's own verify route and a `reset-password?token=` query.

    `sessions` is the server-side session store, if there is one. Passing it
    is what lets a password reset end the sessions somebody else is already
    holding; without it the reset changes the credential and nothing more.

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

    These handlers answer with plain JSON `{"status": ...}` / `{"error": ...}`
    bodies. Failures raised by the framework around them keep the standard
    shape -- a malformed or invalid body is an RFC 9457
    `application/problem+json`, 400 for unparseable JSON and 422 for a body that
    fails validation, the latter carrying an `errors` list of one entry per
    field.
    """
    if not secret:
        raise ValueError("user_router requires a non-empty secret")
    mailer = email_sender if email_sender is not None else LogEmailSender()
    links = link_builder if link_builder is not None else _default_link(base_url, prefix)
    router = Router(prefix=prefix, tags=("users",))
    limiter = LoginLimiter(max_attempts=max_login_attempts, window=login_window)
    reset_limiter = LoginLimiter(
        max_attempts=max_reset_requests,
        window=reset_window,
    )

    def _session(request: Any) -> dict[str, Any] | None:
        return getattr(request.state, "session", None)

    @router.post("/register")
    async def register(request: Any, data: Annotated[RegisterInput, Body()]):
        await _userkit.register(
            store, mailer, secret=secret, email=data.email, password=data.password,
            link_builder=links, ttl=verify_ttl,
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
            refused.headers.append(
                (b"retry-after", str(int(login_window)).encode("ascii"))
            )
            return refused
        user = await _userkit.authenticate(store, data.email, data.password)
        if user is None:
            limiter.record_failure(identifier)
            return JSONResponse({"error": "invalid_credentials"}, status=401)
        limiter.record_success(identifier)
        rotate_session(request)
        session[session_key] = {"sub": user.id, "type": "User", "roles": []}
        return JSONResponse(_profile(user), status=200)

    @router.post("/logout")
    async def logout(request: Any):
        session = _session(request)
        if session is not None:
            session.pop(session_key, None)
        return JSONResponse({"status": "logged_out"}, status=200)

    @router.post("/verify")
    async def verify(request: Any, data: Annotated[TokenInput, Body()]):
        ok = await _userkit.verify_email(store, secret=secret, token=data.token)
        return JSONResponse({"status": "verified" if ok else "invalid_token"},
                           status=200 if ok else 400)

    @router.get("/verify/{token}")
    async def verify_link(request: Any, token: Annotated[str, Path()]):
        ok = await _userkit.verify_email(store, secret=secret, token=token)
        return JSONResponse({"status": "verified" if ok else "invalid_token"},
                           status=200 if ok else 400)

    @router.post("/forgot-password")
    async def forgot(request: Any, data: Annotated[ForgotInput, Body()]):
        identifier = data.email.strip().lower()
        if reset_limiter.allow(identifier):
            # Count issuance attempts whether or not the account exists, keeping
            # both the response and the work decision free of enumeration clues.
            reset_limiter.record_failure(identifier)
            await _userkit.start_password_reset(
                store, mailer, secret=secret, email=data.email,
                link_builder=links, ttl=reset_ttl,
            )
        # Uniform response for absent accounts and exhausted issuance budgets.
        return JSONResponse({"status": "reset_email_sent"}, status=200)

    @router.post("/reset-password")
    async def reset(request: Any, data: Annotated[ResetInput, Body()]):
        ok = await reset_password_endpoint(
            store, sessions, secret=secret, token=data.token,
            new_password=data.password,
        )
        return JSONResponse({"status": "password_reset" if ok else "invalid_token"},
                           status=200 if ok else 400)

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


# --- reference ORM-backed store + model -------------------------------------
#
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

    __slots__ = ("_model", "_session")

    # The model is whatever ORM class the application supplied, so its columns --
    # `select()`, `email`, and the rest -- exist only at runtime. `type[Any]` says
    # that honestly; a bare `type` claims the attributes are absent.
    def __init__(self, session: Any, model: type[Any]) -> None:
        self._session = session
        self._model = model

    def _to_record(self, row: Any) -> UserRecord:
        return UserRecord(
            id=str(row.id), email=row.email, hashed_password=row.hashed_password,
            is_active=bool(row.is_active), is_verified=bool(row.is_verified),
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
