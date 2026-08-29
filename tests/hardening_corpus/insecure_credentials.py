from __future__ import annotations

import hashlib
import hmac
import random

from wreath import Request, Router

ops = Router(prefix="/ops")

DEV_SESSION_SECRET = "northwind-dev-secret"  # hardening-expect: hardcoded-secret
API_TOKEN = "sk-live-9c1f4a2b7e0d"  # hardening-expect: hardcoded-secret


def remember_mac(payload: bytes) -> str:
    return hmac.new(DEV_SESSION_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def reset_token(account_id: int) -> str:
    generator = random.Random(account_id)  # hardening-expect: weak-randomness
    return "".join(generator.choice("0123456789abcdef") for _ in range(32))


def session_secret() -> str:
    api_secret = random.randbytes(16).hex()  # hardening-expect: weak-randomness
    return api_secret


async def leaky_compare(given: str, expected: str) -> bool:
    if len(given) != len(expected):
        return False
    for a, b in zip(given, expected, strict=True):
        if a != b:  # hardening-expect: timing-unsafe-compare
            return False
    return True


@ops.post("/handover")
async def handover(request: Request) -> dict:
    signature = request.header("x-partner-signature", "") or ""
    # Deliberately unmarked. `timing-unsafe-compare` reports a plain `==` only
    # when a second signal gives the comparison an authentication role -- an
    # `expected`/`given` pairing, or two operands of differing provenance --
    # because "signature" alone matched a route signature and a plan digest in
    # wreath's own tree. This line is a real defect that the rule does not
    # claim to find; the loop above is the shape the range actually plants.
    if signature != API_TOKEN:
        return {"accepted": False}
    return {"accepted": True}
