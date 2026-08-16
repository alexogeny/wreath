# Second factors (TOTP and passkeys) and step-up

A password is one secret, and everything about the modern web conspires to
leak it: it gets reused, phished, pasted into the wrong window, and dumped from
somebody else's breach. A second factor is the acknowledgement that this will
happen anyway, and that it should not be enough.

Wreath ships two of them inside `wreath.users`, with no new dependency to
install: the authenticator-app factor — the six digits your phone shows,
formally TOTP from RFC 6238 — and WebAuthn, the passkey or security key the
browser asks for. That last part is not a boast about package counts. TOTP is
HMAC-SHA1 over a counter, which the standard library has always had, and a
WebAuthn assertion is one ES256 or Ed25519 signature check, which wreath already
had to write for JWT. The reason most frameworks reach for a package is that
nobody wrote the code, not that it is hard. What *is* hard is the half-dozen
ways the flow around it goes wrong, and that is what this guide is mostly about.

Reference: [`wreath.users`](../reference/users.md).

## User story: a second factor that cannot lock people out

> *As an API author, I want my users to be able to turn on an authenticator app.
> I do not want to be the reason somebody is locked out of their own account
> because a QR code did not scan, and I do not want a code somebody watched
> being typed to still work thirty seconds later.*

```python
from wreath.users import (
    InMemorySecondFactorStore, OrmSecondFactorStore,
    default_second_factor_model, second_factor_router, user_router,
)

Credential = default_second_factor_model(table="user_second_factors")
factors = OrmSecondFactorStore(session, Credential)

app.include_router(user_router(store, secret=SECRET, second_factors=factors))
app.include_router(second_factor_router(store, factors, issuer="Camera Trap"))
```

Two routers, both opt-in. An application that does not mount
`second_factor_router` gets no new routes and no new table, and passing
`second_factors=` to `user_router` is what teaches login that a second factor
can exist at all.

**Both lines, or the login fails closed.** Mounting the second-factor router and
forgetting `second_factors=` on `user_router` is the shape where everything
looks right and nothing is: users enrol, see their factor listed, and then sign
in with a password alone. So the routers know about each other. Building
`second_factor_router` records it against the `UserStore` it was given, and a
`user_router` over that same store with no `second_factors=` of its own refuses
the login of anybody who has a factor enrolled:

```
POST /users/login   {"email": "...", "password": "..."}
    -> 500 {"error": "second_factor_not_wired",
            "detail": "this account has an enrolled totp second factor and this
                       login path cannot check it: user_router was built without
                       second_factors=. ..."}
```

No session is written. That does lock out a deployment that is wired wrong,
which is the trade the refuse-rather-than-half-wire rule in `AGENTS.md`
makes on purpose — a locked door that names its own misconfiguration beats one
that opens quietly — and it is scoped to accounts that actually have a factor,
so a user with nothing enrolled signs in either way. Pass the same store to both
routers and none of this is reached.

## Enrolment happens in two steps, on purpose

`POST /auth/2fa/totp/begin` mints a secret and hands back an `otpauth://` URI to
render as a QR code. **It enrols nothing.** Only `POST /auth/2fa/totp/confirm`,
given a code the user has read off their phone, writes the credential.

```
POST /auth/2fa/totp/begin
    -> {"uri": "otpauth://totp/...", "secret": "JBSWY3...", "digits": 6, "period": 30}

POST /auth/2fa/totp/confirm   {"code": "492013"}
    -> {"status": "enrolled", "id": "...", "recovery_codes": ["x4k2-9fjq-...", ...]}
```

The single most common bug in hand-rolled MFA is doing this in one step: the
secret is stored the moment it is generated, the user's camera does not focus,
they close the tab, and the account now requires codes from an authenticator
entry that does not exist. Here, a begun-but-unconfirmed enrolment expires after
ten minutes and then simply is not there any more. Nothing was ever written, so
nothing has to be undone.

Both routes require an already-signed-in session: enrolling a factor is
something you do to your own account, from inside it.

### Where the unconfirmed secret waits

