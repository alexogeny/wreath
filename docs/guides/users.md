# User management

Wreath's [auth](auth.md) gives you the primitives — token verification, sessions, a policy engine. The lifecycle *around* them — register, log in, verify an email, reset a password — is the part every app rewrites. `wreath.users` mounts it as a router.

## User story: accounts, without the classic leaks

> *As an API author, I need register / login / verify-email / reset-password on my
> own `User` table. I don't want to hand-roll scrypt hashing or timing-safe
> comparisons, and I never want `register` or `forgot-password` to reveal whether
> an email already has an account.*

```python
from wreath.users import OrmUserStore, default_user_model

User = default_user_model(table="users")
store = OrmUserStore(session, User)

app.users(store, secret=SETTINGS.user_token_secret)   # mounts the full lifecycle under /users
```

Passwords are hashed with stdlib `scrypt` and compared in constant time, and
`register` and `forgot-password` return the same response whether or not the
account exists — so the endpoints can't be turned into a user-enumeration oracle.
The full route list and the pieces you plug in are below.

### Reset, sessions, and guessing

Pass the session store to `user_router(sessions=...)` and a successful password
reset ends that user's other sessions. Without it the reset changes the
credential and nothing more — whoever is already signed in stays signed in,
which is the case that motivates most resets.

Failed sign-ins are throttled per identifier: `max_login_attempts` (10) within
`login_window` (300s), answering `429` in the same shape as a wrong password so
the response does not confirm the account exists. Reset-email issuance is
separately capped by `max_reset_requests` (3) within `reset_window` (15 minutes);
an exhausted budget still returns the same `reset_email_sent` response. A
successful login rotates a server-side session id before adding the principal,
so an anonymous id planted before login cannot become authenticated. The throttle lives in this
router; `wreath._userkit.authenticate` stays unguarded for direct callers.


## Mount the flows

```python
from wreath.users import OrmUserStore, default_user_model

User = default_user_model(table="users")     # or bring your own model
store = OrmUserStore(session, User)

app.users(store, secret=SETTINGS.user_token_secret)
```

That mounts, under `/users`, the full set: `POST /register`, `POST /login`, `POST /logout`, `POST /forgot-password`, `POST /reset-password`, `POST /verify` + `GET /verify/{token}`, and `GET /me`. Login writes the same session principal the auth backend already reads, so a logged-in user *is* an authenticated user everywhere else.

## The pieces you plug in

- **`UserStore`** — a protocol (`get_by_email`, `get_by_id`, `create`, `update`). `OrmUserStore` is the reference implementation; swap it for your own model or datastore.
- **`EmailSender`** — a protocol with a dev `LogEmailSender` default. Verification and reset links are sent through it; wire a real SMTP/SES backend for production.

## Secure by default

Passwords are hashed with stdlib `scrypt` and compared in constant time. Register and forgot-password return **uniform responses** whether or not the account exists (no user enumeration), and login does a dummy hash on an unknown user to keep the timing flat. Reset tokens are HMAC-signed, expiring, and **single-use** — bound to a fingerprint of the current password hash, so a token stops working the moment the password changes.

Prefer to build the router yourself (custom prefix, a link builder that points at your frontend)? Call `user_router(store, secret=..., base_url=..., email_sender=...)` directly and `include_router` it.

## Sending real email

`LogEmailSender` prints verification and reset links to the log — ideal in development. For production, `SmtpEmailSender` delivers over the standard library (zero dependencies), STARTTLS by default or implicit TLS on port 465, with the blocking SMTP work run off the event loop:

```python
from wreath.users import SmtpEmailSender, user_router

mailer = SmtpEmailSender.from_env()   # WREATH_SMTP_HOST / _FROM / _PORT / _USER / _PASSWORD / _TLS
# or, explicitly:
mailer = SmtpEmailSender(host="smtp.example.com", from_addr="no-reply@example.com")

app.include_router(
    user_router(store, secret=SECRET, base_url="https://app.example.com", email_sender=mailer)
)
```

It satisfies the same `EmailSender` protocol, so swapping a bespoke SES or provider backend in later is a one-line change.
