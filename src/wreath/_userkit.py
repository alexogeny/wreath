"""Stdlib-only core for the user-management lifecycle (no wreath imports).

Kept dependency-free and import-light so the password hashing, signed action
tokens, store protocol, and flow logic are unit-testable without the native
package. The wreath-coupled router glue lives in `wreath.users`.

Password hashing uses stdlib `hashlib.scrypt` (zero-dep, memory-hard). Action
tokens (email verification, password reset) are HMAC-SHA256 signed and expiring;
a per-user *fingerprint* (a hash of the current password hash) is folded into the
signature so a reset link self-invalidates the moment the password changes —
giving single-use semantics without server-side token storage.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

__all__ = [
    "CapturingEmailSender",
    "EmailSender",
    "InMemoryUserStore",
    "LogEmailSender",
    "SmtpEmailSender",
    "UserRecord",
    "UserStore",
    "fingerprint",
    "hash_password",
    "register",
    "authenticate",
    "verify_email",
    "start_password_reset",
    "reset_password",
    "sign_token",
    "verify_password",
    "verify_token",
]

# --- password hashing -------------------------------------------------------

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024

#: Longest password accepted, in UTF-8 bytes. scrypt hashes the whole input, so
#: an unbounded one is a CPU amplifier any anonymous caller can pull -- and no
#: human password is near this. Refused on hash, and rejected without spending
#: the CPU on verify.
MAX_PASSWORD_BYTES = 1024


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(
    password: str, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P
) -> str:
    """Hash `password` with a fresh salt; returns `scrypt$n$r$p$salt$hash`."""
    if not password:
        raise ValueError("password must not be empty")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must not exceed {MAX_PASSWORD_BYTES} bytes")
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=_SCRYPT_DKLEN, maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verify `password` against a stored `encoded` hash."""
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        # Refused before scrypt: a password this long cannot be the one that was
        # stored, so hashing it is work an attacker chose for us.
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(hash_b64)
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# --- signed action tokens ---------------------------------------------------


def _frame(*fields: str) -> str:
    """Join fields so each is recoverable whatever it contains.

    `<len>:<field>` per field. A plain `":".join` meant a value containing a
    colon reassigned every field after it -- and the signature covers the joined
    form, so both sides agreed on bytes that meant two different things.
    """
    return "".join(f"{len(field)}:{field}" for field in fields)


def _unframe(body: str, count: int) -> list[str]:
    """The inverse of `_frame`. Raises ValueError on anything malformed."""
    fields: list[str] = []
    position = 0
    for _ in range(count):
        marker = body.index(":", position)
        length = int(body[position:marker])
        start = marker + 1
        fields.append(body[start : start + length])
        position = start + length
    if position != len(body):
        raise ValueError("trailing data in a framed token")
    return fields


def fingerprint(hashed_password: str) -> str:
    """A short, opaque fingerprint of the current password hash (for single-use)."""
    return hashlib.sha256(hashed_password.encode("utf-8")).hexdigest()[:16]


def sign_token(
    secret: str, purpose: str, subject: str, *, ttl: int, bound: str = "", now: float | None = None
) -> str:
    """Sign an expiring, purpose-scoped token bound to `subject` (+ optional `bound`)."""
    issued = int(time.time() if now is None else now)
    expires = issued + int(ttl)
    # Length-framed rather than delimiter-joined: a subject or bound value
    # containing ":" used to shift the fields, so `verify_token` could read a
    # different subject than `sign_token` wrote.
    body = _frame(purpose, subject, str(expires), bound)
    encoded = _b64(body.encode("utf-8"))
    mac = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), "sha256").hexdigest()
    return f"{encoded}.{mac}"


def verify_token(
    secret: str, purpose: str, token: str, *, bound: str = "", now: float | None = None
) -> str | None:
    """Return the token `subject` if valid/unexpired/purpose-and-bound-matched, else None."""
    try:
        encoded, mac = token.split(".")
        expected = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), "sha256"
        ).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        got_purpose, subject, expires, got_bound = _unframe(
            _unb64(encoded).decode("utf-8"), 4
        )
    except (ValueError, TypeError):
        return None
    if got_purpose != purpose or got_bound != bound:
        return None
    if int(expires) < int(time.time() if now is None else now):
        return None
    return subject


# --- user record + store ----------------------------------------------------


@dataclass(slots=True)
class UserRecord:
    id: str
    email: str
    hashed_password: str
    is_active: bool = True
    is_verified: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0


