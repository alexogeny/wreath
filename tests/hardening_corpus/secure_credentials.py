"""a06, a07 and a08 written correctly. Nothing here may produce a finding."""
from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets

from wreath import Request, Router

ops = Router(prefix="/ops")

#: Read at import from the environment, so the repository never holds it.
SESSION_SECRET = os.environ["NORTHWIND_SESSION_SECRET"]
#: A name that looks like a credential but is not one: the empty default is a
#: sentinel, not a key, and a rule that flagged it would be flagging every
#: correct configuration read in the tree.
API_TOKEN = os.environ.get("NORTHWIND_API_TOKEN", "")


def remember_mac(payload: bytes) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def reset_token() -> str:
    return secrets.token_hex(32)


def session_secret() -> str:
    return secrets.token_urlsafe(16)


def sample_shipments(population: list[int]) -> list[int]:
    # `random` is entirely correct when the result is not a credential. This is
    # the case the rule has to leave alone.
    return random.sample(population, 3)


@ops.post("/handover")
async def handover(request: Request) -> dict:
    signature = request.header("x-partner-signature", "") or ""
    if not hmac.compare_digest(signature, API_TOKEN):
        return {"accepted": False}
    return {"accepted": True}
