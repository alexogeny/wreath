"""The buoy vendor signs its callbacks, and this is how the signature is read.

The comparison is constant-time and the digest is right. What is missing is
everything around it: nothing here looks at the timestamp, and nothing
remembers an envelope it has already accepted, so a captured request replays
forever.
"""
import hashlib
import hmac
import os

from fastapi import APIRouter, Header, HTTPException, Request

router = APIRouter(prefix="/hooks", tags=["hooks"])

SIGNING_SECRET = os.environ["KELPBED_WEBHOOK_SECRET"].encode()


@router.post("/buoy")
async def buoy_callback(request: Request, x_buoy_signature: str = Header(...)):
    body = await request.body()
    expected = hmac.new(SIGNING_SECRET, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_buoy_signature):
        raise HTTPException(status_code=401, detail="signature does not match")
    return {"accepted": True}
