"""User-management lifecycle flows — the ``fastapi-users`` equivalent.

Builds registration / login / logout / email-verification / password-reset / me
*on top of* wreath's existing auth: login writes the same signed-session principal
(``{"sub", "type", "roles"}``) that :class:`wreath.auth.SessionIdentityBackend`
already reads, so this integrates with the auth stack rather than reinventing it.

The security-sensitive core (scrypt hashing, HMAC action tokens, flow logic) lives
in the stdlib-only :mod:`wreath._userkit`; this module is the thin wreath glue.

    store = InMemoryUserStore()            # or an ORM-backed UserStore
    app.include_router(user_router(store, secret=SECRET, base_url="https://app"))

Requires ``SessionMiddleware`` for login/logout/``/me``. Email delivery is a
pluggable :class:`EmailSender` (the dev default just logs the link); a real
SMTP/SES backend is the separate email gap (#5).
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
    email: str
    password: str


@dataclass(slots=True)
class LoginInput:
    email: str
    password: str


@dataclass(slots=True)
class TokenInput:
    token: str


@dataclass(slots=True)
class ForgotInput:
    email: str


@dataclass(slots=True)
class ResetInput:
    token: str
    password: str


class LoginLimiter:
    """Counts failed sign-ins per identifier and refuses past a budget.

    Deliberately here rather than in :mod:`wreath._userkit`: that module is
    stdlib-only on purpose, and a limiter wants a bounded store. The primitive
    it guards -- ``_userkit.authenticate`` -- stays unthrottled, so an
    application calling it directly owns this decision itself.

    Keyed per identifier, so failing against one account cannot lock another
    out. A success clears the count, so a legitimate user who mistypes twice and
    then succeeds starts clean.
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
        """Whether another attempt may be made for ``identifier`` right now."""
        entry = self._attempts.get(identifier)
        if entry is None:
            return True
        count, first = entry
        if self._clock() - first >= self._window:
            self._attempts.delete(identifier)
            return True
        return count < self._max

    def record_failure(self, identifier: str) -> None:
        entry = self._attempts.get(identifier)
        now = self._clock()
        if entry is None or now - entry[1] >= self._window:
            self._attempts.set(identifier, (1, now))
            return
        self._attempts.set(identifier, (entry[0] + 1, entry[1]))

    def record_success(self, identifier: str) -> None:
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
    session store; one that cannot enumerate (no ``delete_for``) is used for
    nothing and does not fail the reset.
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
) -> Router:
    """Build a mountable :class:`Router` with the full user lifecycle.

    ``secret`` signs the action tokens (use a stable app secret). ``link_builder``
    maps ``(purpose, token) -> URL`` for the emailed links; the default points at
    this router's own verify route and a ``reset-password?token=`` query.

    ``sessions`` is the server-side session store, if there is one. Passing it
    is what lets a password reset end the sessions somebody else is already
    holding; without it the reset changes the credential and nothing more.

    ``max_login_attempts``/``login_window`` throttle failed sign-ins per
    identifier -- in this router only. ``wreath._userkit.authenticate`` stays
    unguarded for direct callers.
    """
    if not secret:
        raise ValueError("user_router requires a non-empty secret")
    mailer = email_sender if email_sender is not None else LogEmailSender()
    links = link_builder if link_builder is not None else _default_link(base_url, prefix)
    router = Router(prefix=prefix, tags=("users",))
    limiter = LoginLimiter(max_attempts=max_login_attempts, window=login_window)

    def _session(request: Any) -> dict[str, Any] | None:
        return getattr(request.state, "session", None)

    @router.post("/register")
    async def register(request: Any, data: Annotated[RegisterInput, Body()]):  # noqa: ANN202
        await _userkit.register(
            store, mailer, secret=secret, email=data.email, password=data.password,
            link_builder=links, ttl=verify_ttl,
        )
        # Uniform response — never reveals whether the email already existed.
        return JSONResponse({"status": "registration_received"}, status=202)

    @router.post("/login")
    async def login(request: Any, data: Annotated[LoginInput, Body()]):  # noqa: ANN202
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
        session[session_key] = {"sub": user.id, "type": "User", "roles": []}
        return JSONResponse(_profile(user), status=200)

    @router.post("/logout")
    async def logout(request: Any):  # noqa: ANN202
        session = _session(request)
        if session is not None:
            session.pop(session_key, None)
        return JSONResponse({"status": "logged_out"}, status=200)

    @router.post("/verify")
    async def verify(request: Any, data: Annotated[TokenInput, Body()]):  # noqa: ANN202
        ok = await _userkit.verify_email(store, secret=secret, token=data.token)
        return JSONResponse({"status": "verified" if ok else "invalid_token"},
                           status=200 if ok else 400)

    @router.get("/verify/{token}")
    async def verify_link(request: Any, token: Annotated[str, Path()]):  # noqa: ANN202
        ok = await _userkit.verify_email(store, secret=secret, token=token)
        return JSONResponse({"status": "verified" if ok else "invalid_token"},
                           status=200 if ok else 400)

    @router.post("/forgot-password")
    async def forgot(request: Any, data: Annotated[ForgotInput, Body()]):  # noqa: ANN202
        await _userkit.start_password_reset(
            store, mailer, secret=secret, email=data.email,
            link_builder=links, ttl=reset_ttl,
        )
        # Uniform response regardless of whether the account exists.
        return JSONResponse({"status": "reset_email_sent"}, status=200)

    @router.post("/reset-password")
    async def reset(request: Any, data: Annotated[ResetInput, Body()]):  # noqa: ANN202
        ok = await reset_password_endpoint(
            store, sessions, secret=secret, token=data.token,
            new_password=data.password,
        )
        return JSONResponse({"status": "password_reset" if ok else "invalid_token"},
                           status=200 if ok else 400)

    @router.get("/me")
    async def me(request: Any):  # noqa: ANN202
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
    """Build a reference ORM user model. Import-lazy so importing ``users`` needs no DB.

    The uuid primary key auto-generates via ``default=uuid.uuid4`` (wreath's
    client-side default convention); the app may supply its own model instead.
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
    """Reference :class:`UserStore` over a wreath ORM session + user model.

    Reads use ``Model.select().where(...)`` + ``session.fetch_one`` / ``session.get``;
    writes use wreath's unit-of-work API: ``session.add(instance)`` + ``await
    session.flush()`` (there is no ``session.update`` — a loaded row is flushed).
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
        query = self._model.select().where(self._model.email == email.strip().lower())
        return self._found(await self._session.fetch_one(query))

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        return self._found(await self._session.get(self._model, user_id))

    async def create(self, email: str, hashed_password: str) -> UserRecord:
        instance = self._model(email=email.strip().lower(), hashed_password=hashed_password)
        self._session.add(instance)
        await self._session.flush()
        # The instance was just constructed, so this is never a miss -- which is
        # why it calls the total `_to_record` and needs no ignore.
        return self._to_record(instance)

    async def update(self, user: UserRecord) -> UserRecord:
        row = await self._session.get(self._model, user.id)
        row.email = user.email
        row.hashed_password = user.hashed_password
        row.is_active = user.is_active
        row.is_verified = user.is_verified
        await self._session.flush()  # unit-of-work: mutate loaded row, then flush
        return user
