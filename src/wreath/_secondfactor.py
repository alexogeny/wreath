"""The core of second factors: TOTP, recovery codes, WebAuthn, and their store.

TOTP, hashed single-use recovery codes, WebAuthn registration and assertion,
including discoverable passkey login, and the credential store seam all three
share. The wreath-coupled router glue lives in
`wreath.users`; this module is the part that can be reasoned about, and
unit-tested, without an ASGI app. The WebAuthn *wire formats* -- CBOR, COSE,
authenticator data, DER signatures -- live one module along in
`wreath._webauthn`, so what is left here is the flow.

**There is no cryptography implemented here.** TOTP is HMAC-SHA1 over a
big-endian counter (RFC 6238/4226), which is `hmac`, `hashlib` and `struct` from
the standard library, and the comparison is `hmac.compare_digest`. Recovery
codes are hashed with **one pass of SHA-256** -- not the password hasher -- and
`hash_recovery_code` explains at length why that is the right hash for this
secret and the wrong one for a password.

Four things in here are load-bearing security properties rather than
conveniences, and every one is enforced with a `raise` or a refusal rather than
an `assert`, since `python -O` deletes assertions:

* **Replay.** `verify_totp` will not accept a counter that is less than or equal
  to the last one accepted for that credential, and `SecondFactorStore.touch`
  advances a stored counter **only when the new one is strictly greater**,
  reporting whether it won. A TOTP code is valid for a whole step on every
  device that knows the secret; without this, watching one code being typed is
  enough to sign in for the rest of that step. The read and the advance are two
  operations with real awaits between them, so "did the counter go up" is not a
  question the caller can answer by looking -- two requests carrying the same
  code both read the same stored counter, and only the store can say which of
  them actually moved it. `verify_second_factor` therefore treats a lost advance
  exactly as it treats a counter that was already spent: as a replay.
* **Two-phase enrolment.** `begin_totp_enrolment` mints a secret and returns it;
  it touches no store. Only `confirm_totp_enrolment`, given a code that verifies
  against that secret, writes a credential. A secret the user never successfully
  entered can therefore never lock them out.
* **A WebAuthn ceremony answers one challenge, for one relying party, at one
  origin.** `confirm_webauthn_registration` and `verify_webauthn_assertion`
  check the client data's `type`, its `challenge` and its `origin`, and the
  authenticator data's RP ID hash, each as its own refusal. Keeping the
  challenge single-use is the caller's half of the bargain; `wreath.users`
  holds it in a `SessionStore` and deletes it on every exit path.
* **A signature counter never goes backwards.** An authenticator that reports a
  counter and then reports a lower one has been cloned. An authenticator that
  reports zero forever has simply not implemented the counter, which is common
  and legitimate, so zero is read as "not reported" rather than as a regression.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, runtime_checkable
from urllib.parse import quote

from ._userkit import verify_password
from ._webauthn import (
    WebAuthnError,
    b64url_encode,
    check_client_data,
    check_rp_id_hash,
    pack_credential,
    parse_attestation_object,
    parse_authenticator_data,
    parse_cose_key,
    unpack_credential,
    verify_signature,
)

__all__ = [
    "BulkSecondFactorRemovalStore",
    "BulkSecondFactorStore",
    "DEFAULT_DIGITS",
    "DEFAULT_PERIOD",
    "DEFAULT_SKEW",
    "MAX_SKEW",
    "InMemorySecondFactorStore",
    "DiscoverableSecondFactorStore",
    "SecondFactor",
    "SecondFactorStore",
    "TotpEnrolment",
    "WebAuthnAssertion",
    "WebAuthnCeremony",
    "WebAuthnError",
    "base32_to_secret",
    "begin_totp_enrolment",
    "begin_webauthn_assertion",
    "begin_webauthn_registration",
    "confirm_totp_enrolment",
    "confirm_webauthn_registration",
    "generate_recovery_codes",
    "generate_totp_secret",
    "hash_recovery_code",
    "remove_second_factor",
    "secret_to_base32",
    "totp_code",
    "totp_counter",
    "totp_uri",
    "verify_recovery_code",
    "verify_second_factor",
    "verify_totp",
    "verify_webauthn_assertion",
]


#: Digits in a generated code. Six is what every authenticator app shows.
DEFAULT_DIGITS = 6

#: Seconds per TOTP step. Thirty is RFC 6238's own default and effectively
#: universal; changing it means every enrolled authenticator must be told.
DEFAULT_PERIOD = 30

#: Steps of clock skew accepted either side of the current one. One step is the
#: standard's own suggestion: it forgives a slow phone and a slow finger, and it
#: widens the window an observed code is useful in by exactly one step.
DEFAULT_SKEW = 1

#: The largest skew this module will accept at all. A configurable skew is
#: reasonable; a skew of a hundred steps is a fifty-minute password, so the
#: knob has a stop on it rather than trusting a caller's arithmetic.
MAX_SKEW = 10

#: Bytes of shared secret. RFC 4226 requires at least 128 bits and recommends
#: 160, which is also HMAC-SHA1's block-relevant length.
DEFAULT_SECRET_BYTES = 20
MIN_SECRET_BYTES = 16

#: Recovery codes issued at enrolment, and the entropy in each. Ten codes of
#: eighty bits is far past guessing, and few enough to print.
DEFAULT_RECOVERY_CODES = 10
_RECOVERY_CODE_CHARS = 16
_RECOVERY_GROUP = 4

#: Deliberately missing 0/1/i/l/o/u: a recovery code is transcribed by a human
#: from paper, under stress, having lost the phone.
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstvwxyz23456789"

_ALGORITHMS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}

#: What a `kind` may be. Only `totp` and `recovery` are issued in stage one;
#: `webauthn` is reserved so the stored shape does not change under it later.
Kind = Literal["totp", "webauthn", "recovery"]
_KINDS = frozenset(("totp", "webauthn", "recovery"))


def totp_code(
    secret: bytes,
    counter: int,
    *,
    digits: int = DEFAULT_DIGITS,
    algorithm: str = "sha1",
) -> str:
    """The HOTP value for `secret` at `counter`, zero-padded to `digits`.

    This is RFC 4226 section 5.3 verbatim: HMAC the eight-byte big-endian
    counter, take the low nibble of the last byte as an offset, read four bytes
    there, clear the top bit, and reduce modulo a power of ten. RFC 6238 is the
    same function with the counter defined as elapsed steps rather than a
    stored count, which is what `totp_counter` computes.

    Args:
        secret: the shared secret, raw bytes (not base32).
        counter: the step number; never negative.
        digits: 6 to 10. Six is what authenticator apps display; the standard's
            published test vectors are eight, which is why this is a parameter.
        algorithm: `sha1`, `sha256` or `sha512`. SHA-1 is not a weakness here --
            HMAC-SHA1 is unaffected by the collision attacks that retired bare
            SHA-1 -- and it is the only one authenticator apps universally read.

    Raises:
        ValueError: a negative counter, a digit count outside 6..10, an unknown
            algorithm, or a secret shorter than `MIN_SECRET_BYTES`.
    """
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"a TOTP secret must be at least {MIN_SECRET_BYTES} bytes")
    if counter < 0:
        raise ValueError("counter must not be negative")
    if not 6 <= digits <= 10:
        raise ValueError("digits must be between 6 and 10")
    digest_factory = _ALGORITHMS.get(algorithm)
    if digest_factory is None:
        raise ValueError(f"unsupported TOTP algorithm: {algorithm!r}")
    digest = hmac.new(bytes(secret), struct.pack(">Q", counter), digest_factory).digest()
    offset = digest[-1] & 0x0F
    truncated = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def totp_counter(at: float | None = None, *, period: int = DEFAULT_PERIOD) -> int:
    """Steps of `period` seconds elapsed since the Unix epoch at `at` (default now).

    Raises:
        ValueError: a period that is not positive, or a time before the epoch.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    moment = time.time() if at is None else at
    if moment < 0:
        raise ValueError("timestamp precedes the Unix epoch")
    return int(moment // period)


def _normalize_code(code: str) -> str:
    return code.strip().replace(" ", "").replace("-", "")


def verify_totp(
    secret: bytes,
    code: str,
    *,
    at: float | None = None,
    period: int = DEFAULT_PERIOD,
    digits: int = DEFAULT_DIGITS,
    skew: int = DEFAULT_SKEW,
    algorithm: str = "sha1",
    last_counter: int = 0,
) -> int | None:
    """Return the step `code` was valid for, or None. **Replay-aware.**

    Every step from `current - skew` to `current + skew` is tried, and any step
    at or below `last_counter` is skipped. That skip is the replay defence, and
    it lives here rather than in the caller on purpose: a code is valid for a
    whole step to everyone who can see it, so accepting the same step twice
    turns a glance over a shoulder, or a phishing proxy, into a working second
    factor for the rest of that step. Store the returned counter and hand it
    back as `last_counter` next time -- `verify_second_factor` does exactly that
    through `SecondFactorStore.touch`.

    Comparison is `hmac.compare_digest`, and the loop does not stop at the first
    match, so neither the value nor the position of a match is visible in the
    time this takes. A code of the wrong length or with a non-digit in it is
    refused before any HMAC is computed; the length of a code is public.

    Args:
        last_counter: the newest step already accepted for this credential.
            Zero means none has been -- step zero is January 1970, so nothing is
            given up by using it as the sentinel.

    Returns:
        The matched step, strictly greater than `last_counter`, or None.

    Raises:
        ValueError: a negative skew, a skew above `MAX_SKEW`, or any of the
            parameter errors `totp_code` and `totp_counter` raise.
    """
    if skew < 0:
        raise ValueError("skew must not be negative")
    if skew > MAX_SKEW:
        raise ValueError(f"skew must not exceed {MAX_SKEW} steps")
    candidate = _normalize_code(code)
    # `isascii()` as well as `isdigit()`, and that pair is load-bearing rather
    # than belt and braces: `str.isdigit` is true of `'١'` and of `'²'`, while
    # `hmac.compare_digest` *raises* `TypeError` on a `str` with a non-ASCII
    # character in it. Without the first half, six Arabic-Indic digits reach the
    # comparison and the verify endpoint answers 500 instead of refusing --
    # uncounted by the throttle, because the failure never returns to it.
    if len(candidate) != digits or not candidate.isascii() or not candidate.isdigit():
        return None
    current = totp_counter(at, period=period)
    matched: int | None = None
    for counter in range(current - skew, current + skew + 1):
        if counter < 0 or counter <= last_counter:
            continue
        expected = totp_code(secret, counter, digits=digits, algorithm=algorithm)
        if hmac.compare_digest(expected, candidate):
            matched = counter
    return matched


def generate_totp_secret(size: int = DEFAULT_SECRET_BYTES) -> bytes:
    """A fresh random shared secret of `size` bytes from `os.urandom`.

    Raises:
        ValueError: fewer than `MIN_SECRET_BYTES` (128 bits, RFC 4226's floor).
    """
    if size < MIN_SECRET_BYTES:
        raise ValueError(f"a TOTP secret must be at least {MIN_SECRET_BYTES} bytes")
    return os.urandom(size)


def secret_to_base32(secret: bytes) -> str:
    """The unpadded base32 of a secret -- how a user types one in by hand."""
    return base64.b32encode(bytes(secret)).decode("ascii").rstrip("=")


def base32_to_secret(text: str) -> bytes:
    """The inverse of `secret_to_base32`, tolerant of missing padding and case.

    Raises:
        ValueError: the text is not base32, or decodes to fewer than
            `MIN_SECRET_BYTES` bytes. `binascii.Error`, which `b32decode` raises,
            is a `ValueError`, so one `except ValueError` covers both.
    """
    packed = text.strip().replace(" ", "").replace("-", "").upper()
    secret = base64.b32decode(packed + "=" * (-len(packed) % 8))
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"a TOTP secret must be at least {MIN_SECRET_BYTES} bytes")
    return secret


