"""Multi-scheme authentication: OIDC/JWKS bearer, M2M client-credentials, and a
composite ``authenticate`` dependency that sets ``request.state.wrangler``.
"""
import time

import httpx
from fastapi import HTTPException, Request
from jose import jwt

from .settings import get_settings

_JWKS_CACHE: dict = {}
_JWKS_TTL = 3600


async def _load_jwks(issuer: str):
    now = time.time()
    cached = _JWKS_CACHE.get(issuer)
    if cached and cached[0] > now:
        return cached[1]
    async with httpx.AsyncClient() as client:
        meta = (
            await client.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
        ).json()
        keys = (await client.get(meta["jwks_uri"])).json()
    _JWKS_CACHE[issuer] = (now + _JWKS_TTL, keys)
    return keys


def _select_key(keys, kid):
    for key in keys["keys"]:
        if key["kid"] == kid:
            return key
    raise HTTPException(status_code=401, detail="unknown signing key")


async def verify_bearer(request: Request):
    settings = get_settings()
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = header.split(" ", 1)[1]
    keys = await _load_jwks(settings.oidc_issuer)
    kid = jwt.get_unverified_header(token)["kid"]
    key = _select_key(keys, kid)
    return jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=settings.oidc_audience,
        issuer=settings.oidc_issuer,
    )


class M2MClient:
    def __init__(self):
        self._token = None
        self._expires = 0.0

    async def token(self) -> str:
        settings = get_settings()
        if self._token and self._expires > time.time() + 30:
            return self._token
        async with httpx.AsyncClient() as client:
            resp = (
                await client.post(
                    settings.oidc_issuer.rstrip("/") + "/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": settings.m2m_client_id,
                        "client_secret": settings.m2m_client_secret,
                    },
                )
            ).json()
        self._token = resp["access_token"]
        self._expires = time.time() + resp.get("expires_in", 3600)
        return self._token


async def authenticate(request: Request):
    claims = await verify_bearer(request)
    request.state.wrangler = {"id": claims["sub"], "roles": claims.get("roles", [])}
    return request.state.wrangler
