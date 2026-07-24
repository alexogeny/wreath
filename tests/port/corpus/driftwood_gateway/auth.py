"""Integration-service auth idioms (anonymized from a real integration gateway).

Exercises: a legacy pydantic v1 ``BaseSettings``, an authlib ``OAuth2Session``
client-credentials client, a PyJWT ``jwt.decode`` verify, module-level
``HTTPException`` constants (positional status), an ``Action`` enum, and a
CLASS-BASED dependency (``__call__``) used via ``Depends(AuthorizeWithActions([...]))``.
"""
import logging
import os
from enum import Enum
from typing import Any

import jwt
from authlib.integrations.requests_client import OAuth2Session
from fastapi import Header, HTTPException
from pydantic import BaseSettings

logger = logging.getLogger(__name__)
M2M_CLIENT_ID = os.environ.get("M2M_CLIENT_ID")

INVALID_AUTH = HTTPException(401, detail="invalid auth header")
NOT_AUTHENTICATED = HTTPException(401, detail="not authenticated")
UNAUTHORISED = HTTPException(403, detail="unauthorised")


class OAuthSettings(BaseSettings):
    OAUTH2_URL: str
    OAUTH2_CLIENT_ID: str
    OAUTH2_CLIENT_SECRET: str


class Action(str, Enum):
    READ = "READ"
    WRITE = "WRITE"


class TrailheadOAuthClient:
    """Service-to-service (M2M) token exchange via client credentials."""

    _session: OAuth2Session | None = None

    @classmethod
    def initialise(cls, settings: OAuthSettings) -> None:
        cls._session = OAuth2Session(
            settings.OAUTH2_CLIENT_ID,
            settings.OAUTH2_CLIENT_SECRET,
            token_endpoint_auth_method="client_secret_basic",
        )
        cls._token_url = settings.OAUTH2_URL

    @classmethod
    def token(cls) -> str:
        assert cls._session is not None
        tok = cls._session.fetch_token(cls._token_url, grant_type="client_credentials")
        return tok["access_token"]


def decode_bearer(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


class AuthorizeWithActions:
    """A class-based FastAPI dependency: ``Depends(AuthorizeWithActions([Action.READ]))``."""

    def __init__(self, actions: list[Action]):
        self.actions = actions

    async def __call__(self, authorization: str = Header(...)) -> dict[str, Any]:
        if not authorization.lower().startswith("bearer "):
            raise INVALID_AUTH
        claims = decode_bearer(authorization.split(" ", 1)[1])
        if not claims:
            raise NOT_AUTHENTICATED
        return claims
