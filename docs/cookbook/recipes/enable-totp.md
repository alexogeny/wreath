# Turn on an authenticator app (TOTP)

You already mount `user_router`, and you want the six-digit code from a phone as
a second factor. Two changes: give the login router somewhere to look for
credentials, and mount the router that enrols and verifies them.

```python
from wreath.users import (
    OrmSecondFactorStore, OrmUserStore,
    default_second_factor_model, default_user_model,
    second_factor_router, user_router,
)

User = default_user_model()
Credential = default_second_factor_model()          # a new table, not new columns

users = OrmUserStore(session, User)
factors = OrmSecondFactorStore(session, Credential)

app.include_router(user_router(users, secret=SECRET, second_factors=factors))
app.include_router(second_factor_router(users, factors, issuer="Camera Trap"))
```

Both routers need `SessionPolicy` registered globally, as login already did.

**Both lines, not just the second one.** `second_factors=` is what teaches login
that a factor exists; mounting only `second_factor_router` would leave users
enrolling, seeing the factor listed, and still signing in with a password alone.
That mistake is now refused rather than served: a login it cannot check answers
`500 {"error": "second_factor_not_wired", ...}` naming what to pass, and writes
no session.

## The enrolment call, from the client's side

```
POST /auth/2fa/totp/begin                       # signed in already
    -> {"uri": "otpauth://totp/...", "secret": "JBSWY3...", "digits": 6, "period": 30}
```

Render `uri` as a QR code and show `secret` for anyone typing it in by hand.
**Nothing is enrolled yet** — that is the point of the two calls. When the user
types what their phone shows:

```
POST /auth/2fa/totp/confirm   {"code": "492013"}
    -> {"status": "enrolled", "id": "...", "recovery_codes": ["x4k2-9fjq-mn3p-7wzr", ...]}
```

Show those recovery codes and make the user acknowledge them. They are SHA-256
hashes in the database from this moment on; this response is the only time they
are readable, by anyone, ever. (One hash pass rather than the password hasher:
78 bits of entropy has no guessing list to defend against — the guide explains.)

## What login looks like afterwards

```
POST /users/login    -> 200 {"status": "second_factor_required", "methods": ["recovery", "totp"]}
POST /auth/2fa/verify   {"code": "492013"}    -> 200 {"id": ..., "email": ...}
```

Between those two calls the caller holds a *pending* session, which carries no
principal — so `SessionIdentityBackend` yields no identity, and every
`@authenticated()` route treats them as anonymous without your handlers
checking anything. `POST /auth/2fa/verify` rotates the session id as it
promotes, and takes a recovery code in the same field as a TOTP code.

A user with no enrolled factor logs in exactly as before, so this is safe to
roll out to an existing user base one account at a time.

## Demanding a *recent* factor

Verification stamps `second_factor_at` on the session, so a route can ask when
rather than whether:

```python
from wreath.auth import second_factor

@app.delete("/accounts/{account_id}")
@second_factor(max_age=300)
async def close_account(request) -> dict: ...
```

A caller who has not proved a factor in the last five minutes gets a `403`
whose detail is `second_factor_required`; posting a code to
`POST /auth/2fa/verify` from that already-signed-in session steps them up and
the request then succeeds. `DELETE /auth/2fa/{id}` is guarded the same way,
because turning a second factor off is what a stolen session wants most.

Full flow, the security properties, and the operator's removal path:
[Second factors](../../guides/second-factors.md).