def totp_uri(
    secret: bytes,
    *,
    account: str,
    issuer: str = "",
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    algorithm: str = "sha1",
) -> str:
    """Build the `otpauth://totp/...` URI an authenticator app scans as a QR code.

    The de-facto format (Google's, which every app implements): a label of
    `issuer:account`, then the secret in base32 and the parameters as a query.
    The issuer appears twice -- in the label and as a parameter -- because older
    apps read one and newer ones read the other.

    **The URI contains the secret in clear.** It is meant for one pair of eyes,
    over TLS, and belongs in a response body or a QR image, never in a log line
    or a URL that a proxy will record.

    Args:
        account: how the entry is named in the app, usually an email address.
        issuer: the application's name; omitted from the label when empty.

    Raises:
        ValueError: an empty account, or an account or issuer containing a colon
            (which would move the boundary between the two halves of the label).
    """
    if not account:
        raise ValueError("account must not be empty")
    if ":" in account or ":" in issuer:
        raise ValueError("account and issuer must not contain ':'")
    label = f"{issuer}:{account}" if issuer else account
    query = [
        f"secret={secret_to_base32(secret)}",
        f"algorithm={algorithm.upper()}",
        f"digits={int(digits)}",
        f"period={int(period)}",
    ]
    if issuer:
        query.append(f"issuer={quote(issuer, safe='')}")
    return f"otpauth://totp/{quote(label, safe='')}?" + "&".join(query)