It has to survive one round trip, and it waits **server-side, always**. The
cookie carries an opaque handle and never the secret. There is no configuration
in which it rides in the session, and nothing to remember to switch on.

By default it goes to a `ChallengeStore` the router builds for itself — the
same place a WebAuthn challenge goes. Name a store and it goes there instead:

```python
store = PostgresSessionStore(app.postgres("main"))
app.configure_http_policy(
    HttpPolicy(session=SessionPolicy(secret=SECRET, store=store))
)
app.include_router(second_factor_router(users, factors, enrolments=store))
```

The usual argument for a cookie being fine — the `begin` response already handed
the same secret to the same browser — is true and incomplete: a response body is
transient, and a cookie is written down. It can reach a disk, a proxy log, or the
next person to use a shared machine. The secret is not a credential either way
(nothing refers to it until a code verifies), but there is no reason to leave it
lying about, and a store also makes an abandoned enrolment revocable.

!!! note "This used to be a warning, and the warning is gone"

    Earlier versions emitted a `UserWarning` when the router was built without
    `enrolments=`, because the secret and the challenge then rode in the session.
    It could only ever name *half* its condition: a router is built before any
    application exists, so it cannot tell whether your `SessionPolicy` was
    given a `store=`. A warning nobody can act on with certainty is one people
    learn to ignore. The property now simply holds, so there is nothing left to
    warn about.

### Which store, and the one behaviour change

`MemoryChallengeStore` is the default and bounds a single worker. Behind more
than one, a ceremony begun on one worker is not spendable on another — pass
`challenges=PostgresChallengeStore(app.postgres("main"))` for a fleet:

```python
app.include_router(
    second_factor_router(
        users, factors, rp_id="example.test",
        challenges=PostgresChallengeStore(app.postgres("main")),
    )
)
```

**This is a real change for one deployment shape**: multiple workers, cookie
sessions, and no store named. A challenge used to ride in the cookie there, so
any worker could finish it; now only the worker that began it can. That fails
*closed* — the caller is refused and begins again — which is the safe direction,
but it is a change rather than a strict improvement, and `challenges=` is the
answer to it.

The enrolment is bound to the user who began it, so a session that changes hands
cannot confirm the secret the previous person started with.

**And it does not outlive the sitting it began in.** `POST /users/login` and
`POST /users/logout` both clear a begun-but-unconfirmed enrolment — the session
key *and* the row behind it, wherever `enrolments=` puts it — as they clear a
half-finished login. An unconfirmed secret sitting in a browser profile on a
shared machine after somebody signed out is state outliving its ceremony, and
the user binding is the second line rather than the first. The same applies to a
live WebAuthn challenge. An application that signs users in *without*
`user_router` — an OAuth2 callback writing the principal itself — clears
nothing, and there the binding is all there is.

### The recovery codes are the whole safety net

`confirm` returns ten single-use recovery codes, and that response is the only
time they exist in readable form. They are stored as a SHA-256 hash — not run
through the password hasher — so there is no way to show them again later, and
no way for a database read to reveal them.

**That is a deliberate difference from passwords, not an oversight.** scrypt is
slow because a password is low-entropy: a person chooses it, so an attacker
holding the hashes guesses from a list and the only defence is to make each
guess expensive. These codes are sixteen characters drawn uniformly from a
thirty-symbol alphabet — about 78 bits — so there is no list, and no search at
any price per guess. Paying a KDF for them bought nothing and cost ~0.4s of a
worker thread at every enrolment plus a ten-hash walk on every wrong code. A
single SHA-256 is what GitHub and Django store recovery codes as, for the same
reason. Passwords keep scrypt, unchanged.

Show them. Make the user acknowledge them. The most likely real failure of any
second factor is a person with one phone, no codes, and a new phone.

## Passkeys and security keys

Pass `rp_id` and four more routes appear. Without it the router is TOTP-only, so
an application that does not want passkeys gets no passkey endpoints:

```python
app.include_router(
    second_factor_router(
        store, factors,
        rp_id="example.com",                       # the registrable domain
        origins=("https://example.com",),          # matched exactly
        enrolments=session_store,
    )
)
```

`rp_id` is a decision rather than a setting: a credential is bound to it for as
long as it exists, so use the registrable domain (`example.com`) and not a
subdomain you might one day move off.

`origins` defaults to `https://{rp_id}`, which is right for a site served at its
apex over TLS — **and to `http://{rp_id}` as well when `rp_id` is loopback**:
`localhost`, `::1`, or anything in 127.0.0.0/8. Browsers grant loopback a secure
context over plain HTTP and WebAuthn genuinely works there, so
`rp_id="localhost"` needs no `origins=` at all, and a loopback origin may carry
any port — `http://localhost:8000` matches `http://localhost` without being
named, because no default set can enumerate the port a development server picked.

That is the only place the default widens. No other host is ever admitted over
`http://`, no port is tolerated off loopback, and there is no wildcard anywhere:
`origins` is an allowlist, and a loose one is the exact vulnerability the origin
check exists to prevent. `http://evil.localhost` and `http://localhost.example.com`
are different hosts and are both refused. Behind a proxy, or on a port in
production, name your origins explicitly.

Registration is two-phase for the same reason TOTP is:

```
POST /auth/2fa/webauthn/begin
    -> PublicKeyCredentialCreationOptions, JSON, ready for the browser
POST /auth/2fa/webauthn/confirm  {"client_data": "...", "attestation_object": "...", "label": "YubiKey"}
    -> {"status": "enrolled", "id": "...", "user_verified": true, "recovery_codes": [...]}
```

and the assertion half is the same pair pointed at a login or a step-up:

```
POST /auth/2fa/webauthn/verify/begin
    -> PublicKeyCredentialRequestOptions, with the caller's own credentials listed
POST /auth/2fa/webauthn/verify   {"id": "...", "client_data": "...", "authenticator_data": "...", "signature": "..."}
    -> the profile (a pending login, promoted) or {"status": "second_factor_verified"}
```

The browser deals in `ArrayBuffer`s and JSON does not, so the page's own script
base64url-encodes what `navigator.credentials` hands back — which is the encoding
WebAuthn already uses for every binary member of the options it is given.

**Only `none` attestation is accepted.** `begin` asks for it, a conforming client
replaces whatever the authenticator produced with a none statement when it is
asked, and anything else is refused. Verifying an attestation statement means
checking it against a metadata service, which means a network dependency and a
different product; consumer passkey flows do not use it, and neither does this.

**ES256 and Ed25519 only.** An RS256 authenticator is refused with a message
naming the algorithm rather than met with a second signature scheme implemented
for a case almost nobody hits.

### What a ceremony has to prove

Four separate checks, four separate refusals, and one test each:

- **`type`** — a registration cannot answer with an assertion's client data, or
  the other way about.
- **`challenge`** — minted by `begin`, held server-side, and matched with
  `hmac.compare_digest`.
- **`origin`** — matched against `origins`, exactly, but for a loopback host
  where the port may vary. This is the reason the ceremony exists: a signature
  collected by a phishing site names *that* site
  here and so does not match. A ceremony running in a cross-origin frame is
  refused too, since the frame's origin would match while the user is looking at
  somebody else's page.
- **the RP ID hash** — the origin is the browser's statement; the RP ID hash is
  inside the bytes the authenticator signed.

