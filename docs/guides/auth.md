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

Require a role or a permission:

```python
from wreath.authorization import roles, permissions, authorize

@app.get("/admin")
@roles("admin")
async def admin(request) -> dict:
    return {"ok": True}
```

When your rules grow beyond a simple check, write them in Cedar. Wreath ships
its own Cedar policy engine — no dependency, no service, evaluated natively —
and `CedarPolicies` parses your policy set once at startup, where a syntax
error is an application bug rather than a request-time surprise:

```python
from wreath.authorization import CedarAuthorizer, CedarPolicies, EntityUid, authorize

engine = CedarPolicies("""
    permit(principal in Role::"editor", action == Action::"Document::read", resource);
    forbid(principal, action, resource) when { context.method != "GET" };
""")
app.configure_auth(BearerTokenBackend(verify), CedarAuthorizer(engine=engine))

@app.get("/documents/{id}")
@authorize(
    action="Document::read",
    resource=lambda request: EntityUid("Document", request.path_params["id"]),
)
async def document(request) -> dict:
    ...
```

The default mappers model the common case: the authenticated identity becomes
the principal, its roles become `Role::"..."` parents (so `principal in
Role::"editor"` works with no further wiring), and the request method and path
arrive as `context`. Forbid overrides permit, the default is deny, and a
policy that errors is skipped and reported in the decision's diagnostics —
never silently satisfied. The engine covers the Cedar core; extension types
(`ip`, `decimal`, `datetime`) and schema validation are not implemented yet
and fail loudly at parse time. A different evaluator can be swapped in through
the same `CedarEngine` protocol the built-in engine satisfies.

When a request is denied, Wreath still runs your global finalizers — so the
`401` or `403` carries the same security headers and CORS treatment as a success
— but it skips the route middleware and your handler entirely. The person who
shouldn't be there never reaches your code, and the response that turns them away
is still well-formed.

**Reference:** [`wreath.auth`](../reference/auth.md),
[`wreath.authorization`](../reference/authorization.md).
