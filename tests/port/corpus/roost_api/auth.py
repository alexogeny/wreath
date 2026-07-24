"""Roost auth — authlib OAuth client registration + python-jose HS256 sessions
(a different auth surface from tumbleweed_api's OIDC/JWKS bearer flow).
"""
from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request
from jose import JWTError, jwt

from .settings import settings

oauth = OAuth()
oauth.register(
    name="trailhead",
    server_metadata_url=settings.jwt_issuer.rstrip("/")
    + "/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email"},
)


def decode_session(request: Request) -> dict:
    token = request.cookies.get("roost_session")
    if not token:
        raise HTTPException(status_code=401, detail="no session")
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="bad session")
