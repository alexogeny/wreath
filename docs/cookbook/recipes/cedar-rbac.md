# Authorize with Cedar policies (RBAC)

When "require the admin role" grows into real rules — editors may read documents,
but only with a `GET`, and never someone else's draft — stop scattering `if`
checks through handlers and write the rules as Cedar policies. Wreath ships its
own Cedar engine (no dependency, no sidecar, evaluated natively), parses your
policy set once at startup, and gates handlers with `@authorize`:

```python
from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, CedarPolicies, EntityUid, authorize

app = Wreath()

async def verify(token: str) -> Identity | None:
    user = await lookup(token)
    return Identity(user.id, roles=frozenset(user.roles)) if user else None

engine = CedarPolicies("""
    permit(principal in Role::"editor", action == Action::"Document::read", resource)
      when { context.method == "GET" };
    forbid(principal, action, resource) when { context.method != "GET" };
""")
app.configure_auth(BearerTokenBackend(verify), CedarAuthorizer(engine=engine))

@app.get("/documents/{document_id}")
@authorize(
    action="Document::read",
    resource=lambda request: EntityUid("Document", request.path_params["document_id"]),
)
async def document(request) -> dict:
    return {"id": request.path_params["document_id"]}
```

The default mappers wire the common case for you: the authenticated identity
becomes the principal, its roles become `Role::"..."` parents (so `principal in
Role::"editor"` just works), and the request method and path arrive as `context`.
`forbid` overrides `permit`, the default is deny, and a policy that errors is
skipped and reported in the decision's diagnostics — never silently satisfied. A
syntax error in the policy string is an application bug caught at startup, not a
request-time surprise. When rules are truly simple, `@roles("admin")` from
`wreath.authorization` is the lighter tool.