Most of the suite behind those checks signs with a synthetic authenticator — a
real P-256 or Ed25519 key, a real signature, real CBOR/COSE/DER — which shows
that Wreath and an independent signature implementation agree about the wire
formats, and shows nothing about interoperability, because the same test module
wrote the bytes it reads back. One registration and one assertion are therefore
transcribed from the [W3C WebAuthn Level 3 specification's own test
vectors](https://www.w3.org/TR/2026/CR-webauthn-3-20260526/#sctn-test-vectors-none-es256)
(§16.2, `none` attestation over ES256) and verified as they stand.

### The challenge is single-use, unconditionally

`begin` mints it, binds it to the user who began the ceremony, and `confirm` or
`verify` **spends it before verifying anything** — success, failure, expired,
all the same. A challenge that survived a failed attempt would let a recorded
assertion be posted again until it timed out.

Spending it is one statement:

```sql
DELETE FROM wreath_second_factor_challenges
WHERE handle = $1 AND user_id = $2 AND kind = $3 AND expires > clock_timestamp()
RETURNING payload
```

Two properties fall out of that shape, and neither is a check written beside it:

- **The returned row *is* the consumption.** Exactly one concurrent statement can
  return it, so two completions of one challenge cannot both proceed.
- **The user binding is a `WHERE` clause, not an afterthought.** A mismatched
  attempt matches no row, so it deletes nothing and the rightful user's ceremony
  survives it. Checking *after* consuming would let anyone holding a handle burn
  somebody else's login.

Who the caller may be is derived from the session — a half-finished login first,
then whoever is signed in — and handed to that statement as a *precondition*. It
is never read back out of the record: a record that names its own owner means
whoever holds a handle defines who they are.

A browser that signs out and signs back in as somebody else therefore cannot
finish the previous person's ceremony, and session rotation carrying the session
contents across is exactly why that binding has to exist.

Challenges expire after five minutes (`webauthn_ttl`).

### The signature counter, and why zero is not a regression

An authenticator that reports a signature counter increments it every time. If a
stored non-zero counter does not increase, two devices are answering for one
credential — the authenticator has been cloned — and the assertion is refused.

Many perfectly good authenticators report `0` forever instead, and every passkey
in a synced credential manager does. Zero on both sides is read as *not
reported*, never as a regression; a counter that has been above zero and then
drops, including dropping to zero, is refused.

### User verification is recorded, not demanded

The ceremonies ask for `userVerification: "preferred"`: verify the user with a
PIN or a fingerprint where the device can, and let a security key without one
still work. The outcome lands on the session principal as `second_factor_uv`
beside `second_factor_at`, so a policy that cares about the difference can read
it rather than assume it. `user_verification="required"` tightens it for the
whole router. Discoverable first-factor login always requires it.

### Recovery codes come with the first key too

A user whose only factor is a security key, and who loses the key, is the
lockout this whole feature has to avoid. So the first WebAuthn registration
issues the same ten single-use recovery codes a TOTP enrolment does, in the same
one response that will ever contain them. A second key returns
`"recovery_codes": []` — the user already has a set, and minting another would
put two pieces of paper in circulation with neither invalidating the other.

## What happens at login

With `second_factors=` configured, `POST /users/login` grows a third answer.
Correct password, no enrolled factor: signed in, exactly as before. Correct
password *and* an enrolled factor:

```
POST /users/login   {"email": "...", "password": "..."}
    -> 200 {"status": "second_factor_required", "methods": ["recovery", "webauthn"]}
```

`methods` says what this user can actually satisfy the challenge with, so the
page knows whether to prompt for six digits or to call
`POST /auth/2fa/webauthn/verify/begin`.

No profile comes back, and — this is the part that matters — **no principal is
written to the session**. What is written is a pending marker under its own key.
`SessionIdentityBackend` builds an identity from the principal, so a pending
session is not an identity anywhere in the framework: every `@authenticated`
route, every permission check, and every Cedar policy sees an anonymous caller.
There is no flag for an application to remember to check, because there is
nothing for it to check.

`POST /auth/2fa/verify` finishes the job:

```
POST /auth/2fa/verify   {"code": "492013"}     # or a recovery code
    -> 200 {"id": "...", "email": "...", "is_verified": true}
```

The session id is rotated at that moment, exactly as it is at login: promotion
from pending to full is a privilege change, and an id an attacker planted before
the password step must not be the id that ends up signed in. The pending marker
expires on its own after five minutes, so a half-finished login is not left
standing as an invitation to guess at the second half.

## Step-up: asking *when*, not *whether*

A second factor at login is a formality by the afternoon. The interesting
question before a destructive action is not "does this person have a factor" but
"did they prove one just now" — because the session that is about to delete an
account may have been left open in a coffee shop three hours ago.

Verification records the moment. `second_factor_at` goes onto the session
principal, so it arrives on `Identity.claims`, and a route says what it needs:

```python
from wreath.auth import second_factor

@app.delete("/accounts/{account_id}")
@second_factor(max_age=300)
async def close_account(request: Request) -> dict:
    ...
```

An identity that has not proved a factor within the window is refused with a
**403** whose detail is `second_factor_required` — the caller is signed in, so
re-entering a password would not help. The remediation is the same
`POST /auth/2fa/verify` route: posted from an *already signed-in* session it is
step-up rather than promotion, checks the code, rotates the session id, and
re-stamps the principal. Then the original request succeeds.

An identity carrying no stamp at all never satisfies the requirement. A bearer
token and an OIDC login have none, so a route guarded this way refuses them
rather than reading an absent record as a fresh one — absent is a refusal, never
a zero.

The same fact reaches Cedar as `context.second_factor_age`, in seconds, so a
policy can insist on recency without the application threading a flag through
every handler:

```
permit(principal, action == Action::"close", resource)
when { context has second_factor_age && context.second_factor_age <= 300 };
```

The key is **absent** rather than a large number when there is no factor to
report, which is what makes both shapes fail closed: a `when` guarded by `has`
is false, and an `unless` guarded by `has` leaves the `forbid` standing.

One caveat worth knowing: the permission manifest from `permissions_router` does
not model freshness. A route behind `@second_factor` can appear permitted in the
manifest and then answer 403, which is the same "chrome may be optimistic,
enforcement is on the route" property the manifest already documents.

Guard the actions that warrant re-prompting. A whole API behind `@second_factor`
is a five-minute session with extra steps, and users learn to type codes at
anything that asks.

### Where else a window can be declared

Nothing above is HTTP-specific: `second_factor_at` is a claim, and every enforcer
in Wreath reads the same one through the same check. Two other places take a
window directly, so a generated route or a model-facing tool does not have to be
hand-written to get one:

```python
from wreath.crud import Access

app.crud(Account, open_session, authorize={
    "delete": Access.roles("admin").within(300),   # generated CRUD
})

@mcp.tool(action="Sighting::purge", second_factor=300)   # an MCP tool
async def purge_sightings(request) -> dict:
    ...
```

The MCP one is the most interesting, because the "person" and the "caller" come
apart: the model calls the tool, and the *human* is the one who has to have
proved a factor recently. That is how "the model may read, and the human must
step up before the model may delete" is written down — see
[MCP: what guards it](mcp.md#what-guards-it). Exposing a route that already
carries `@second_factor` keeps its window unchanged, and stacking two keeps the
shorter one.

## Turning a factor off

```
DELETE /auth/2fa/{id}      -> 200 {"status": "removed", "id": "..."}
                           -> 403 {"error": "second_factor_required"}
```

Removing a second factor is the first thing somebody holding a stolen session
wants to do, so it is guarded by the one thing they do not have: a factor proved
within the last five minutes (`step_up_ttl`). Enrolling the **first** factor
counts — `confirm` stamps the session, since a code out of the new authenticator
was just checked — so a user who has only ever enrolled does not have to step up
separately to undo it.

**Enrolling an additional factor does not stamp anything**, and the asymmetry is
the point. A stamp answers "did this caller prove a factor the account already
had". A factor the caller has just chosen answers it with an authenticator they
brought themselves — so if enrolling always stamped, somebody holding a stolen
session would register a passkey of their own, be stamped for it, and walk
through this route and every `@second_factor` route with the guard that exists
to stop them. Adding a key to an account that already has one therefore leaves
the stamp where it was, and removing something afterwards means proving a factor
at `POST /auth/2fa/verify` like anybody else.

The id is looked up among the caller's own credentials and the store's `remove`
is scoped to the owner as well, so somebody else's id answers 404 — the same
answer as an id that does not exist, which keeps the route from being a probe.

**Removing the last real factor removes the recovery codes with it.** Otherwise
the user is told their authenticator is gone while login still demands a code,
and the only codes left are on the paper they threw away when they turned the
feature off. For the same reason a recovery code cannot be deleted by id at all:
one at a time only ever moves somebody closer to being locked out.

## The parts that are easy to get wrong

Each of these is a check in the code with a test named after it, rather than a
paragraph of advice:

- **A code that verified once never verifies again.** Every credential stores
  the newest step it accepted, and verification requires a strictly greater one.
  Without this, a code is valid to anyone who saw it for the rest of its window,
  which is the entire attack a second factor is supposed to stop.
- **Not even twice at once.** Verification reads the credential, checks the
  code, and writes the counter back, with real awaits in between — so two
  requests carrying one observed code both read the same stored counter, and a
  plain `UPDATE ... SET counter = ...` lets both of them through. Racing the
  legitimate user is precisely the attack, so `SecondFactorStore.touch` is a
  conditional advance that **reports whether it won**: `WHERE counter < $1
  RETURNING 1` against PostgreSQL, a compare-and-set in a non-suspending
  function in memory. Losing is treated as a replay and refused. Implementing
  the protocol yourself means implementing that, not merely storing a number.
- **Skew is one step either side.** Thirty seconds of forgiveness for a slow
  phone and a slow typist. It is configurable, up to a hard stop, because a wide
  window is just a slower-expiring password.
- **Verification is throttled per user.** Six digits is a million guesses, which
  is an afternoon at any request rate you would otherwise allow. Five failures
  in five minutes and the endpoint answers `429` with a `Retry-After` — counted
  per account rather than per address, since addresses are the cheap thing for
  an attacker to acquire.
- **Comparisons are constant-time**, via `hmac.compare_digest`, so the time a
  wrong code takes says nothing about how much of it was right.
- **Secrets are never rendered.** `GET /auth/2fa` lists labels, kinds and dates
  and counts the recovery codes remaining; it never returns credential material.
  The dataclass keeps its material out of its own `repr`, so a traceback cannot
  spill it, and the reference model's column is called `secret_material` so that
  `wreath.crud` and the GraphQL schema builder hide it by name.

## Operating it

**The support path** — a user who has lost the phone *and* the codes — runs
through the store directly, since by definition they cannot satisfy the HTTP
route's step-up:

```python
for credential in await factors.credentials(user_id):
    await factors.remove(user_id, credential.id)
```

Do that behind whatever identity check your support tooling already uses — and
note that `remove` is scoped to the owner, so a credential id alone is not
enough to delete somebody else's factor.

**Watch the server clock.** TOTP is arithmetic on the current time, so a server
whose clock has drifted more than a step from its users' phones rejects every
correct code, and reports it as a wrong code. It is a confusing failure with an
easy cause; `wreath doctor` is the place to look first.

**Do not throw the recovery codes into the log.** They are returned in exactly
one response body, and everything else in this module is built on the assumption
that this is the only place they appear.

## Passkeys as a first factor

Set `passkey_login=True` on the router to require discoverable resident
credentials at enrolment and mount a usernameless login pair:

```python
app.include_router(
    second_factor_router(
        users,
        factors,
        rp_id="example.com",
        passkey_login=True,
    )
)
```

`POST /auth/2fa/webauthn/login/begin` returns an empty `allowCredentials` list,
so the authenticator chooses an account. `POST /auth/2fa/webauthn/login` verifies
that assertion with user verification required, resolves its owner through one
indexed `DiscoverableSecondFactorStore.credential` lookup, rotates the session, and writes
the same principal as password login. Existing non-resident security keys keep
working as second factors but cannot identify an account.

## What is not here yet

Also deliberately absent: attestation verified against a metadata service, SMS
and email one-time codes, and push approval. The reasoning for each is in
`docs/plans/second-factors-totp-webauthn.md`; the short version is that the first
needs a network dependency, the second two are worse than they look, and the
third needs a mobile SDK.
