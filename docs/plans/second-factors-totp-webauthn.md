# Prescriptive plan: second factors — TOTP and WebAuthn passkeys

Status: **stages 1-4 implemented** (August 2026). TOTP enrolment and verification,
step-up (`second_factor_at`, `wreath.auth.second_factor(max_age=...)`,
`context.second_factor_age`), and WebAuthn as a *second* factor all ship — see
[Second factors](../guides/second-factors.md).

Stage 4 adds opt-in discoverable enrolment and usernameless first-factor login
through `second_factor_router(passkey_login=True)`. It requires resident
credentials and user verification, resolves the returned public credential id
with one indexed store lookup, and uses the same single-use `ChallengeStore` as
second-factor assertions. The permission manifest still does not model
freshness, so a route behind `@second_factor` can read as permitted and then
answer 403.

Related material:

- `AGENTS.md`
- `docs/agents/manifest.json` (subsystems `users`, `auth`)
- `docs/plans/middleware-auth-rbac-cedar-comforts.md`
- `docs/plans/aggressive-red-team-remediation.md`
- `docs/guides/users.md`, `docs/guides/auth.md`
- `~/research/pypi-downloads/wreath-gap-analysis.md`

## Goal

Add second factors to `wreath.users`: TOTP enrolment and verification, hashed
recovery codes, WebAuthn/passkey registration and assertion, and a step-up
challenge that authorization policy can require for sensitive actions — all
without a runtime dependency.

## Why this, and why now

The download evidence is real but modest: `pyotp` 41.3M installs a year,
`django-otp` 4.4M, `webauthn` 6.2M, `fido2` 2.2M — ~54M combined, against ~950M
for MCP. This is the smallest of the three items it was ranked with, and it earns
its place on **completeness rather than volume**.

Wreath ships a user kit (`users.py`: `user_router`, password hashing, email
verification, reset), OAuth2 auth-code + PKCE login, OIDC, session identity, and
a Cedar authorizer. The only match for `totp` in `src/wreath` is the redaction
regex at `crud.py:194` — Wreath redacts a field it cannot issue. A framework that
ships user management and a policy engine and cannot enrol a passkey looks
unfinished in precisely the place where a reviewer decides whether it takes
security seriously.

The second reason is that **it is cheaper here than anywhere else**, and for a
reason no competitor can copy — see the next section.

## The C/Python split, decided

**All Python. No new C, and no C in any later stage either.**

This is not a shortcut; it is the rule this repository already follows for
cryptography, applied consistently:

- `docs/plans/omni-directional-webhooks.md` states the constraint in its own
  terms: *"Use stdlib `hmac` and `hashlib` for the first signature profile. Do
  not implement cryptographic algorithms in C."*
- `_native/jose.c` follows it. It accelerates base64url decoding, JWS parsing,
  HMAC verification, and registered-claim checks — the parsing and framing — while
  the elliptic-curve point arithmetic stays in pure Python in `_auth/_ecverify.py`,
  which says so in its own header: *"not written for side-channel resistance;
  that is irrelevant for verifying a [public] signature."*

Applied here:

- **TOTP is HMAC-SHA1 over a big-endian counter** (RFC 6238/4226). That is
  `hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1)` and a modulo. The
  stdlib call is already C. There is nothing to accelerate, and `hmac.compare_digest`
  is the constant-time comparison the verification step needs.
- **WebAuthn assertion verification is one ES256 or Ed25519 signature check per
  login**, and `_auth/_ecverify.py` already implements both (`verify_es256` over
  NIST P-256, `verify_ed25519` over edwards25519), dependency-free. Reuse them
  verbatim. One point multiplication per login is not a hot path by any
  measurement this repository would accept.
- **CBOR decoding of the attestation object** is the only genuine C candidate —
  `_native/msgpack.c` is structurally the same kind of parser. It runs at most
  twice per registration and once per login, on a payload of a few hundred bytes.
  Adding it would mean a second implementation in `_pure/` plus a parity test, to
  save time nobody can measure. **Do not.** If a measurement ever contradicts
  this, `codecs.c` is where it would go and the measurement goes in the commit.