def generate_recovery_codes(count: int = DEFAULT_RECOVERY_CODES) -> list[str]:
    """Mint `count` single-use recovery codes, formatted for a human to copy.

    Each is 16 characters from a 30-symbol alphabet -- about 78 bits, which is
    not guessable -- grouped in fours with hyphens, and drawn from an alphabet
    with no `0`, `1`, `i`, `l`, `o` or `u` in it, because these get read off
    paper by somebody who has just lost their phone. Hyphens and case are
    ignored when one is redeemed.

    These are the plaintext. Show them once, store only what
    `confirm_totp_enrolment` stores, which is `hash_recovery_code` of each.

    Raises:
        ValueError: a count below 1 or above 50.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if count > 50:
        raise ValueError("count must not exceed 50")
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_CODE_CHARS))
        codes.append(
            "-".join(
                raw[at : at + _RECOVERY_GROUP]
                for at in range(0, _RECOVERY_CODE_CHARS, _RECOVERY_GROUP)
            )
        )
    return codes


def _normalize_recovery_code(code: str) -> str:
    return code.strip().lower().replace("-", "").replace(" ", "")


#: Names the scheme in a stored hash. A stored value *without* this prefix was
#: written by stage one, which hashed recovery codes with the password hasher;
#: `verify_recovery_code` still reads those so that an upgrade does not silently
#: invalidate every recovery code a deployment has already handed out.
_RECOVERY_SCHEME = "sha256$"


def hash_recovery_code(code: str) -> str:
    """The stored form of one recovery code: `sha256$` and a hex SHA-256 digest.

    **One hash pass, not a password KDF, and that is the considered choice
    rather than a shortcut.** scrypt, bcrypt and argon2 are slow on purpose
    because a *password* is low-entropy: a human picks it, so an offline
    attacker holding the hashes guesses from a list of a few billion and the
    only defence is to make each guess expensive. A recovery code here is
    sixteen characters drawn uniformly from a thirty-symbol alphabet -- about 78
    bits -- so there is no list to guess from and no feasible search at any cost
    per guess. Paying a KDF for it buys nothing and costs plenty: stage one
    spent ~0.4s of a worker thread minting ten of them at enrolment, and walked
    all ten scrypt hashes on every wrong code entered. A single SHA-256 is what
    GitHub and Django store recovery codes as, for the same reason.

    Unsalted, likewise deliberately. A salt defeats a precomputed table, and a
    table over a 78-bit space cannot be built; two users who somehow drew the
    same code would hash alike, which reveals nothing anyone can act on.

    **Passwords are unaffected.** `wreath._userkit.hash_password` still stores
    them with scrypt, and nothing here changes that.

    Args:
        code: the plaintext, in any of the forms a human types it -- hyphens,
            spaces and case are normalized away before hashing, exactly as
            `verify_recovery_code` normalizes what it is given.
    """
    packed = _normalize_recovery_code(code)
    return _RECOVERY_SCHEME + hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _is_legacy_recovery_hash(stored: str) -> bool:
    """Whether `stored` is a stage-one scrypt hash rather than a SHA-256 one.

    Kept separate because the two verify at very different costs: the SHA-256
    branch is inline, and the scrypt branch has to go to a worker thread or it
    stalls the event loop for ~40ms per stored code.
    """
    return not stored.startswith(_RECOVERY_SCHEME)


def verify_recovery_code(code: str, stored: str) -> bool:
    """Whether `code` redeems against `stored`. Constant-time, and never `==`.

    Reads both forms: a `sha256$` hash this module writes, and a stage-one
    `scrypt$` hash, which is verified through `wreath._userkit.verify_password`.
    The legacy branch is read-only -- nothing mints one any more.

    The comparison is `hmac.compare_digest` over the hex digests. A `==` there
    would stop at the first differing character, and the time it took would say
    how much of a guess was right.
    """
    candidate = _normalize_recovery_code(code)
    if not candidate:
        return False
    if _is_legacy_recovery_hash(stored):
        return verify_password(candidate, stored)
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
        stored[len(_RECOVERY_SCHEME) :],
    )


@dataclass(frozen=True, slots=True)
class SecondFactor:
    """One enrolled second factor: a TOTP secret, or one recovery code's hash.

    Frozen, because the two mutable parts -- the counter and the last-used
    stamp -- move through `SecondFactorStore.touch` so that a store can enforce
    that the counter only ever goes forwards. Read a changed copy back from the
    store rather than assigning to one of these.

    `material` is deliberately opaque and deliberately not in the `repr`: for a
    TOTP credential it *is* the shared secret, and a dataclass `repr` reaches
    logs, tracebacks and error reporters without anyone choosing to put it
    there. Nothing in `wreath.users` ever renders it.

    Args:
        kind: `totp` or `recovery` today; `webauthn` is accepted and reserved.
        label: shown to the user when listing factors ("iPhone", "1Password").
        counter: for TOTP, the newest step accepted; 0 means none yet.

    Raises:
        ValueError: an empty id or user id, or a kind outside the three above.
    """

    id: str
    user_id: str
    kind: Kind
    label: str
    created_at: datetime
    last_used_at: datetime | None
    material: bytes = field(repr=False)
    counter: int = 0

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("a second factor needs an id")
        if not self.user_id:
            raise ValueError("a second factor needs a user id")
        if self.kind not in _KINDS:
            raise ValueError(f"unknown second-factor kind: {self.kind!r}")
        if self.counter < 0:
            raise ValueError("counter must not be negative")


@runtime_checkable
class SecondFactorStore(Protocol):
    """Persistence seam for second factors, sibling to `UserStore`.

    A separate protocol rather than four more methods on `UserStore`: a user has
    zero or many credentials, and `get`/`create`/`update` is the wrong shape for
    a collection. `InMemorySecondFactorStore` and
    `wreath.users.OrmSecondFactorStore` both implement it.

    An implementation owes two guarantees the flows rely on and cannot check:

    * `remove` must be scoped to `user_id`, so one user cannot delete another's
      factor by id.
    * **`touch` must be an atomic conditional advance**, and must report whether
      it won. It stores `counter` and `at` only when `counter` is strictly
      greater than the counter already stored, and returns True exactly when it
      did. A credential that is not there loses, like any other non-advance.

      A plain `UPDATE ... SET counter = $1` is not enough, and that is the whole
      reason this method returns something. Verification reads the credential,
      awaits, checks the code, awaits, and only then writes -- so two requests
      carrying the same observed code both read the same stored counter and both
      write it, and the second one succeeds. Racing the legitimate user with a
      code read over their shoulder is the attack the counter exists to stop, so
      the comparison has to happen where the write happens: `WHERE counter < $1`
      in the same statement, `test_and_set` in the same non-suspending function.
    """

    async def credentials(self, user_id: str) -> list[SecondFactor]:
        """Every credential registered to `user_id`, or an empty list.

        A user with no second factor is an empty list, never `None` — "has none"
        is an answer, and the flows read it as one.
        """
        ...

    async def add(self, credential: SecondFactor) -> SecondFactor:
        """Store `credential` and return what was stored.

        The id is the caller's, not the store's, and must be treated as taken:
        overwriting an existing one would drop a live credential and, for TOTP,
        the replay counter with it. `InMemorySecondFactorStore` raises
        `ValueError` on a duplicate id and an implementation should do the same
        rather than replace.
        """
        ...

    async def remove(self, user_id: str, credential_id: str) -> None:
        """Delete `credential_id`, **only** if it belongs to `user_id`.

        The ownership check is the implementation's to make and is not optional:
        the credential id is the only thing a caller supplies, so a store that
        deletes by id alone lets any user delete any user's factor.

        Removing something that is not there is not an error. The outcome asked
        for is that the credential is gone, and it is.
        """
        ...

    async def touch(self, credential_id: str, *, counter: int, at: datetime) -> bool:
        """Advance the replay counter, atomically, and report whether this won.

        Store `counter` and `at` against `credential_id` **only when `counter`
        is strictly greater than the counter already stored**, and return True
        exactly when the store did. Anything else returns False: an equal or
        lower counter, and a credential that is not there.

        The return value is the whole point — see the class docstring for why a
        read-then-write cannot be substituted for a conditional one.
        """
        ...


@runtime_checkable
class BulkSecondFactorStore(SecondFactorStore, Protocol):
    """A second-factor store that can persist one credential set together."""

    async def add_many(self, credentials: Sequence[SecondFactor]) -> tuple[SecondFactor, ...]:
        """Store every credential and return them in input order."""
        ...


@runtime_checkable
class BulkSecondFactorRemovalStore(SecondFactorStore, Protocol):
    """A second-factor store that can remove one credential set together."""

    async def remove_many(self, user_id: str, credential_ids: Sequence[str]) -> None:
        """Delete the named credentials only when they belong to `user_id`."""
        ...


@runtime_checkable
class DiscoverableSecondFactorStore(SecondFactorStore, Protocol):
    """The additional indexed lookup required by first-factor passkeys.

    Kept separate from `SecondFactorStore` so existing TOTP and second-factor
    WebAuthn stores remain valid. `second_factor_router(passkey_login=True)`
    refuses at construction unless this extension is implemented.
    """

    async def credential(self, credential_id: str) -> SecondFactor | None:
        """Look up one credential by its framework id, or return `None`.

        Discoverable login derives this opaque id from the public WebAuthn
        credential id with the same one-way function enrolment uses. The lookup
        must be indexed; enumerating every user's credentials makes login
        proportional to the deployment's account count.
        """
        ...


@dataclass(slots=True)
class InMemorySecondFactorStore:
    """A dict-backed `SecondFactorStore` for development and tests.

    Per-process and unshared, exactly like `InMemoryUserStore`: two workers do
    not see each other's credentials, and *the replay counter is per process
    too*, so this is not a store to run a real second factor on. Use
    `wreath.users.OrmSecondFactorStore`, or your own, in production.
    """

    _rows: dict[str, SecondFactor] = field(default_factory=dict)
    _by_user: dict[str, dict[str, SecondFactor]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for credential in self._rows.values():
            self._by_user.setdefault(credential.user_id, {})[credential.id] = credential

    async def credentials(self, user_id: str) -> list[SecondFactor]:
        """Every credential belonging to `user_id`, in insertion order."""
        rows = self._by_user.get(user_id)
        return [] if rows is None else list(rows.values())

    def _add(self, credential: SecondFactor) -> SecondFactor:
        if credential.id in self._rows:
            raise ValueError(f"duplicate second-factor id: {credential.id!r}")
        rows = self._by_user.setdefault(credential.user_id, {})
        rows[credential.id] = credential
        try:
            self._rows[credential.id] = credential
        except MemoryError:
            del rows[credential.id]
            if not rows:
                del self._by_user[credential.user_id]
            raise
        return credential

    async def add(self, credential: SecondFactor) -> SecondFactor:
        """Store `credential`.

        Raises:
            ValueError: the id is already taken. Silently overwriting would drop
                a live credential -- and, for TOTP, its replay counter with it.
        """
        return self._add(credential)

    async def add_many(self, credentials: Sequence[SecondFactor]) -> tuple[SecondFactor, ...]:
        """Store one credential set without suspending between rows."""
        batch = tuple(credentials)
        seen: set[str] = set()
        for credential in batch:
            if credential.id in seen or credential.id in self._rows:
                raise ValueError(f"duplicate second-factor id: {credential.id!r}")
            seen.add(credential.id)
        for credential in batch:
            self._add(credential)
        return batch

    async def credential(self, credential_id: str) -> SecondFactor | None:
        """Return one credential by id, or `None`."""
        return self._rows.get(credential_id)

    def _remove(self, user_id: str, credential_id: str) -> None:
        row = self._rows.get(credential_id)
        if row is not None and row.user_id == user_id:
            del self._rows[credential_id]
            rows = self._by_user[user_id]
            del rows[credential_id]
            if not rows:
                del self._by_user[user_id]

    async def remove(self, user_id: str, credential_id: str) -> None:
        """Delete one credential, but only if it belongs to `user_id`.

        A miss is not an error; the outcome asked for is that the credential is
        gone. The ownership check is not decoration -- the id is the only thing
        a caller passes, so without it any user could delete any factor.
        """
        self._remove(user_id, credential_id)

    async def remove_many(self, user_id: str, credential_ids: Sequence[str]) -> None:
        """Delete one owned credential set."""
        for credential_id in dict.fromkeys(credential_ids):
            await self.remove(user_id, credential_id)

    def _advance(self, credential_id: str, counter: int, at: datetime) -> bool:
        """Compare and set, in a **synchronous** function. Returns whether it won.

        The atomicity is structural rather than conventional, and that is the
        point of this being a `def` and not an `async def`: a synchronous
        function cannot suspend, so the event loop cannot run another task
        between the read of `row.counter` and the write that replaces the row.
        `touch` below is a one-line `async` wrapper over it precisely so that
        nobody can later add an `await` *inside* the compare-and-set -- there is
        nowhere in here to put one. Written as an `async def` with the same body,
        this store would be correct only by the accident of its own statements
        not happening to suspend today.
        """
        row = self._rows.get(credential_id)
        if row is None or counter <= row.counter:
            return False
        updated = replace(row, counter=counter, last_used_at=at)
        self._rows[credential_id] = updated
        self._by_user[row.user_id][credential_id] = updated
        return True

    async def touch(self, credential_id: str, *, counter: int, at: datetime) -> bool:
        """Record that `credential_id` was just used at step `counter`.

        Returns:
            True when the counter advanced. False when it did not -- a counter
            at or below the stored one, or a credential that is no longer there.
            A caller verifying a code must read False as a replay: either the
            step was already spent, or another request spent it while this one
            was checking the code. It is not an error, and it is deliberately
            not an exception: a losing racer is an ordinary refusal, and two
            requests arriving with the same code is exactly what this is for.
        """
        return self._advance(credential_id, counter, at)


@dataclass(frozen=True, slots=True)
class TotpEnrolment:
    """A minted TOTP secret that is **not enrolled yet**.

    `begin_totp_enrolment` returns one of these and writes nothing anywhere.
    Show `uri` as a QR code and `secret_base32` for hand entry, keep the secret
    somewhere it survives one round trip, and pass it to
    `confirm_totp_enrolment` along with a code the user read off their phone.
    Only that call creates a credential. Enrolling before a code has verified is
    the most common bug in hand-rolled MFA and it locks people out of their own
    accounts.

    Neither `secret` nor `uri` appears in the `repr`; the URI embeds the secret.
    """

    secret: bytes = field(repr=False)
    uri: str = field(repr=False)
    label: str
    digits: int = DEFAULT_DIGITS
    period: int = DEFAULT_PERIOD

    @property
    def secret_base32(self) -> str:
        """The secret as unpadded base32, for an app that cannot scan a code."""
        return secret_to_base32(self.secret)


def new_credential_id() -> str:
    """A random opaque credential id (128 bits, hex)."""
    return secrets.token_hex(16)


def discoverable_credential_id(credential_id: bytes) -> str:
    """The opaque, indexed row id for a public WebAuthn credential handle.

    A factor's framework id appears in the factor-management API. Keeping the
    raw handle there needlessly exposes a stable authenticator identifier, while
    a random id cannot be derived during username-less login. A domain-separated
    SHA-256 digest provides both properties and remains an ordinary primary-key
    lookup for every discoverable store.
    """
    if not credential_id:
        raise ValueError("a discoverable credential needs a non-empty id")
    return hashlib.sha256(b"wreath-webauthn-row\x00" + credential_id).hexdigest()


def begin_totp_enrolment(
    *,
    account: str,
    issuer: str = "",
    label: str = "Authenticator app",
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    secret: bytes | None = None,
) -> TotpEnrolment:
    """Phase one: mint a secret and describe it. **Stores nothing.**

    Args:
        account: the name shown in the authenticator, usually the user's email.
        issuer: the application's name, shown beside it.
        secret: supply one only in tests; the default is fresh randomness.

    Raises:
        ValueError: an empty account, a secret below `MIN_SECRET_BYTES`, or a
            digit count or period the code generator will not accept.
    """
    material = generate_totp_secret() if secret is None else bytes(secret)
    uri = totp_uri(material, account=account, issuer=issuer, digits=digits, period=period)
    # Generate one code now, so a parameter the verifier would later reject --
    # a period of zero, eleven digits -- fails here, before a user has been
    # shown a QR code that could never have worked.
    totp_code(material, totp_counter(period=period), digits=digits)
    return TotpEnrolment(secret=material, uri=uri, label=label, digits=digits, period=period)


async def confirm_totp_enrolment(
    store: SecondFactorStore,
    user_id: str,
    *,
    secret: bytes,
    code: str,
    label: str = "Authenticator app",
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    skew: int = DEFAULT_SKEW,
    at: float | None = None,
    recovery_codes: int = DEFAULT_RECOVERY_CODES,
) -> tuple[SecondFactor, list[str]] | None:
    """Phase two: verify one code against `secret`, and only then enrol.

    On success the credential is stored with its counter already set to the step
    that just verified, so the code used to confirm the enrolment cannot also be
    used to sign in -- the first thing a replay would try. Recovery codes are
    minted, hashed with `hash_recovery_code`, and stored one credential per
    code; the plaintext is returned here and nowhere else, so a caller that does
    not show it has lost it.

    Returns:
        `(credential, recovery_codes)` on success, or None when the code did not
        verify -- and on None **nothing has been written**.

    Raises:
        ValueError: `user_id` already has a TOTP factor (enrol once, or remove
            the old one first), or a parameter `verify_totp` refuses.
    """
    existing = await store.credentials(user_id)
    if any(row.kind == "totp" for row in existing):
        raise ValueError("this user already has a TOTP factor enrolled")
    counter = verify_totp(secret, code, at=at, period=period, digits=digits, skew=skew)
    if counter is None:
        return None
    now = datetime.now(UTC) if at is None else datetime.fromtimestamp(at, UTC)
    credential = SecondFactor(
        id=new_credential_id(),
        user_id=user_id,
        kind="totp",
        label=label,
        created_at=now,
        last_used_at=now,
        material=bytes(secret),
        counter=counter,
    )
    recoveries, plaintext = _new_recovery_codes(user_id, recovery_codes, now)
    credentials = (credential, *recoveries)
    await _store_credentials(store, credentials)
    return credential, plaintext


async def _store_credentials(store: SecondFactorStore, credentials: Sequence[SecondFactor]) -> None:
    if isinstance(store, BulkSecondFactorStore):
        await store.add_many(credentials)
    else:
        for item in credentials:
            await store.add(item)


def _new_recovery_codes(
    user_id: str, count: int, now: datetime
) -> tuple[tuple[SecondFactor, ...], list[str]]:
    if count < 1:
        return (), []
    plaintext = generate_recovery_codes(count)
    credentials = tuple(
        SecondFactor(
            id=new_credential_id(),
            user_id=user_id,
            kind="recovery",
            label="Recovery code",
            created_at=now,
            last_used_at=None,
            material=hash_recovery_code(code_text).encode("utf-8"),
            counter=0,
        )
        for code_text in plaintext
    )
    return credentials, plaintext


async def verify_second_factor(
    store: SecondFactorStore,
    user_id: str,
    code: str,
    *,
    at: float | None = None,
    period: int = DEFAULT_PERIOD,
    digits: int = DEFAULT_DIGITS,
    skew: int = DEFAULT_SKEW,
) -> SecondFactor | None:
    """Redeem `code` against any of `user_id`'s factors. Returns the one that matched.

    TOTP factors are tried first and each is checked against its own stored
    counter, so a replayed code fails even though it is arithmetically valid;
    a match is written back through `touch` before this returns, which is what
    closes the window on the second attempt.

    **A code that verified but did not advance the counter is a replay too.**
    The read, the HMAC and the write are separated by real awaits, so two
    requests carrying the same code both see the same stored counter and both
    reach `touch`; only one of them moves it. The loser is refused here, on the
    same line as a counter that was already spent, rather than being reported as
    a success nobody recorded -- which is the difference between a second factor
    that is single-use and one that is single-use unless you send it twice at
    once.

    Recovery codes are tried next, compared constant-time by
    `verify_recovery_code`, and the credential is **deleted** on a match: single
    use is the whole point of a recovery code, and a deletion is a stronger
    statement of it than a used flag. The deletion is claimed through the same
    conditional advance the TOTP branch uses -- a recovery row is minted at
    counter zero and exactly one caller can move it to one -- because a
    `remove` that returns nothing cannot say which of two simultaneous
    redemptions actually spent the code, and both were reported as a match.

    Nothing here is throttled. `wreath.users.second_factor_router` wraps this
    with a `LoginLimiter`, and a caller reaching for this directly owns that
    decision -- six digits is a million guesses, which is minutes of unthrottled
    requests.

    Returns:
        The credential that matched (a TOTP one carries its new counter), or
        None. Nothing distinguishes "no factors" from "wrong code" here.
    """
    rows = await store.credentials(user_id)
    now = datetime.now(UTC) if at is None else datetime.fromtimestamp(at, UTC)
    for row in rows:
        if row.kind != "totp":
            continue
        counter = verify_totp(
            row.material,
            code,
            at=at,
            period=period,
            digits=digits,
            skew=skew,
            last_counter=row.counter,
        )
        if counter is not None and await store.touch(row.id, counter=counter, at=now):
            return replace(row, counter=counter, last_used_at=now)
    candidate = _normalize_recovery_code(code)
    if not candidate:
        return None
    candidate_digest: str | None = None
    for row in rows:
        if row.kind != "recovery":
            continue
        stored = row.material.decode("utf-8")
        if _is_legacy_recovery_hash(stored):
            # A stage-one scrypt row. ~40ms and 16 MB, which is a stall rather
            # than a delay on an event loop, so it goes to a thread; the
            # SHA-256 rows below are a microsecond and stay inline.
            matched = await asyncio.to_thread(verify_recovery_code, candidate, stored)
        else:
            if candidate_digest is None:
                candidate_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            matched = hmac.compare_digest(
                candidate_digest,
                stored[len(_RECOVERY_SCHEME) :],
            )
        if matched:
            # Single use is decided where the write happens, exactly as it is
            # for a TOTP counter -- and for exactly the same reason. The read,
            # the hash and the delete are separated by real awaits, so two
            # requests carrying the same recovery code both see the row and both
            # reach `remove`; a delete that cannot say whether it deleted cannot
            # tell them apart, and both were reported as a match. A recovery
            # credential is minted with `counter=0`, so the store's one atomic
            # conditional advance settles it: exactly one caller moves it to 1,
            # and the loser is refused on the same line as a spent TOTP step.
            if not await store.touch(row.id, counter=1, at=now):
                return None
            await store.remove(user_id, row.id)
            return replace(row, last_used_at=now)
    return None


async def remove_second_factor(
    store: SecondFactorStore, user_id: str, credential_id: str
) -> SecondFactor | None:
    """Un-enrol one of `user_id`'s factors, taking the recovery codes with the last one.

    Two rules live here rather than in the router, because both are properties
    of the credential set rather than of HTTP:

    * **A recovery credential cannot be deleted by id.** They are not listed by
      `wreath.users.second_factor_router`'s `GET`, they carry no label worth
      choosing between, and deleting them one at a time only ever moves a user
      closer to being locked out. This answers as it does for an id that does
      not exist.
    * **Removing the last real factor removes the recovery codes with it.**
      Otherwise the user is told their authenticator is gone while login still
      demands a code, and the only codes left are the ones on a piece of paper
      they threw away when they turned the feature off. Recovery codes are a
      fallback *for* a factor, so they do not outlive the last one.

    Ownership is the store's guarantee (`SecondFactorStore.remove` is scoped to
    `user_id`), and this reads the user's own credentials first, so an id
    belonging to somebody else is simply not found.

    Returns:
        The credential that was removed, or None when the id names nothing this
        user may remove -- one answer for "no such id", "not yours", and "that
        is a recovery code", so this cannot be used to probe which ids exist.
    """
    rows = await store.credentials(user_id)
    target = next((row for row in rows if row.id == credential_id), None)
    if target is None or target.kind == "recovery":
        return None
    removing = [credential_id]
    survivors = [row for row in rows if row.id != credential_id and row.kind != "recovery"]
    if not survivors:
        removing = [row.id for row in rows]
    if len(removing) > 1 and isinstance(store, BulkSecondFactorRemovalStore):
        await store.remove_many(user_id, removing)
    else:
        for item in removing:
            await store.remove(user_id, item)
    return target


#: Bytes of challenge. WebAuthn requires at least 16 and recommends more; 32 is
#: what every relying party uses and matches the SHA-256 the client hashes into.
WEBAUTHN_CHALLENGE_BYTES = 32

#: How long the browser is told to wait, in milliseconds. It is a hint to the
#: client, not the security bound -- `wreath.users.second_factor_router` expires
#: the challenge server-side, which is the half that matters.
DEFAULT_WEBAUTHN_TIMEOUT_MS = 120_000

#: WebAuthn's own limit on the opaque user handle in the credential.
MAX_USER_HANDLE_BYTES = 64

#: What `authenticatorSelection.userVerification` may say. Second-factor use
#: asks for `preferred`: the authenticator verifies the user when it can, and a
#: security key with no PIN still works. `required` is the discoverable-login
#: setting used by stage four.
UserVerification = Literal["required", "preferred", "discouraged"]
_USER_VERIFICATION = frozenset(("required", "preferred", "discouraged"))


@dataclass(frozen=True, slots=True)
class WebAuthnCeremony:
    """A begun WebAuthn ceremony: what to remember, and what to send.

    `options` is the `PublicKeyCredentialCreationOptions` or
    `PublicKeyCredentialRequestOptions` dictionary, with every byte string
    already base64url-encoded, so it is JSON as it stands. `challenge` is the
    raw bytes to keep server-side and hand back to the confirming call.

    Neither member is in the `repr`. A challenge is not a secret -- it is sent
    to the browser -- but it is the thing that makes a ceremony single-use, and
    the plan asks for challenges to stay out of logs rather than to be argued
    about.
    """

    ceremony: Literal["register", "authenticate"]
    challenge: bytes = field(repr=False)
    options: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class WebAuthnAssertion:
    """A verified assertion: which credential answered, and how.

    `user_verified` is the outcome of *this* ceremony's user verification -- a
    PIN or a fingerprint rather than a bare touch. It is reported rather than
    enforced, because whether a bare touch is enough is a policy question;
    `verify_webauthn_assertion(require_user_verification=True)` is how a caller
    that has decided turns it into a refusal.

    `counter` is the signature counter this assertion carried, already written
    back to the store.
    """

    credential: SecondFactor
    user_verified: bool
    counter: int


def _webauthn_origins(origins: Sequence[str]) -> tuple[str, ...]:
    accepted = tuple(origins)
    if not accepted or any(not origin for origin in accepted):
        raise ValueError("at least one non-empty origin is required")
    return accepted


def _webauthn_rp_id(rp_id: str) -> str:
    if not rp_id:
        raise ValueError("a WebAuthn ceremony needs an RP ID")
    return rp_id


def _webauthn_challenge(challenge: bytes | None) -> bytes:
    if challenge is None:
        return secrets.token_bytes(WEBAUTHN_CHALLENGE_BYTES)
    minted = bytes(challenge)
    if len(minted) < 16:
        raise ValueError("a WebAuthn challenge must be at least 16 bytes")
    return minted


def _descriptors(credentials: Sequence[SecondFactor]) -> list[dict[str, Any]]:
    """`PublicKeyCredentialDescriptor`s for the caller's webauthn credentials.

    A credential whose material does not decode is skipped rather than raised
    on: it names a key this build cannot use, and one unreadable row should not
    stop the user signing in with the authenticator that still works.
    """
    out = []
    for row in credentials:
        if row.kind != "webauthn":
            continue
        try:
            stored = unpack_credential(row.material)
        except WebAuthnError:
            continue
        out.append({"type": "public-key", "id": b64url_encode(stored.credential_id)})
    return out


def begin_webauthn_registration(
    *,
    user_id: str,
    account: str,
    rp_id: str,
    rp_name: str = "",
    display_name: str = "",
    existing: Sequence[SecondFactor] = (),
    timeout_ms: int = DEFAULT_WEBAUTHN_TIMEOUT_MS,
    user_verification: str = "preferred",
    discoverable: bool = False,
    challenge: bytes | None = None,
) -> WebAuthnCeremony:
    """Phase one of registration: mint a challenge and describe what to create.

    **Stores nothing**, exactly as `begin_totp_enrolment` stores nothing. The
    returned challenge is the caller's to hold somewhere server-side, bound to
    the user who began the ceremony, until `confirm_webauthn_registration`.

    `attestation` is `none` in the options, and that is a decision rather than a
    default: verifying an attestation statement means a metadata service and a
    network dependency, and the plan rules it out. A client asked for `none`
    replaces whatever the authenticator produced with a none statement, which is
    why `parse_attestation_object` can insist on seeing one.

    `existing` populates `excludeCredentials`, so an authenticator the user has
    already registered declines rather than silently creating a second
    credential nobody asked for.

    Args:
        user_id: the application's own id, sent as the opaque user handle.
        account: the `name` shown in the authenticator's account chooser.
        rp_id: the registrable domain the credential is scoped to. A credential
            is bound to it forever, so it is a decision, not a setting -- use
            the site's registrable domain (`example.com`), not a subdomain you
            might move off.
        discoverable: require a resident credential that can identify its
            account during a usernameless first-factor assertion.
        challenge: supply one only in tests; the default is fresh randomness.

    Raises:
        ValueError: an empty RP ID or account, a user handle over
            `MAX_USER_HANDLE_BYTES`, or an unknown `user_verification`.
    """
    rp_id = _webauthn_rp_id(rp_id)
    if not account:
        raise ValueError("a WebAuthn registration needs an account name")
    if user_verification not in _USER_VERIFICATION:
        raise ValueError(f"unknown user verification: {user_verification!r}")
    handle = user_id.encode("utf-8")
    if not handle or len(handle) > MAX_USER_HANDLE_BYTES:
        raise ValueError(f"a WebAuthn user handle must be 1..{MAX_USER_HANDLE_BYTES} bytes")
    minted = _webauthn_challenge(challenge)
    options: dict[str, Any] = {
        "rp": {"id": rp_id, "name": rp_name or rp_id},
        "user": {
            "id": b64url_encode(handle),
            "name": account,
            "displayName": display_name or account,
        },
        "challenge": b64url_encode(minted),
        # ES256 first, Ed25519 second: the order is the relying party's
        # preference, and the two are the only algorithms wreath verifies.
        "pubKeyCredParams": [
            {"type": "public-key", "alg": -7},
            {"type": "public-key", "alg": -8},
        ],
        "timeout": int(timeout_ms),
        "attestation": "none",
        "authenticatorSelection": {
            "userVerification": user_verification,
            "residentKey": "required" if discoverable else "discouraged",
            "requireResidentKey": discoverable,
        },
        "excludeCredentials": _descriptors(existing),
    }
    return WebAuthnCeremony(ceremony="register", challenge=minted, options=options)


def begin_webauthn_assertion(
    credentials: Sequence[SecondFactor],
    *,
    rp_id: str,
    timeout_ms: int = DEFAULT_WEBAUTHN_TIMEOUT_MS,
    user_verification: str = "preferred",
    challenge: bytes | None = None,
) -> WebAuthnCeremony:
    """Phase one of an assertion: mint a challenge and list what may answer it.

    `allowCredentials` names the caller's own registered credentials for a
    second-factor assertion. Pass an empty sequence for discoverable login: the
    authenticator then chooses a resident credential and returns its public id.

    Raises:
        ValueError: an empty RP ID or an unknown `user_verification`.
    """
    rp_id = _webauthn_rp_id(rp_id)
    if user_verification not in _USER_VERIFICATION:
        raise ValueError(f"unknown user verification: {user_verification!r}")
    minted = _webauthn_challenge(challenge)
    options: dict[str, Any] = {
        "challenge": b64url_encode(minted),
        "rpId": rp_id,
        "timeout": int(timeout_ms),
        "userVerification": user_verification,
        "allowCredentials": _descriptors(credentials),
    }
    return WebAuthnCeremony(ceremony="authenticate", challenge=minted, options=options)


async def confirm_webauthn_registration(
    store: SecondFactorStore,
    user_id: str,
    *,
    challenge: bytes,
    client_data: bytes,
    attestation_object: bytes,
    rp_id: str,
    origins: Sequence[str],
    label: str = "Security key",
    require_user_verification: bool = False,
    at: float | None = None,
    recovery_codes: int = DEFAULT_RECOVERY_CODES,
) -> tuple[SecondFactor, list[str]]:
    """Phase two: verify the attestation, and only then store the public key.

    Every check the ceremony exists for happens before anything is written --
    the client data's `type`, `challenge` and `origin`, the authenticator data's
    RP ID hash, the user-present flag, and the COSE algorithm -- and each one is
    its own refusal with its own message.

    **Recovery codes are issued when the user has none.** A user whose only
    factor is a security key and who then loses the key is the lockout the plan
    names as the most likely real failure, and issuing codes at enrolment is the
    stated mitigation. A user who already has codes from a TOTP enrolment keeps
    those and gets an empty list back, because minting a second set would
    invalidate neither and leave two pieces of paper in circulation.

    Returns:
        `(credential, recovery_codes)`. The codes are the plaintext, they exist
        nowhere else, and the list is empty when the user already had some.

    Raises:
        WebAuthnError: any part of the ceremony that did not verify.
        ValueError: an empty RP ID or origin list, or a credential this user has
            already registered.
    """
    rp_id = _webauthn_rp_id(rp_id)
    accepted = _webauthn_origins(origins)
    check_client_data(
        client_data,
        expected_type="webauthn.create",
        challenge=challenge,
        origins=accepted,
    )
    auth_data = parse_attestation_object(attestation_object)
    check_rp_id_hash(auth_data, rp_id)
    if not auth_data.user_present:
        raise WebAuthnError("the authenticator reported no user presence")
    if require_user_verification and not auth_data.user_verified:
        raise WebAuthnError("this registration requires user verification")
    # The COSE key is *not* re-parsed here. `parse_authenticator_data` parses it
    # to find where it ends, and refuses an unusable algorithm by name while it
    # does -- so a second `parse_cose_key(auth_data.public_key)` in this function
    # could never fail, and a check that cannot fail reports a safety it is not
    # providing (a check that has nothing to check). One existed here and was removed.
    rows = await store.credentials(user_id)
    for row in rows:
        if row.kind != "webauthn":
            continue
        try:
            stored = unpack_credential(row.material)
        except WebAuthnError:
            continue
        if hmac.compare_digest(stored.credential_id, auth_data.credential_id):
            raise WebAuthnError("this credential is already registered")
    now = datetime.now(UTC) if at is None else datetime.fromtimestamp(at, UTC)
    credential = SecondFactor(
        # The stable public authenticator handle is never rendered by the
        # factor-management API. Its domain-separated digest is still
        # derivable at username-less login and uses the store's primary-key
        # index instead of scanning accounts or unpacking Python objects.
        id=discoverable_credential_id(auth_data.credential_id),
        user_id=user_id,
        kind="webauthn",
        label=label,
        created_at=now,
        last_used_at=now,
        material=pack_credential(
            auth_data.credential_id,
            auth_data.public_key,
            user_verified=auth_data.user_verified,
        ),
        counter=auth_data.sign_count,
    )
    if not any(row.kind == "recovery" for row in rows):
        recoveries, issued = _new_recovery_codes(user_id, recovery_codes, now)
    else:
        recoveries, issued = (), []
    await _store_credentials(store, (credential, *recoveries))
    return credential, issued


async def verify_webauthn_assertion(
    store: SecondFactorStore,
    user_id: str,
    *,
    challenge: bytes,
    credential_id: bytes,
    client_data: bytes,
    authenticator_data: bytes,
    signature: bytes,
    rp_id: str,
    origins: Sequence[str],
    require_user_verification: bool = False,
    at: float | None = None,
) -> WebAuthnAssertion:
    """Verify one assertion against one of `user_id`'s registered credentials.

    The signature covers `authenticatorData || SHA-256(clientDataJSON)`, which
    is what binds the two halves together: neither can be swapped for a recorded
    one without breaking the other.

    **The signature counter is checked before the store is touched, and again
    by the store touching it.** A counter that has been reported non-zero and
    then does not increase is the cloned-authenticator signal, and it is
    refused; so is one that lost the store's conditional advance, because a
    concurrent assertion having moved the counter first says the same thing. A
    counter that is zero on both sides is an authenticator that does not
    implement the counter -- which is common, and legitimate, and must not read
    as a regression, so a zero that cannot advance is not one.

    Raises:
        WebAuthnError: an unknown credential id, any of the client-data or
            authenticator-data checks, a signature that does not verify, or a
            counter that did not move.
        ValueError: an empty RP ID or origin list.
    """
    rp_id = _webauthn_rp_id(rp_id)
    accepted = _webauthn_origins(origins)
    row, stored = await _webauthn_credential(store, user_id, credential_id)
    check_client_data(
        client_data,
        expected_type="webauthn.get",
        challenge=challenge,
        origins=accepted,
    )
    auth_data = parse_authenticator_data(authenticator_data)
    check_rp_id_hash(auth_data, rp_id)
    if not auth_data.user_present:
        raise WebAuthnError("the authenticator reported no user presence")
    if require_user_verification and not auth_data.user_verified:
        raise WebAuthnError("this assertion requires user verification")
    key = parse_cose_key(stored.public_key)
    signed = authenticator_data + hashlib.sha256(client_data).digest()
    if not verify_signature(key, signed, signature):
        raise WebAuthnError("the assertion signature did not verify")
    if row.counter and auth_data.sign_count <= row.counter:
        raise WebAuthnError(
            "the signature counter did not increase; this authenticator may be a clone"
        )
    now = datetime.now(UTC) if at is None else datetime.fromtimestamp(at, UTC)
    if not await store.touch(row.id, counter=auth_data.sign_count, at=now) and (
        auth_data.sign_count
    ):
        # The counter was checked against what this request read, and the store
        # has since refused to advance it: another assertion carrying the same
        # or a newer count got there first. Same conclusion as the check above,
        # reached the only way a race can be seen -- by losing it.
        # Guarded on a non-zero count because zero is "this authenticator does
        # not implement the counter", and an advance from zero to zero is not
        # something any store can win. Those credentials are protected by the
        # single-use challenge instead, which is where their replay defence has
        # always lived.
        raise WebAuthnError(
            "the signature counter did not increase; this authenticator may be a clone"
        )
    return WebAuthnAssertion(
        credential=replace(row, counter=auth_data.sign_count, last_used_at=now),
        user_verified=auth_data.user_verified,
        counter=auth_data.sign_count,
    )


async def _webauthn_credential(
    store: SecondFactorStore, user_id: str, credential_id: bytes
) -> tuple[SecondFactor, Any]:
    """The caller's credential with this id, or a refusal.

    Scoped to `user_id` rather than looked up globally: the id comes off the
    wire, and a credential belonging to somebody else must not answer for this
    account even when the signature over it is perfectly good.
    """
    if not credential_id:
        raise WebAuthnError("the assertion names no credential")
    for row in await store.credentials(user_id):
        if row.kind != "webauthn":
            continue
        try:
            stored = unpack_credential(row.material)
        except WebAuthnError:
            continue
        if hmac.compare_digest(stored.credential_id, credential_id):
            return row, stored
    raise WebAuthnError("no such credential for this user")


# A ceremony challenge is spent exactly once, by the user who began it. The
# property is *atomic* consumption, and it is why these live on
# `wreath.store`'s keyed table rather than in the session: a read followed by a
# delete lets two concurrent completions both conclude they were first, and a
# challenge carried in a cookie is not single-use at all -- a caller who kept
# an older copy of the cookie kept the challenge with it.
# The user binding is part of the consuming statement, never a check after it.
# Consuming first and comparing afterwards would let anyone holding a handle
# burn the rightful user's ceremony, which is a denial of service against
# somebody else's login rather than a defence of it.

#: Kinds a challenge may carry. The kind is part of the consuming condition, so
#: a registration challenge cannot be spent to answer an assertion.
#: Both WebAuthn ceremonies share one kind because they share one session key
#: and one namespace; which ceremony a challenge was minted for is a field in
#: the payload (`ceremony`), checked by the caller after the consume.
#: The two WebAuthn ceremonies are *different kinds*, not one kind with a
#: discriminator in the payload. `consume` matches on kind, so a registration
#: challenge answering an assertion is refused by the statement itself rather
#: than by a check after the row has already been spent.
CHALLENGE_WEBAUTHN_REGISTER: Final = "webauthn:register"
CHALLENGE_WEBAUTHN_ASSERT: Final = "webauthn:authenticate"
CHALLENGE_ENROLMENT: Final = "totp-enrolment"

#: The table wreath keeps its challenges in. Wreath owns its own furniture, so
#: this never belongs in a user's migration artifact -- it arrives through the
#: schema component and is applied at lifespan startup.
CHALLENGE_TABLE: Final = "wreath_second_factor_challenges"


def challenge_declaration(*, table: str = CHALLENGE_TABLE, prefix: str = "wreath_2fa") -> Any:
    """The `Keyed` declaration behind a challenge store.

    `ttl=None`: a challenge's lifetime is the caller's, because a WebAuthn
    ceremony and a TOTP enrolment are offerable for different lengths of time
    and one table holds both. `claim=False`: the generated claim is an
    insert-or-reclaim, and what a challenge needs is the consumption of a row
    that already exists.
    """
    from .store import Column, Keyed

    return Keyed(
        table=table,
        columns=(
            Column("user_id", "text", null=False),
            Column("kind", "text", null=False),
            Column("payload", "jsonb", null=False),
        ),
        key="handle",
        stamp="expires",
        deadline=True,
        ttl=None,
        index_stamp=True,
        claim=False,
        prefix=prefix,
    )


def _payload(value: Any) -> dict[str, Any]:
    """A `jsonb` column as a dict, whichever result format the driver used.

    The binary format hands back a decoded object; the text format hands back
    the JSON. Both reach this, so neither caller has to know which.
    """
    return json.loads(value) if isinstance(value, (str, bytes)) else value


@dataclass(frozen=True, slots=True)
class ChallengeRow:
    """A live challenge, read without being spent.

    `payload` is a copy: a caller editing what it read must not edit what is
    still stored, or a `peek` becomes a way to rewrite somebody's ceremony.
    """

    user_id: str
    kind: str
    payload: dict[str, Any]


@runtime_checkable
class ChallengeStore(Protocol):
    """Where a begun ceremony's challenge waits to be spent exactly once."""

    async def put(
        self, handle: str, *, user_id: str, kind: str, payload: dict[str, Any], ttl: float
    ) -> None:
        """Hold `payload` under `handle` for `ttl` seconds, bound to `user_id`."""
        ...

    async def peek(self, handle: str) -> ChallengeRow | None:
        """The live challenge under `handle`, **without spending it**.

        Two callers want this and neither of them may consume:

        * a ceremony whose property is "confirmed exactly once" rather than
          "read exactly once" -- a TOTP enrolment has to survive a mistyped
          code, or one wrong digit costs the user a fresh QR scan;
        * choosing an error code *after* a `consume` has already refused, to
          tell "there is no such challenge" apart from "it is not yours".

        It applies no binding. Reporting who a row belongs to is not deciding
        whether the caller may have it -- that is `consume`'s job, and keeping
        the two apart is what makes this safe to call on a refusal path. **No
        security decision may rest on the result.**

        None whenever there is nothing live to report: absent, expired, or
        already spent, on the same deadline `consume` uses.
        """
        ...

    async def consume(self, handle: str, *, user_id: str, kind: str) -> dict[str, Any] | None:
        """Spend the challenge and return its payload, or None.

        None covers every way it is not spendable -- absent, expired, already
        spent, another user's, another kind -- and the row is left untouched
        whenever the answer is None.
        """
        ...

    async def discard(self, handle: str) -> None:
        """Drop `handle`, spent or not. Not an error when it is already gone."""
        ...


