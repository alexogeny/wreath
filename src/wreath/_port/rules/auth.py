"""Authentication and security: hand-rolled token verification and the FastAPI
security schemes.
"""

from __future__ import annotations

from ..ir import NEEDS_REVIEW

AUTH: dict[str, tuple[str, str, str, str]] = {
    "auth.jwt": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "Verifying a JWT by hand is easy to get subtly wrong. Wreath does it for you: app.oidc_provider() for a standard provider, or BearerTokenBackend with JwtVerifier for a token you issue. Both fetch and cache signing keys and check the claims.",
    ),
    "auth.oidc_manual": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "This manually verifies an issuer- and audience-bound JWT. Replace the key fetch and decode together with app.oidc_provider(...), then configure BearerTokenBackend(provider.bearer_verifier()); keep only application-specific claim mapping.",
    ),
    "auth.oauth": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "OAuth is built in: oauth2_login() for a user sign-in flow, ClientCredentials for machine-to-machine. Drop authlib.",
    ),
}

AUTH_SCHEMES: dict[str, tuple[str, str, str, str]] = {
    "auth.security_scheme": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "Wreath authenticates once at the route boundary rather than through a dependency on each route. Configure it with configure_auth(BearerTokenBackend(...)) or ApiKeyBackend(...), and delete the scheme object -- routes stop declaring it.",
    ),
    "auth.security": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "Security(scheme, scopes=[...]) splits in two: the dependency becomes a plain Depends(), and the scopes become @permissions(...) or @roles(...) on the route. Wreath has no scope slot on the dependency itself.",
    ),
}
