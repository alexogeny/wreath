"""Driftwood Gateway — app assembly + provider/sighting routers.

Exercises: a class-based ``Depends(AuthorizeWithActions([...]))``, module-level
``HTTPException`` constants, ``response_model=``, path/query/body params, an
``@app.exception_handler``, and a webhook route reading the raw request body.
"""
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler

from .auth import Action, AuthorizeWithActions
from .providers import collar_by_serial, providers_for_ranch, verify_webhook
from .schemas import ProviderCreate, ProviderOut, SightingIn

app = FastAPI(title="Driftwood Gateway")
router = APIRouter(prefix="/providers", tags=["providers"])

NOT_FOUND = HTTPException(status_code=404, detail="provider not found")


@router.get("", response_model=list)
async def list_providers(
    request: Request,
    ranch_id: str,
    claims=Depends(AuthorizeWithActions([Action.READ])),
):
    return await providers_for_ranch(ranch_id)


@router.post("", status_code=201, response_model=ProviderOut)
async def create_provider(
    payload: ProviderCreate,
    request: Request,
    claims=Depends(AuthorizeWithActions([Action.WRITE])),
):
    return {"id": "new", "name": payload.name, "kind": payload.kind, "endpoint": payload.endpoint}


@router.post("/sightings", status_code=202)
async def ingest_sighting(payload: SightingIn, request: Request):
    collar = await collar_by_serial(payload.collar_serial)
    if collar is None:
        raise NOT_FOUND
    return {"accepted": True}


@router.post("/webhook/{provider_id}")
async def provider_webhook(provider_id: str, request: Request):
    body = await request.body()
    verify_webhook("shared-secret", body, request.headers.get("x-signature", ""))
    return {"ok": True}


app.include_router(router)


@app.exception_handler(HTTPException)
async def on_http_exception(request: Request, exc: HTTPException):
    return await http_exception_handler(request, exc)
