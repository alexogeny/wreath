"""Manual discovery-key loading and token verification for station identity."""

import httpx
from jose import jwt


async def load_keys(issuer):
    async with httpx.AsyncClient() as client:
        response = await client.get(issuer.rstrip("/") + "/.well-known/jwks.json")
        return response.json()


async def verify_station_token(token, issuer, audience):
    keys = await load_keys(issuer)
    return jwt.decode(
        token,
        keys,
        algorithms=["RS256"],
        audience=audience,
        issuer=issuer,
    )