@runtime_checkable
class UserStore(Protocol):
    """Persistence seam for users — supply your own, or use InMemory/an ORM adapter."""

    async def get_by_email(self, email: str) -> UserRecord | None:
        """The user with this email address, or `None` if there is none.

        The lookup every sign-in and every "forgot password" starts with.
        `None` is an ordinary answer rather than an error, and the flows are
        careful to respond identically whichever it is, so that a caller cannot
        learn which addresses are registered.

        Matching should be case- and whitespace-insensitive, as
        `InMemoryUserStore` is: an address entered with a capital or a trailing
        space is the same account.
        """
        ...

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        """The user with this id, or `None` if there is none.

        The id is `UserRecord.id`, which the store minted in `create` — it is
        also what a verification or reset token carries as its subject, so this
        is what turns a redeemed token back into a user.
        """
        ...

    async def create(self, email: str, hashed_password: str) -> UserRecord:
        """Create a user and return the stored record, with its id assigned.

        `hashed_password` is already hashed — `wreath.users` hashes off the
        event loop before calling, and a store must never be handed or asked to
        handle a plaintext one. New users start `is_active=True` and
        `is_verified=False`; verification is a later `update`.

        Called only after `get_by_email` returned `None`, so a store need not
        make the duplicate check its own — but it is registering an address as
        unique, and a real one should enforce that.
        """
        ...

    async def update(self, user: UserRecord) -> UserRecord:
        """Persist a modified record and return what was stored.

        The whole record, not a patch. The shipped flows use it for exactly two
        things — marking an account verified, and replacing a password hash —
        and both pass a copy of the record they read, so a store may treat the
        record's id as the key.
        """
        ...


def _normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(slots=True)
class InMemoryUserStore:
    """A dict-backed store for dev and tests. Emails are normalized (lower/trim)."""

    _by_id: dict[str, UserRecord] = field(default_factory=dict)
    _by_email: dict[str, str] = field(default_factory=dict)
    _seq: int = 0

    async def get_by_email(self, email: str) -> UserRecord | None:
        """The user registered under this address, trimmed and lower-cased first."""
        user_id = self._by_email.get(_normalize_email(email))
        return self._by_id.get(user_id) if user_id else None

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        """The user with this id, or `None`."""
        return self._by_id.get(user_id)

    async def create(self, email: str, hashed_password: str) -> UserRecord:
        """Store a new user under the next id in a per-instance counter.

        Ids are `"1"`, `"2"`, and so on, which is stable within one process and
        means nothing outside it. The email is normalized before it is stored,
        so the record carries the trimmed, lower-cased form. `created_at` and
        `updated_at` are both set to now.
        """
        self._seq += 1
        now = time.time()
        record = UserRecord(
            id=str(self._seq), email=_normalize_email(email),
            hashed_password=hashed_password, created_at=now, updated_at=now,
        )
        self._by_id[record.id] = record
        self._by_email[record.email] = record.id
        return record

    async def update(self, user: UserRecord) -> UserRecord:
        """Store `user` against its own id, stamping `updated_at` to now.

        An upsert: a record whose id is not present is simply inserted. The
        email index gains an entry for the record's current address, and a
        previous address the record no longer carries is **not** removed from
        it, so a changed email stays resolvable under the old one. That has no
        effect on the shipped flows, which never change an address — but it is a
        reason not to reach for this store beyond development and tests.
        """
        user.updated_at = time.time()
        self._by_id[user.id] = user
        self._by_email[_normalize_email(user.email)] = user.id
        return user


# --- email hook -------------------------------------------------------------


@runtime_checkable
class EmailSender(Protocol):
    """Delivery seam. `SmtpEmailSender` below is the shipped transport."""

    async def send_verification(self, email: str, link: str) -> None:
        """Deliver the "confirm your address" link to a newly registered user.

        `link` is built by the application's `link_builder` and already carries
        the signed token; the sender's job is delivery and nothing else.

        Registration awaits this, so a slow or failing transport is a slow or
        failing registration. It is also the last remaining timing signal that
        distinguishes a new address from one already registered — the flow is
        uniform in every other respect — so a transport that queues rather than
        delivers inline closes that gap as well as the latency one.
        """
        ...

    async def send_password_reset(self, email: str, link: str) -> None:
        """Deliver the password-reset link to a user who asked for one.

        The same contract as `send_verification`, and the same reason to be
        quick: "forgot password" answers identically whether or not the address
        is registered, and only the sender can make the timing match too.
        """
        ...


class LogEmailSender:
    """Default dev sender — prints the link instead of delivering it.

    The default so that a freshly wired application works end to end with no
    SMTP server: the verification link appears wherever `printer` writes, and
    can be pasted into a browser. That also means **every link is printed in
    clear text**, which is exactly what you do not want in production; put
    `SmtpEmailSender` (or your own transport) in front of it there.

    Args:
        printer: Where a line goes. `print` by default; pass a logger's method
            to route it somewhere durable.
    """

    def __init__(self, printer: Callable[[str], None] = print) -> None:
        self._print = printer

    async def send_verification(self, email: str, link: str) -> None:
        """Print one `[wreath.users] verify <email>: <link>` line."""
        self._print(f"[wreath.users] verify {email}: {link}")

    async def send_password_reset(self, email: str, link: str) -> None:
        """Print one `[wreath.users] reset <email>: <link>` line."""
        self._print(f"[wreath.users] reset {email}: {link}")


