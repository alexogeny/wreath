---
description: Authenticate callers, require explicit access declarations and test denial paths.
keywords: guide bearer authentication identity users sessions authorization security
---

# Identity and users

Authentication answers who called. Route declarations answer whether proof is required.
Keep both explicit, and make forgotten declarations a startup error.

```python title="app.py"
from wreath import Request, Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated, public


identities = {
    "dev-alice": Identity("alice", roles=frozenset({"member"})),
    "dev-operator": Identity("operator", roles=frozenset({"operator"})),
}


def verify_token(token: str) -> Identity | None:
    return identities.get(token)


app = Wreath(require_access_declarations=True)
app.configure_auth(BearerTokenBackend(verify_token))


@app.get("/health")
@public()
async def health(request: Request) -> dict:
    return {"status": "ok"}


@app.get("/me")
@authenticated()
async def me(request: Request) -> dict:
    return {
        "id": request.identity.id,
        "roles": sorted(request.identity.roles),
    }
```

```python title="test_app.py"
from wreath.testing import TestClient

from app import app


async def test_public_work_does_not_need_credentials() -> None:
    async with TestClient(app) as client:
        response = await client.get("/health")
    assert response.status == 200


async def test_private_work_challenges_then_publishes_identity() -> None:
    async with TestClient(app) as client:
        missing = await client.get("/me")
        invalid = await client.get(
            "/me",
            headers={"authorization": "Bearer unknown"},
        )
        allowed = await client.get(
            "/me",
            headers={"authorization": "Bearer dev-operator"},
        )

    assert missing.status == invalid.status == 401
    assert allowed.json() == {"id": "operator", "roles": ["operator"]}
```

Replace the development mapping with a constant-time token lookup, JWT verifier or
session backend. Do not put a database query in public routes: Wreath only invokes the
backend when a route's compiled access requirement needs identity.

For registration, verification, reset, sessions, TOTP and WebAuthn, mount
`user_router` and `second_factor_router`; the complete path is exercised in
[the serious API story](../stories/serious-api.md). SAML, OIDC, SCIM, Cedar and tenant
boundaries live in the [identity reference](../reference/identity.md).
