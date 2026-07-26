# Add OIDC/OAuth2 login

When you want users to sign in through Cognito, Auth0, Okta, or any OpenID
Connect provider, you don't hand-roll the auth-code dance. Register the provider,
then register the login — Wreath mounts `/auth/login` and `/auth/callback`,
carries the CSRF `state` and PKCE verifier in the signed session, exchanges the
code, verifies the returned `id_token` against the provider's own JWKS, and
writes a principal into the session for you:

```python
import os

from wreath import Wreath
from wreath.auth import SessionIdentityBackend
from wreath.middleware import SessionMiddleware

app = Wreath()
app.add_middleware(SessionMiddleware(secret=os.environ["SESSION_SECRET"]))

# 1. an HTTP client pinned to the issuer origin
app.http_client("idp", base_url="https://issuer.example.com")

# 2. the provider — discovered during lifespan startup, not on first request
app.oidc_provider(
    "idp",
    issuer="https://issuer.example.com",
    audience="my-api",
    http_client="idp",
)

# 3. the login + callback routes
app.oauth2_login(
    "idp",
    provider="idp",
    client_id=os.environ["OIDC_CLIENT_ID"],
    client_secret=os.environ["OIDC_CLIENT_SECRET"],
    redirect_uri="https://app.example.com/auth/callback",
    scopes=("openid", "email"),
)

# 4. read the logged-in identity back out of the session
app.configure_auth(SessionIdentityBackend())
```

Sending a browser to `/auth/login` starts the flow; after the callback,
`request.identity` is populated on every request from the signed session, so
`@authenticated()` and Cedar authorization work unchanged. The `http_client`
origin pin is the anti-SSRF guard: every endpoint the provider reaches must live
on the exact issuer origin, so a tampered discovery document can't redirect the
exchange elsewhere. For machine-to-machine calls instead of a browser login,
reach for `ClientCredentials` from the same module.