@dataclass(slots=True)
class CapturingEmailSender:
    """Test sender — records (email, link) pairs instead of sending."""

    verifications: list[tuple[str, str]] = field(default_factory=list)
    resets: list[tuple[str, str]] = field(default_factory=list)

    async def send_verification(self, email: str, link: str) -> None:
        """Append `(email, link)` to `verifications`. Nothing is delivered."""
        self.verifications.append((email, link))

    async def send_password_reset(self, email: str, link: str) -> None:
        """Append `(email, link)` to `resets`. Nothing is delivered."""
        self.resets.append((email, link))


@dataclass(slots=True)
class SmtpEmailSender:
    """Real SMTP delivery via stdlib `smtplib`/`email` (zero-dep).

    STARTTLS by default (or implicit TLS on `port=465`). The blocking
    `smtplib` work runs in a worker thread so the event loop is never blocked.
    Build from env with `from_env`
    (`WREATH_SMTP_HOST`/`_FROM`/`_PORT`/`_USER`/`_PASSWORD`/`_TLS`).
    """

    host: str
    from_addr: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    use_tls: bool = True
    timeout: float = 30.0
    verify_subject: str = "Verify your email"
    reset_subject: str = "Reset your password"

    @classmethod
    def from_env(cls) -> SmtpEmailSender:
        """Build a sender from the `WREATH_SMTP_*` environment variables.

        `WREATH_SMTP_HOST` and `WREATH_SMTP_FROM` are required. `_PORT` defaults
        to 587, `_USER` and `_PASSWORD` to unset (no login), and `_TLS` to on —
        it is off only for the literal values `0`, `false` or `False`, so a
        typo leaves TLS enabled rather than silently disabling it.

        The subjects and the timeout are not read from the environment; set them
        on the instance if you want them changed.

        Raises:
            KeyError: `WREATH_SMTP_HOST` or `WREATH_SMTP_FROM` is not set.
            ValueError: `WREATH_SMTP_PORT` is not an integer.
        """
        return cls(
            host=os.environ["WREATH_SMTP_HOST"],
            from_addr=os.environ["WREATH_SMTP_FROM"],
            port=int(os.environ.get("WREATH_SMTP_PORT", "587")),
            username=os.environ.get("WREATH_SMTP_USER"),
            password=os.environ.get("WREATH_SMTP_PASSWORD"),
            use_tls=os.environ.get("WREATH_SMTP_TLS", "1") not in ("0", "false", "False"),
        )

    async def send_verification(self, email: str, link: str) -> None:
        """Send a plain-text verification mail carrying `link`.

        Subject is `verify_subject`. The connection is opened, used and closed
        for this one message, on a worker thread, so the event loop is never
        blocked by `smtplib`. Anything `smtplib` raises propagates — delivery
        failure is not swallowed here.
        """
        await self._send(email, self.verify_subject, f"Verify your email:\n\n{link}\n")

    async def send_password_reset(self, email: str, link: str) -> None:
        """Send a plain-text password-reset mail carrying `link`.

        Subject is `reset_subject`; otherwise identical to
        `send_verification`, connection handling and all.
        """
        await self._send(email, self.reset_subject, f"Reset your password:\n\n{link}\n")

    async def _send(self, to_addr: str, subject: str, body: str) -> None:
        await asyncio.to_thread(self._send_blocking, to_addr, subject, body)

    def _send_blocking(self, to_addr: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.from_addr
        message["To"] = to_addr
        message["Subject"] = subject
        message.set_content(body)
        context = ssl.create_default_context()
        if self.port == 465:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=context
            )
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            if self.use_tls and self.port != 465:
                smtp.starttls(context=context)
            if self.username is not None and self.password is not None:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
        finally:
            smtp.close()


# --- flow logic (framework-agnostic) ----------------------------------------
#
# Each returns plain data (bool / UserRecord | None) — the router glue in
# wreath.users maps these to JSON responses + session writes. Responses are kept
# uniform for register/forgot so an attacker can't enumerate accounts.

_VERIFY = "verify"
_RESET = "reset"


async def _hash_password_off_loop(password: str) -> str:
    """`hash_password` in a worker thread.

    scrypt at N=2^14 is tens of milliseconds of CPU and ~16 MB of memory. Called
    inline from an async flow it stalls the whole worker -- every other request
    on that process waits for one login -- and the flows below are the only
    callers that are already async. `SmtpEmailSender` in this module already
    treats its blocking work this way.
    """
    return await asyncio.to_thread(hash_password, password)


