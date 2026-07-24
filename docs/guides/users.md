# User management

Wreath's [auth](auth.md) gives you the primitives — token verification, sessions, a policy engine. The lifecycle *around* them — register, log in, verify an email, reset a password — is the part every app rewrites. `wreath.users` mounts it as a router.

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
