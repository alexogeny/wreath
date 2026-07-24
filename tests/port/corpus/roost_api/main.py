"""Roost API — the alpaca-boarding sub-service.

Uses the real-world ``create_app()`` factory idiom: a kwargs dict with
per-environment docs gating (docs/redoc/openapi disabled in production), an HTML
landing route (``response_class=HTMLResponse``, ``include_in_schema=False``), and
a delegated exception handler.
"""
import os
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse

from .auth import decode_session
from .tasks import send_boarding_reminder

ENV = os.getenv("ENV", "develop")

router = APIRouter(prefix="/boarding", tags=["boarding"])


@router.get("/alpacas")
async def list_alpacas(request: Request, session=Depends(decode_session)):
    return []


@router.post("/alpacas/{alpaca_id}/remind", status_code=202)
async def remind(alpaca_id: str):
    send_boarding_reminder.delay(alpaca_id)
    return {"queued": True}


def create_app() -> FastAPI:
    kwargs: dict[str, Any] = {"title": "Roost"}
    if ENV == "production":
        kwargs["docs_url"] = None
        kwargs["redoc_url"] = None
        kwargs["openapi_url"] = None
    app = FastAPI(**kwargs)
    app.include_router(router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home():
        return "<html><head><title>Roost</title></head><body><h1>Roost</h1></body></html>"

    @app.exception_handler(HTTPException)
    async def on_http_exception(request: Request, exc: HTTPException) -> Response:
        return await http_exception_handler(request, exc)

    return app


app = create_app()