async def _verify_password_off_loop(password: str, encoded: str) -> bool:
    """`verify_password` in a worker thread. See `_hash_password_off_loop`."""
    return await asyncio.to_thread(verify_password, password, encoded)


async def register(
    store: UserStore,
    mailer: EmailSender,
    *,
    secret: str,
    email: str,
    password: str,
    link_builder: Callable[[str, str], str],
    ttl: int = 24 * 3600,
    now: float | None = None,
) -> None:
    """Create an unverified user (if new) and email a verification link. Uniform, no leak."""
    # Hashed *before* the existence check, so both paths pay the same dominant
    # cost. Returning early on a duplicate spent no CPU at all, which made the
    # uniform response a formality: the latency answered the question anyway.
    # (The remaining signal is the mail send; queue it to remove that too.)
    hashed = await _hash_password_off_loop(password)
    existing = await store.get_by_email(email)
    if existing is not None:
        # Do not reveal existence; optionally the app could email a "you already
        # have an account" notice here. We stay silent + uniform.
        return None
    user = await store.create(email, hashed)
    # Verify tokens are idempotent + expiry-limited; no fingerprint binding (that
    # single-use mechanism is reserved for password resets).
    token = sign_token(secret, _VERIFY, user.id, ttl=ttl, now=now)
    await mailer.send_verification(user.email, link_builder(_VERIFY, token))
    return None


async def authenticate(store: UserStore, email: str, password: str) -> UserRecord | None:
    """Return the user iff credentials are valid AND the account is active.

    Uniform failure, and **unthrottled**: nothing here counts attempts, so a
    caller invoking this directly owns rate limiting. `wreath.users.user_router`
    wraps it with `LoginLimiter`; this module stays stdlib-only and storeless on
    purpose, which is why the guard lives there rather than here.
    """
    user = await store.get_by_email(email)
    if user is None:
        # Spend comparable work to blunt timing-based enumeration.
        await _verify_password_off_loop(password, "scrypt$16384$8$1$AAAA$AAAA")
        return None
    if not await _verify_password_off_loop(password, user.hashed_password):
        return None
    if not user.is_active:
        return None
    return user


async def verify_email(
    store: UserStore, *, secret: str, token: str, now: float | None = None
) -> bool:
    """Mark a user verified from a valid verification token."""
    subject = verify_token(secret, _VERIFY, token, now=now)
    if subject is None:
        return False
    user = await store.get_by_id(subject)
    if user is None:
        return False
    if not user.is_verified:
        await store.update(replace(user, is_verified=True))
    return True


async def start_password_reset(
    store: UserStore,
    mailer: EmailSender,
    *,
    secret: str,
    email: str,
    link_builder: Callable[[str, str], str],
    ttl: int = 3600,
    now: float | None = None,
) -> None:
    """Email a reset link if the account exists. Uniform response regardless."""
    user = await store.get_by_email(email)
    if user is None:
        # Mint and discard, so the work is the same shape either way. The mail
        # send remains the honest asymmetry; queue it to remove that too.
        sign_token(secret, _RESET, "", ttl=ttl, bound="", now=now)
        return None
    token = sign_token(secret, _RESET, user.id, ttl=ttl,
                       bound=fingerprint(user.hashed_password), now=now)
    await mailer.send_password_reset(user.email, link_builder(_RESET, token))
    return None


async def reset_password(
    store: UserStore, *, secret: str, token: str, new_password: str, now: float | None = None
) -> bool:
    """Set a new password from a valid, single-use reset token.

    The token's fingerprint is bound to the password hash that was current when it
    was minted, so we peek the (still-untrusted) subject, load that user, then
    verify the token against the user's CURRENT fingerprint — a token minted
    before the latest password change fails, giving single-use semantics.
    """
    subject = _token_subject(token)
    if subject is None:
        return False
    user = await store.get_by_id(subject)
    if user is None:
        return False
    if verify_token(secret, _RESET, token, now=now,
                    bound=fingerprint(user.hashed_password)) != user.id:
        return False
    try:
        hashed = await _hash_password_off_loop(new_password)
    except ValueError:
        # An empty or over-long new password is a caller error, not a server
        # fault: it used to raise out of `hash_password` and become a 500 on an
        # input a form can submit.
        return False
    await store.update(replace(user, hashed_password=hashed))
    return True


def _token_subject(token: str) -> str | None:
    """Peek the subject from a token WITHOUT trusting it (caller must verify)."""
    try:
        encoded = token.split(".", 1)[0]
        return _unframe(_unb64(encoded).decode("utf-8"), 4)[1]
    except (ValueError, TypeError, IndexError):
        return None
