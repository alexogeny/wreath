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
import email.policy
import hashlib
import hmac
import os
import smtplib
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ._b64 import b64url_decode, b64url_encode
from ._dkim import DkimSigner

__all__ = [
    "CapturingEmailSender",
    "EmailSender",
    "InMemorySuppressionList",
    "InMemoryUserStore",
    "LogEmailSender",
    "MailClass",
    "Message",
    "SmtpEmailSender",
    "SuppressionList",
    "SuppressionReason",
    "Unsubscribe",
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


#: Named locally because `_unb64` below is its inverse and the pair reads as
#: one. The implementation is shared; see `wreath._b64.b64url_encode`.
_b64 = b64url_encode


def _unb64(text: str) -> bytes:
    """The inverse of `_b64`, which strips padding, so the input is unpadded."""
    return b64url_decode(text)


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
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
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
            password.encode("utf-8"),
            salt=_unb64(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except ValueError, TypeError:
        return False
    return hmac.compare_digest(dk, expected)


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
        expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), "sha256").hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        got_purpose, subject, expires, got_bound = _unframe(_unb64(encoded).decode("utf-8"), 4)
    except ValueError, TypeError:
        return None
    if got_purpose != purpose or got_bound != bound:
        return None
    if int(expires) < int(time.time() if now is None else now):
        return None
    return subject


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
    """Persistence seam for users — supply your own, or use InMemory/an ORM adapter.

    **A store that does not hold its own data should declare a `store_id`.**
    `wreath.users` has to decide whether a `user_router` and a
    `second_factor_router` serve the same users, and two objects over one
    database are two objects: `OrmUserStore(session, model)` keeps nothing, so
    building one inline for each router is a mistake that reads as correct at
    the call site. `store_id` is any hashable value equal across the stores that
    share rows — the ORM adapters return their model class. Declaring none means
    "this object is the identity", which is right for `InMemoryUserStore` and
    for anything else that owns what it serves.
    """

    #: Optional. What decides which rows this store serves, when that is not the
    #: object itself. See the class docstring; `wreath.users._same_store` reads
    #: it, and a store that declares nothing is matched by identity.
    store_id: object

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

    async def get_many_by_id(self, user_ids: Iterable[str]) -> list[UserRecord | None]:
        """Return one result per supplied id, in the same order.

        Missing ids produce `None`, and repeated ids repeat their result. This
        exact shape lets a caller retain its own ordering while a database store
        serves the whole request with one query.
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

    async def get_many_by_id(self, user_ids: Iterable[str]) -> list[UserRecord | None]:
        """One ordered dictionary lookup per id, retaining repeats and misses."""
        return [self._by_id.get(user_id) for user_id in user_ids]

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
            id=str(self._seq),
            email=_normalize_email(email),
            hashed_password=hashed_password,
            created_at=now,
            updated_at=now,
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

    async def send(self, message: Message) -> None:
        """Print one line for `message`, including its class. Nothing is sent."""
        self._print(f"[wreath.notifications] {message.mail_class} {message.to}: {message.subject}")


@dataclass(slots=True)
class CapturingEmailSender:
    """Test sender — records (email, link) pairs instead of sending."""

    verifications: list[tuple[str, str]] = field(default_factory=list)
    resets: list[tuple[str, str]] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    suppression: SuppressionList | None = None

    async def send_verification(self, email: str, link: str) -> None:
        """Append `(email, link)` to `verifications`. Nothing is delivered."""
        self.verifications.append((email, link))

    async def send_password_reset(self, email: str, link: str) -> None:
        """Append `(email, link)` to `resets`. Nothing is delivered."""
        self.resets.append((email, link))

    async def send(self, message: Message) -> None:
        """Append `message` to `messages`, applying suppression if one is set.

        The suppression check is duplicated here rather than inherited so that a
        test using this sender exercises the same refusal a real one would --
        a capturing double that accepts what production refuses is how a
        suppression bug reaches production green.
        """
        if message.mail_class is MailClass.MARKETING and self.suppression is not None:
            reason = await self.suppression.reason(message.to)
            if reason is not None:
                raise SuppressedError(f"{message.to} is suppressed ({reason})")
        self.messages.append(message)


class MailClass(StrEnum):
    """Whether a message is operational or promotional.

    **There is deliberately no default.** The distinction is a legal question
    with a technical encoding, and both wrong answers are expensive: call
    everything transactional and you are a non-compliant bulk sender under the
    Google/Yahoo/Microsoft rules that carry a permanent 550 since May 2026; call
    everything marketing and a suppressed address stops receiving its own
    password resets, which reads to the user as a broken account.

    So the caller states it, per message, and `Message` gives the field no
    default so that "I did not think about it" cannot compile.
    """

    #: Operational mail the recipient asked for by acting: verification, a
    #: password reset, a receipt, a security alert. Exempt from RFC 8058
    #: one-click unsubscribe, and **delivered even to a suppressed address** --
    #: a bounce complaint about a newsletter must not lock someone out of their
    #: own account.
    TRANSACTIONAL = "transactional"
    #: Promotional or discretionary mail: newsletters, digests, re-engagement.
    #: Requires an `Unsubscribe`, and is refused for a suppressed address.
    MARKETING = "marketing"


class SuppressionReason(StrEnum):
    """Why an address is on the suppression list."""

    #: The receiving server refused permanently (a 5xx at SMTP time).
    HARD_BOUNCE = "hard_bounce"
    #: The recipient pressed "this is spam".
    COMPLAINT = "complaint"
    #: The recipient used the unsubscribe link.
    UNSUBSCRIBED = "unsubscribed"
    #: An operator suppressed it by hand.
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Unsubscribe:
    """The RFC 8058 one-click unsubscribe target for one message.

    `url` must be **https** and must accept a `POST` carrying
    `List-Unsubscribe=One-Click`; the mailbox provider posts to it directly, with
    no human in the loop and no confirmation page. A `mailto:` alternative is
    allowed alongside it and is what older clients use.

    A visible "unsubscribe" link in the body does **not** satisfy the
    requirement. The headers are the requirement.
    """

    url: str
    mailto: str | None = None

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError(
                "a one-click unsubscribe URL must be https, because the provider "
                f"POSTs to it unattended; got {self.url!r}"
            )

    def header(self) -> str:
        targets = [f"<{self.url}>"]
        if self.mailto:
            targets.insert(0, f"<{self.mailto}>")
        return ", ".join(targets)


@dataclass(frozen=True, slots=True)
class Message:
    """One outgoing message, with its class stated rather than inferred."""

    to: str
    subject: str
    body: str
    #: No default, on purpose. See `MailClass`.
    mail_class: MailClass
    unsubscribe: Unsubscribe | None = None
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.mail_class is MailClass.MARKETING and self.unsubscribe is None:
            raise ValueError(
                "marketing mail requires a one-click unsubscribe target (RFC 8058); "
                "pass Unsubscribe(url=...), or send it as MailClass.TRANSACTIONAL "
                "if it really is operational mail"
            )


@runtime_checkable
class SuppressionList(Protocol):
    """Addresses that must not receive marketing mail.

    Consulted **by the thing that sends**, on every send. A list held at an
    email service provider does not stop a self-hosted SMTP path from mailing
    someone who complained, and the complaint rate that results is measured
    against the sending domain either way.
    """

    async def reason(self, email: str) -> SuppressionReason | None:
        """Why this address is suppressed, or `None` if it is not."""
        ...

    async def suppress(self, email: str, reason: SuppressionReason) -> None:
        """Add an address, or replace the reason it is already there for."""
        ...

    async def release(self, email: str) -> None:
        """Remove an address, for a re-subscribe or an operator correction."""
        ...


@dataclass(slots=True)
class InMemorySuppressionList:
    """A dict-backed suppression list for development and tests.

    Addresses are normalized the same way `InMemoryUserStore` normalizes them,
    so a capital or a trailing space is the same recipient. A real deployment
    wants this in the database, because a suppression that a restart forgets is
    a complaint the recipient gets to make twice.
    """

    _entries: dict[str, SuppressionReason] = field(default_factory=dict)

    async def reason(self, email: str) -> SuppressionReason | None:
        """The reason this address is suppressed, or `None`."""
        return self._entries.get(_normalize_email(email))

    async def suppress(self, email: str, reason: SuppressionReason) -> None:
        """Record `email` as suppressed for `reason`."""
        self._entries[_normalize_email(email)] = reason

    async def release(self, email: str) -> None:
        """Forget any suppression for `email`. A no-op if there is none."""
        self._entries.pop(_normalize_email(email), None)


class SuppressedError(Exception):
    """A marketing message was addressed to a suppressed recipient."""


@dataclass(slots=True)
class SmtpEmailSender:
    """Real SMTP delivery via stdlib `smtplib`/`email` (zero-dep).

    STARTTLS by default (or implicit TLS on `port=465`). The blocking
    `smtplib` work runs in a worker thread so the event loop is never blocked.
    Build from env with `from_env`
    (`WREATH_SMTP_HOST`/`_FROM`/`_PORT`/`_USER`/`_PASSWORD`/`_TLS`).

    Three things beyond the transport, each of which a 2026 sender needs and
    none of which an SMTP library supplies:

    * **`dkim`** signs the exact bytes handed to the MTA. Pair it with SPF and a
      DMARC policy; `wreath.doctor.check_email_deliverability` reports whether
      the DNS actually backs the configuration up, which is the failure this
      class would otherwise be silent about.
    * **`suppression`** is consulted before every send. Marketing mail to a
      suppressed address is refused; transactional mail is delivered, because a
      newsletter complaint must not cost someone their password reset.
    * **`Message.mail_class`** decides both of those, and has no default.

    Counters (`sent`, `suppressed`, `transport_failures`) are readable at any
    time. They exist because the failure mode here is silence: mail that
    "sent", nothing raised, and nobody received it.
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
    dkim: DkimSigner | None = None
    suppression: SuppressionList | None = None
    #: Delivered, refused as suppressed, and failed at the transport. Read them;
    #: a rising `transport_failures` with a flat `sent` is the shape of an
    #: outage that no exception reaches the application.
    sent: int = 0
    suppressed: int = 0
    transport_failures: int = 0

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

        Subject is `verify_subject`. Transactional, so it reaches a suppressed
        address: someone who unsubscribed from a newsletter still has to be able
        to confirm their own account. The connection is opened, used and closed
        for this one message, on a worker thread, so the event loop is never
        blocked by `smtplib`. Anything `smtplib` raises propagates — delivery
        failure is not swallowed here.
        """
        await self._send_link(email, link, self.verify_subject, "Verify your email")

    async def send_password_reset(self, email: str, link: str) -> None:
        """Send a plain-text password-reset mail carrying `link`.

        Subject is `reset_subject`; otherwise identical to
        `send_verification`, transactional class and all.
        """
        await self._send_link(email, link, self.reset_subject, "Reset your password")

    async def _send_link(self, email: str, link: str, subject: str, instruction: str) -> None:
        await self.send(
            Message(
                to=email,
                subject=subject,
                body=f"{instruction}:\n\n{link}\n",
                mail_class=MailClass.TRANSACTIONAL,
            )
        )

    async def send(self, message: Message) -> None:
        """Deliver one `Message`, honouring its class.

        Raises:
            SuppressedError: `message` is marketing and its recipient is
                suppressed. Raised rather than returned quietly, because a
                caller that keeps sending to a suppressed list is the thing that
                drives a domain past the 0.10% complaint threshold, and it
                should find out.
            OSError: the transport failed. Counted in `transport_failures`
                first, then re-raised — see the note in `_deliver`.
        """
        if message.mail_class is MailClass.MARKETING and self.suppression is not None:
            reason = await self.suppression.reason(message.to)
            if reason is not None:
                self.suppressed += 1
                raise SuppressedError(
                    f"{message.to} is suppressed ({reason}); marketing mail is refused. "
                    "Transactional mail to this address is still delivered."
                )
        await asyncio.to_thread(self._deliver, message)
        self.sent += 1

    def build(self, message: Message) -> bytes:
        """Serialise `message` to the exact bytes that go on the wire.

        Public because it is what the tests sign and verify, and because a
        caller integrating a different transport needs the same bytes: **the
        signature covers these bytes and no others.** Re-serialising an
        `EmailMessage` after signing it is the most common way a valid DKIM
        signature becomes an invalid one, which is why this returns bytes and
        the sender hands those bytes straight to `sendmail`.
        """
        built = EmailMessage()
        built["From"] = self.from_addr
        built["To"] = message.to
        built["Subject"] = message.subject
        # `Date` is mandatory (RFC 5322 §3.6.1) and `Message-ID` is close to it
        # in practice: a message missing either is scored as unusual by every
        # major filter, and neither is added by `EmailMessage` on its own. Both
        # are in `DEFAULT_SIGNED_HEADERS`, so they are covered by the signature
        # rather than free for a relay to rewrite.
        built["Date"] = formatdate(localtime=False, usegmt=True)
        built["Message-ID"] = make_msgid(domain=self._message_id_domain())
        if message.unsubscribe is not None:
            built["List-Unsubscribe"] = message.unsubscribe.header()
            # RFC 8058. Both headers, or the provider renders its own
            # "unsubscribe" affordance from neither.
            built["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        for name, value in message.headers:
            built[name] = value
        built.set_content(message.body)
        raw = built.as_bytes(policy=email.policy.SMTP)
        if self.dkim is None:
            return raw
        signature = self.dkim.sign(raw)
        return b"DKIM-Signature: " + signature.encode("utf-8") + b"\r\n" + raw

    def _message_id_domain(self) -> str:
        """The domain a generated `Message-ID` is scoped to.

        The DKIM signing domain when there is one, so the identifier aligns with
        what DMARC checks; otherwise whatever follows the `@` in `from_addr`.
        `make_msgid`'s own default is the sending *host's* FQDN, which on a
        container is a name that resolves nowhere and reads as forged.
        """
        if self.dkim is not None:
            return self.dkim.domain
        _, _, domain = self.from_addr.rpartition("@")
        return domain.strip("<>") or "localhost"

    def _deliver(self, message: Message) -> None:
        raw = self.build(message)
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
            # `sendmail` rather than `send_message`, so the bytes DKIM signed are
            # the bytes transmitted. `send_message` re-serialises from the
            # `EmailMessage` under its own policy, which can refold a header and
            # invalidate the signature over it.
            smtp.sendmail(self.from_addr, [message.to], raw)
        except OSError, smtplib.SMTPException:
            # Counted, then re-raised. Not swallowed: this is the exact site
            # where "the mail sent, nothing raised, nobody got it" comes from,
            # and the counter is what makes an outage visible to an operator
            # whose application never sees the exception because a retrying job
            # absorbed it.
            self.transport_failures += 1
            raise
        finally:
            smtp.close()


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
    token = sign_token(
        secret, _RESET, user.id, ttl=ttl, bound=fingerprint(user.hashed_password), now=now
    )
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
    if (
        verify_token(secret, _RESET, token, now=now, bound=fingerprint(user.hashed_password))
        != user.id
    ):
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
    except ValueError, TypeError, IndexError:
        return None
