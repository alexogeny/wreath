"""The core of second factors: TOTP, recovery codes, WebAuthn, and their store.

Stages one to three of `docs/plans/second-factors-totp-webauthn.md`: TOTP,
hashed single-use recovery codes, WebAuthn registration and assertion, and the
credential store seam all three share. The wreath-coupled router glue lives in
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
import os
import secrets
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable
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
    "DEFAULT_DIGITS",
    "DEFAULT_PERIOD",
    "DEFAULT_SKEW",
    "MAX_SKEW",
    "InMemorySecondFactorStore",
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

# --- parameters -------------------------------------------------------------

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


# --- RFC 4226 / 6238 --------------------------------------------------------


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


# --- recovery codes ---------------------------------------------------------


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
        raw = "".join(
            secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_CODE_CHARS)
        )
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


# --- the credential record and its store ------------------------------------


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


@dataclass(slots=True)
class InMemorySecondFactorStore:
    """A dict-backed `SecondFactorStore` for development and tests.

    Per-process and unshared, exactly like `InMemoryUserStore`: two workers do
    not see each other's credentials, and *the replay counter is per process
    too*, so this is not a store to run a real second factor on. Use
    `wreath.users.OrmSecondFactorStore`, or your own, in production.
    """

    _rows: dict[str, SecondFactor] = field(default_factory=dict)

    async def credentials(self, user_id: str) -> list[SecondFactor]:
        """Every credential belonging to `user_id`, in insertion order."""
        return [row for row in self._rows.values() if row.user_id == user_id]

    async def add(self, credential: SecondFactor) -> SecondFactor:
        """Store `credential`.

        Raises:
            ValueError: the id is already taken. Silently overwriting would drop
                a live credential -- and, for TOTP, its replay counter with it.
        """
        if credential.id in self._rows:
            raise ValueError(f"duplicate second-factor id: {credential.id!r}")
        self._rows[credential.id] = credential
        return credential

    async def remove(self, user_id: str, credential_id: str) -> None:
        """Delete one credential, but only if it belongs to `user_id`.

        A miss is not an error; the outcome asked for is that the credential is
        gone. The ownership check is not decoration -- the id is the only thing
        a caller passes, so without it any user could delete any factor.
        """
        row = self._rows.get(credential_id)
        if row is not None and row.user_id == user_id:
            del self._rows[credential_id]

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
        self._rows[credential_id] = replace(row, counter=counter, last_used_at=at)
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


# --- enrolment and verification flows ---------------------------------------


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
    uri = totp_uri(
        material, account=account, issuer=issuer, digits=digits, period=period
    )
    # Generate one code now, so a parameter the verifier would later reject --
    # a period of zero, eleven digits -- fails here, before a user has been
    # shown a QR code that could never have worked.
    totp_code(material, totp_counter(period=period), digits=digits)
    return TotpEnrolment(
        secret=material, uri=uri, label=label, digits=digits, period=period
    )


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
    counter = verify_totp(
        secret, code, at=at, period=period, digits=digits, skew=skew
    )
    if counter is None:
        return None
    now = datetime.now(UTC) if at is None else datetime.fromtimestamp(at, UTC)
    credential = await store.add(
        SecondFactor(
            id=new_credential_id(),
            user_id=user_id,
            kind="totp",
            label=label,
            created_at=now,
            last_used_at=now,
            material=bytes(secret),
            counter=counter,
        )
    )
    plaintext = await _mint_recovery_codes(store, user_id, recovery_codes, now)
    return credential, plaintext


async def _mint_recovery_codes(
    store: SecondFactorStore, user_id: str, count: int, now: datetime
) -> list[str]:
    """Issue `count` recovery codes, store their hashes, return the plaintext.

    The plaintext exists only in the returned list. Nothing writes it anywhere,
    so a caller that does not show it to the user has thrown it away.
    """
    if count < 1:
        return []
    plaintext = generate_recovery_codes(count)
    for code_text in plaintext:
        hashed = hash_recovery_code(code_text)
        await store.add(
            SecondFactor(
                id=new_credential_id(),
                user_id=user_id,
                kind="recovery",
                label="Recovery code",
                created_at=now,
                last_used_at=None,
                material=hashed.encode("utf-8"),
                counter=0,
            )
        )
    return plaintext


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
            matched = verify_recovery_code(candidate, stored)
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
    await store.remove(user_id, credential_id)
    survivors = [
        row for row in rows if row.id != credential_id and row.kind != "recovery"
    ]
    if not survivors:
        for row in rows:
            if row.kind == "recovery":
                await store.remove(user_id, row.id)
    return target


# --- WebAuthn ---------------------------------------------------------------

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
#: security key with no PIN still works. `required` is the passwordless setting,
#: and belongs to stage four rather than here.
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
        out.append(
            {"type": "public-key", "id": b64url_encode(stored.credential_id)}
        )
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
        raise ValueError(
            f"a WebAuthn user handle must be 1..{MAX_USER_HANDLE_BYTES} bytes"
        )
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
            # A second factor is looked up by id, so it does not need to be
            # discoverable. Discoverable credentials are stage four.
            "residentKey": "discouraged",
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

    `allowCredentials` names the caller's own registered credentials, which is
    what makes this a *second* factor: the user has already been identified by
    the first one, so there is nothing to discover. Usernameless login, where
    the list is empty and the authenticator chooses, is stage four.

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
    # providing (docs/decisions/0024). One existed here and was removed.
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
    credential = await store.add(
        SecondFactor(
            id=new_credential_id(),
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
    )
    issued: list[str] = []
    if not any(row.kind == "recovery" for row in rows):
        issued = await _mint_recovery_codes(store, user_id, recovery_codes, now)
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
    if (row.counter or auth_data.sign_count) and auth_data.sign_count <= row.counter:
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
        #
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
