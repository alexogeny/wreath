"""OAuth2 helpers: machine-to-machine tokens and an auth-code + PKCE login.

The login flow registers `/auth/login` and `/auth/callback` on the app,
carries CSRF `state` and the PKCE verifier in the signed session, exchanges
the code at the provider's (origin-pinned) token endpoint, verifies the returned
`id_token` with the provider's own JWKS verifier, and writes a minimal
principal into the session for the `SessionIdentityBackend` to read.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode

from ..middleware.sessions import rotate_session
from ..response import JSONResponse, RedirectResponse
from .oidc import OidcProvider, _same_origin_path

if TYPE_CHECKING:
    from ..request import Request

__all__ = ["ClientCredentials", "register_oauth2_login"]

_TOKEN_SKEW = 30.0


def _bearer_401(error: str) -> JSONResponse:
    """A 401 with the RFC 6750 §3 Bearer challenge (RFC 9110 §15.5.2 requires
    a 401 to carry WWW-Authenticate)."""
    response = JSONResponse({"error": error}, status=401)
    response.headers.append((b"www-authenticate", b'Bearer error="invalid_token"'))
    return response


class ClientCredentials:
    """Cached OAuth2 client-credentials (M2M) token acquisition.

    `token_path` is a path on `http_client`'s pinned origin, or a zero-arg
    callable returning one (so it can be resolved lazily after OIDC discovery).
    """

    __slots__ = (
        "_client",
        "_client_id",
        "_client_secret",
        "_expires_at",
        "_lock",
        "_scope",
        "_token",
        "_token_path",
    )

    def __init__(
        self,
        *,
        http_client: Any,
        token_path: str | Callable[[], str],
        client_id: str,
        client_secret: str,
        scope: str | None = None,
    ) -> None:
        self._client = http_client
        self._token_path = token_path
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        loop = asyncio.get_running_loop()
        if self._token is not None and loop.time() < self._expires_at - _TOKEN_SKEW:
            return self._token
        async with self._lock:
            if self._token is not None and loop.time() < self._expires_at - _TOKEN_SKEW:
                return self._token
            await self._refresh(loop)
            assert self._token is not None
            return self._token

    async def _refresh(self, loop: asyncio.AbstractEventLoop) -> None:
        form: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            form["scope"] = self._scope
        token_path = self._token_path
        # isinstance(str), not callable(): ty narrows the else-branch to the
        # Callable so the call resolves.
        path = token_path if isinstance(token_path, str) else token_path()
        response = await self._client.post(
            path,
            headers=((b"content-type", b"application/x-www-form-urlencoded"),),
            body=urlencode(form).encode("ascii"),
        )
        if response.status != 200:
            raise RuntimeError(f"client-credentials token request failed: HTTP {response.status}")
        document = json.loads(response.body)
        access = document.get("access_token")
        if not isinstance(access, str):
            raise RuntimeError("token response missing 'access_token'")
        ttl = document.get("expires_in", 3600)
        self._token = access
        self._expires_at = loop.time() + float(ttl)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def register_oauth2_login(
    app: Any,
    name: str,
    *,
    provider: OidcProvider,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: tuple[str, ...] = ("openid", "email"),
    login_path: str = "/auth/login",
    callback_path: str = "/auth/callback",
    post_login_redirect: str = "/",
    session_key: str = "principal",
) -> None:
    """Register the login + callback routes for `provider` on `app`."""

    state_key = f"_oidc_state_{name}"
    verifier_key = f"_oidc_verifier_{name}"
    nonce_key = f"_oidc_nonce_{name}"

    @app.get(login_path)
    async def login(request: Request) -> RedirectResponse | JSONResponse:
        if provider.authorization_endpoint is None:
            return JSONResponse({"error": "provider_not_discovered"}, status=503)
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        # `state` protects the callback; `nonce` protects the *token*. Without
        # it an id_token minted for another session of the same client is
        # accepted here, because nothing ties the token to this login attempt
        # (OIDC Core §3.1.2.1, §3.1.3.7 step 11).
        nonce = secrets.token_urlsafe(24)
        session = getattr(request.state, "session", None)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        session[state_key] = state
        session[verifier_key] = verifier
        session[nonce_key] = nonce
        params = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return RedirectResponse(f"{provider.authorization_endpoint}?{params}", status=302)

    @app.get(callback_path)
    async def callback(request: Request):
        query = parse_qs(request.query_string.decode("ascii", "replace"))
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        session = getattr(request.state, "session", None)
        if session is None:
            return JSONResponse({"error": "session_middleware_required"}, status=500)
        expected_state = session.pop(state_key, None)
        verifier = session.pop(verifier_key, None)
        expected_nonce = session.pop(nonce_key, None)
        if not code or not state or state != expected_state or not verifier:
            return JSONResponse({"error": "invalid_state"}, status=400)
        if provider.token_endpoint is None:
            return JSONResponse({"error": "provider_not_discovered"}, status=503)
        token_path = _same_origin_path(provider.issuer, provider.token_endpoint)
        response = await provider._client.post(  # same package
            token_path,
            headers=((b"content-type", b"application/x-www-form-urlencoded"),),
            body=urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code_verifier": verifier,
                }
            ).encode("ascii"),
        )
        if response.status != 200:
            return JSONResponse({"error": "token_exchange_failed"}, status=502)
        document = json.loads(response.body)
        id_token = document.get("id_token")
        if not isinstance(id_token, str):
            return _bearer_401("missing_id_token")
        identity = await provider.bearer_verifier()(id_token)
        if identity is None:
            return _bearer_401("invalid_id_token")
        # The verifier checked the signature and the registered claims; the
        # nonce is this flow's own binding and has to be checked here.
        if expected_nonce and identity.claims.get("nonce") != expected_nonce:
            return _bearer_401("nonce_mismatch")
        # The caller's privileges change here, so the id they arrived with must
        # not survive: an attacker who fixed a session id beforehand would
        # otherwise hold one that is now authenticated. A no-op for cookie-backed
        # sessions, which carry no server-side id.
        rotate_session(request)
        session[session_key] = {
            "sub": identity.id,
            "type": identity.type,
            "roles": sorted(identity.roles),
            # Carried so an SSO caller and a bearer caller reach the authorizer
            # with the same shape. Without it, `@authorize(permissions=...)`
            # refused every SSO session while admitting the same person's token.
            "permissions": sorted(identity.permissions),
            # The provider's own expiry, so the session ends when the token
            # would have rather than when the cookie happens to.
            "exp": identity.claims.get("exp"),
        }
        return RedirectResponse(post_login_redirect, status=302)