This split is also the competitive point worth stating in the docs: `webauthn`
pulls `cryptography`; `pyotp` is a dependency people add. Wreath ships both
factors inside the zero-dependency rule because the verify-only primitives were
already written for JWT.

## Non-goals

- **No attestation verification against a metadata service.** Wreath accepts
  `none` attestation, which is what consumer passkey flows use and what every
  major relying party does in practice. Enterprise attestation with an MDS blob
  is a different product and would need a network dependency.
- **No SMS or email OTP.** SMS second factors are worse than no second factor for
  the threat they claim to address, and email OTP is a password reset wearing a
  hat. `users.py` already sends verification mail; that is a different mechanism
  with different guarantees and should not be conflated with a second factor.
- **Not an identity provider.** Wreath authenticates its own users; it does not
  issue assertions to third parties.
- **No push-approval factor.** Needs a mobile SDK.

## Public model

Second-factor state does not belong on `UserRecord`. A user has zero or many
credentials, and `UserStore`'s four methods (`get_by_email`, `get_by_id`,
`create`, `update`) are the wrong shape for a collection. Add a sibling protocol
so `InMemoryUserStore` and `OrmUserStore` both implement it and neither grows a
list field:

```python
class SecondFactorStore(Protocol):
    async def credentials(self, user_id: str) -> list[SecondFactor]: ...
    async def add(self, credential: SecondFactor) -> SecondFactor: ...
    async def remove(self, user_id: str, credential_id: str) -> None: ...
    async def touch(self, credential_id: str, *, counter: int, at: datetime) -> None: ...
```

```python
@dataclass(frozen=True, slots=True)
class SecondFactor:
    id: str
    user_id: str
    kind: Literal["totp", "webauthn", "recovery"]
    label: str
    created_at: datetime
    last_used_at: datetime | None
    # kind-specific, opaque to callers: the TOTP secret, the COSE public key,
    # or the recovery-code hash. Never rendered by `me`, never logged.
    material: bytes
    counter: int = 0
```

New names from `wreath.users`: `SecondFactor`, `SecondFactorStore`,
`OrmSecondFactorStore`, `InMemorySecondFactorStore`, `TotpEnrolment`,
`totp_uri`, `generate_recovery_codes`, and `second_factor_router`.

`default_user_model` grows a companion `default_second_factor_model` rather than
new columns, so an existing deployment adds a table instead of altering a hot
one.

### Routes

`second_factor_router` mounts alongside `user_router` and is opt-in — an
application that does not want MFA gets no new routes and no new table:

| Route | Purpose |
|---|---|
| `POST /auth/2fa/totp/begin` | mint a secret, return the `otpauth://` URI; **not yet enrolled** |
| `POST /auth/2fa/totp/confirm` | verify one code, then enrol; returns recovery codes once |
| `POST /auth/2fa/webauthn/begin` | registration options + challenge in the session |
| `POST /auth/2fa/webauthn/confirm` | verify the attestation, store the public key |
| `GET /auth/2fa` | list enrolled factors (labels and dates; never material) |
| `DELETE /auth/2fa/{id}` | remove a factor — requires a fresh second factor |
| `POST /auth/2fa/verify` | satisfy a pending challenge during login or step-up |

`begin`/`confirm` is two-phase on purpose: enrolling a TOTP secret the user never
successfully entered locks people out, and it is the single most common bug in
homegrown MFA.

## Login and step-up

`user_router`'s `login` currently authenticates and writes a session. It grows a
third outcome: **authenticated but incomplete**. When the user has enrolled
factors, `login` writes a *pending* session marked with what it still needs and
returns a challenge; the identity backend must not treat a pending session as an
identity.

Step-up is the same machinery pointed at a different moment. A session records
`second_factor_at`; an `AuthRequirement` can demand one within a window, so a
Cedar policy can require a recent second factor for a destructive action without
the application threading a flag through every handler. This is what makes MFA
worth having in a framework that already owns authorization, rather than a
bolt-on: `declared_actions` and the permission document already enumerate
sensitive actions, so the policy has somewhere to attach.

Rotate the session on every factor transition. `policy/sessions.py` already
exports `rotate_session` and `_auth/oauth2.py` already calls it after login;
promotion from pending to full is exactly the same fixation risk.

