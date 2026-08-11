"""Who may read hut state, and who may hold a bed.

Both halves of a small service's auth surface: a scoped ``Security()``
dependency the routes declare one at a time, and an authlib client for the
warden console's sign-in.
"""
from authlib.integrations.starlette_client import OAuth
from fastapi import Security
from fastapi.security import OAuth2AuthorizationCodeBearer

warden_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl="https://id.ridgeline.invalid/authorize",
    tokenUrl="https://id.ridgeline.invalid/token",
    scopes={"huts:read": "Read hut state", "huts:write": "Hold and release beds"},
)

oauth = OAuth()
oauth.register(
    name="warden",
    server_metadata_url="https://id.ridgeline.invalid/.well-known/openid-configuration",
    client_kwargs={"scope": "openid profile"},
)


async def current_warden(token: str = Security(warden_scheme, scopes=["huts:read"])):
    return _claims(token)


async def hold_writer(token: str = Security(warden_scheme, scopes=["huts:write"])):
    return _claims(token)


def _claims(token: str) -> dict:
    raise NotImplementedError("the session store is wired up in the app factory")