class MemoryChallengeStore:
    """Challenges in one worker's memory: bounded, TTL'd, single-use.

    Being synchronous between the read and the delete is what makes `consume`
    atomic -- there is no await for another task on this loop to interleave at,
    which is the in-process counterpart of the single DELETE the PostgreSQL twin
    issues. Enough for a single-worker deployment; behind more than one worker a
    ceremony begun on one is not spendable on another, so pass a
    `PostgresChallengeStore` there.

    Bounded because a challenge store is reachable by anyone who can begin a
    ceremony, and an unbounded one is a way to spend a server's memory.
    """

    __slots__ = ("_cache",)

    def __init__(
        self, *, max_entries: int = 4096, clock: Callable[[], float] = time.monotonic
    ) -> None:
        from ._capability_map import CapabilityMap

        # `ttl=None`: the deadline is per-entry and kept in the value, because
        # one store holds ceremonies with different lifetimes. The cache's own
        # bound is what evicts whatever is never spent.
        self._cache = CapabilityMap(max_entries=max_entries, clock=clock)

    async def put(
        self, handle: str, *, user_id: str, kind: str, payload: dict[str, Any], ttl: float
    ) -> None:
        self._cache.put(handle, (user_id, kind, dict(payload)), ttl=float(ttl))

    async def peek(self, handle: str) -> ChallengeRow | None:
        entry = self._cache.peek(handle)
        if entry is None:
            return None
        held_user, held_kind, payload = entry
        # A copy, so editing what was read cannot edit what is still stored.
        return ChallengeRow(held_user, held_kind, dict(payload))

    async def consume(self, handle: str, *, user_id: str, kind: str) -> dict[str, Any] | None:
        entry = self._cache.consume(
            handle,
            predicate=lambda held: held[0] == user_id and held[1] == kind,
        )
        if entry is None:
            # Refused *without* consuming: the row belongs to whoever began the
            # ceremony, and an attempt on it must not cost them their challenge.
            return None
        _held_user, _held_kind, payload = entry
        return payload

    async def discard(self, handle: str) -> None:
        self._cache.discard(handle)


