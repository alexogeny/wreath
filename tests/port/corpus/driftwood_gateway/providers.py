"""Outbound provider adapter + inbound webhook signature verification.

Exercises: an ``httpx.AsyncClient`` provider-adapter call, an HMAC webhook
signature verify (business logic — copied verbatim by the codemod), and a small
``.objects.`` repository (the annotate-only query tar-pit).
"""
import hashlib
import hmac

import httpx
from fastapi import HTTPException

from .models import Collar, Provider


async def fetch_remote_collars(provider: Provider) -> list[dict]:
    async with httpx.AsyncClient(base_url=provider.endpoint) as client:
        resp = await client.get("/collars", headers={"x-api-key": provider.api_key})
        return resp.json()


def verify_webhook(secret: str, body: bytes, signature: str) -> None:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="bad webhook signature")


async def providers_for_ranch(ranch_id: str) -> list[Provider]:
    return await Provider.objects.filter(ranch_id=ranch_id, deleted=False).all()


async def collar_by_serial(serial: str) -> Collar | None:
    return await Collar.objects.get_or_none(serial=serial)