## Security requirements, stated as requirements

These are the parts that are wrong in most implementations, so they are listed
where they cannot be skimmed past:

- **TOTP replay.** A code that verified once must not verify again inside its
  window. Store the last accepted counter per credential and require strictly
  greater. Without this the second factor is replayable from a shoulder-surf or a
  proxy for thirty seconds, which is the whole attack.
- **Skew** is ±1 step, configurable, never more by default.
- **Rate limiting.** `LoginLimiter` already exists in `users.py` and must cover
  the verify endpoints too — a 6-digit code is 10⁶ and unthrottled verification
  is a brute force with extra steps. Count failures per credential, not per IP.
- **Constant-time comparison** for codes and recovery codes: `hmac.compare_digest`.
- **Recovery codes are hashed** with the same password hasher, shown once, and
  single-use. A recovery code stored in plaintext is a password stored in
  plaintext.
- **WebAuthn challenges** are single-use, bound to the session, and expire.
  Verify `type`, `challenge`, `origin`, and the RP ID hash, and reject on any
  mismatch — an assertion valid for another origin is the attack the ceremony
  exists to prevent.
- **Signature counter regression** (a stored counter that goes backwards) is a
  cloned-authenticator signal. Many authenticators legitimately report 0 always;
  treat 0 as "not reported" and a regression from a non-zero counter as a
  rejection.
- **User verification** (`uv`) is required for passwordless and requested for
  second-factor use; record which, because a policy may care.
- **Nothing is logged.** Secrets, codes, and challenges never reach the recorder.
  `crud.py:194`'s regex already covers `otp`, `mfa`, `totp`, and
  `security[_-]?code`; add a test asserting the new fields are caught by it
  rather than assuming they are.
- **No invariant may depend on `assert`.** Every check above is a `raise`.

## Staging

**Stage 1 — TOTP.** Two-phase enrolment, verification with replay protection and
skew, hashed single-use recovery codes, the store protocol and both
implementations, rate limiting, pending sessions. Guide, reference page, recipe.

**Stage 2 — step-up.** `second_factor_at` on the session, the `AuthRequirement`
that demands a recent factor, Cedar integration, and factor-removal requiring a
factor.

**Stage 3 — WebAuthn as a second factor.** Registration and assertion over
`_ecverify`, CBOR attestation parsing, challenge binding, counter handling.
ES256 and Ed25519 only; RS256 authenticators are rare enough to reject with a
clear message rather than to implement a second signature scheme for.

**Stage 4 — passkeys as a first factor.** Implemented: discoverable credentials, usernameless
login. Only after stage 3 has been used, because the failure modes are the same
ones with no password to fall back to.

## Tests

- `tests/test_users_totp.py` — vectors from RFC 6238 for SHA-1 (the standard's
  own test table), replay rejection, skew boundaries, unconfirmed enrolment never
  activates, recovery code single use, verification throttled.
- `tests/test_users_webauthn.py` — a recorded registration and assertion from a
  known-good authenticator round-trip; wrong origin, wrong RP ID, wrong
  challenge, replayed challenge, and counter regression each rejected
  individually, so a single over-broad check cannot pass the file.
- `tests/test_users_stepup.py` — a pending session is not an identity; a Cedar
  policy requiring a recent factor denies without one and allows with one;
  session identifier changes on promotion.
- `tests/test_users_second_factor_redaction.py` — the new field names are caught
  by the existing sensitive-name regex.
- Run under `python -O` as well as normally. That is cheap here and it is exactly
  the interpreter mode where an `assert`-guarded check would vanish.

## Risks

- **Lockout.** The most likely real-world failure is a user with one factor and
  no recovery codes. Mitigated by issuing recovery codes at enrolment, not on
  request, and by documenting an operator-side removal path in the guide.
- **Clock skew on the server.** TOTP fails silently and confusingly when the
  server's clock drifts. `doctor.py` should check it; the guide should say so.
- **Scope creep into an IdP.** If a design question requires issuing assertions to
  a third party, it is out of scope and belongs with `_auth/oauth2.py`.