class PostgresChallengeStore:
    """Challenges in a table every worker shares.

    `consume` is one `DELETE ... WHERE handle AND user_id AND kind AND live
    RETURNING payload`. Exactly one concurrent statement can return the row, so
    **the returned row is the consumption** -- no owner column, no second round
    trip, and no window in which two completions both proceed. A mismatched user
    or kind matches no row, so it deletes nothing and the challenge survives for
    whoever began it.
    """

    __slots__ = ("_store",)

    def __init__(
        self, database: Any, *, table: str = CHALLENGE_TABLE, prefix: str = "wreath_2fa"
    ) -> None:
        from .store import PostgresStore

        declaration = challenge_declaration(table=table, prefix=prefix)
        store = PostgresStore(database, declaration)
        # Registered through `define` so these get the same lazy preparation and
        # the same statement naming as the generated ones. The store's own
        # `read`/`delete`/`purge` are generated and used as they stand.
        store.define(
            "put",
            f"INSERT INTO {table} (handle, user_id, kind, payload, expires)\n"
            f"VALUES ($1, $2, $3, $4::jsonb, {store.window('$5')})\n"
            "ON CONFLICT (handle) DO UPDATE SET\n"
            "    user_id = EXCLUDED.user_id,\n"
            "    kind = EXCLUDED.kind,\n"
            "    payload = EXCLUDED.payload,\n"
            "    expires = EXCLUDED.expires",
        )
        store.define(
            "consume",
            f"DELETE FROM {table}\n"
            "WHERE handle = $1 AND user_id = $2 AND kind = $3\n"
            "  AND expires > clock_timestamp()\n"
            "RETURNING payload",
        )
        # `clock_timestamp()` and not `now()`, and the same deadline `consume`
        # applies: a `peek` that reported a row `consume` has already decided is
        # gone would answer with the wrong error code, which is the one thing
        # this read exists to get right.
        store.define(
            "peek",
            f"SELECT user_id, kind, payload FROM {table}\n"
            "WHERE handle = $1 AND expires > clock_timestamp()",
            workload="read",
        )
        self._store = store

    @property
    def declaration(self) -> Any:
        """The `Keyed` this store was built from."""
        return self._store.declaration

    def component(self) -> Any:
        """This store's claim on the wreath schema."""
        return self._store.schema_claim("second_factor_challenges")

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined."""
        return self._store.schema_sql()

    async def put(
        self, handle: str, *, user_id: str, kind: str, payload: dict[str, Any], ttl: float
    ) -> None:
        await self._store.statement("put").execute(
            handle, user_id, kind, json.dumps(payload), float(ttl)
        )

    async def peek(self, handle: str) -> ChallengeRow | None:
        row = await self._store.statement("peek").fetchrow(handle)
        if row is None:
            return None
        return ChallengeRow(str(row[0]), str(row[1]), _payload(row[2]))

    async def consume(self, handle: str, *, user_id: str, kind: str) -> dict[str, Any] | None:
        row = await self._store.statement("consume").fetchrow(handle, user_id, kind)
        if row is None:
            return None
        return _payload(row[0])

    async def discard(self, handle: str) -> None:
        await self._store.delete(handle)

    async def purge_count(self) -> int | None:
        """Drop every expired challenge, reporting how many went."""
        return await self._store.purge_count()
