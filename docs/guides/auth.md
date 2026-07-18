# Authentication and authorization

These two words are often blurred together, and blurring them is where security
bugs hide. Wreath keeps them apart, in plain terms:

- **Authentication** establishes *who* a request is. It lives in `wreath.auth`.
- **Authorization** decides *what* that identity is allowed to do. It lives in
  `wreath.authorization`.

Identity first, permission second — always in that order.

## Establishing identity

Configure a backend that turns a credential into an `Identity`, or into nothing
if the credential doesn't check out:

```python
from wreath.auth import BearerTokenBackend, Identity

async def verify(token: str) -> Identity | None:
    user = await lookup(token)
    return Identity(user.id, roles=frozenset(user.roles)) if user else None

app.configure_auth(BearerTokenBackend(verify))
```

Once configured, a handler can ask whether the request is authenticated and who
it belongs to, and the `authenticated` decorator will turn away anyone who
isn't.

## Deciding what they may do

Require a role or a permission, or hand the decision to the built-in Cedar policy
engine when your rules grow beyond a simple check:

```python
from wreath.authorization import roles, permissions, authorize, CedarEngine

@app.get("/admin")
@roles("admin")
async def admin(request) -> dict:
    return {"ok": True}
```

When a request is denied, Wreath still runs your global finalizers — so the
`401` or `403` carries the same security headers and CORS treatment as a success
— but it skips the route middleware and your handler entirely. The person who
shouldn't be there never reaches your code, and the response that turns them away
is still well-formed.

**Reference:** [`wreath.auth`](../reference/auth.md),
[`wreath.authorization`](../reference/authorization.md).
