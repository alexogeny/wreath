"""Tumbleweed Trails API — app assembly, lifespan, middleware, routers.

Exercises: an ``@asynccontextmanager`` lifespan (startup task + shutdown around
``yield``), ``add_middleware`` (CORS class+kwargs form, TrustedHost, and a
custom subclass), a DYNAMIC ``include_router`` loop (unsupported-dynamic case),
a GraphQL mount, an ``@app.exception_handler``, and per-env ``docs_url``.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from strawberry.asgi import GraphQL

from tumbleweed_core import base_ormar_config, sentry_init

from .background import trek_reconciler
from .graphql import schema
from .middleware import TrailStateMiddleware
from .routers import bookings, llamas, ranches
from .settings import get_settings

sentry_init()
settings = get_settings()

_ROUTERS = {
    "bookings": bookings.router,
    "llamas": llamas.router,
    "ranches": ranches.router,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await base_ormar_config.database.connect()
    stop = asyncio.Event()
    task = asyncio.create_task(trek_reconciler(stop))
    app.state.stop = stop
    yield
    stop.set()
    task.cancel()
    await base_ormar_config.database.disconnect()


app = FastAPI(
    title="Tumbleweed Trails",
    lifespan=lifespan,
    docs_url=None if settings.environment == "production" else "/docs",
)

app.add_middleware(TrailStateMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.tumbleweed.example"],
    allow_credentials=True,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

for _name, _router in _ROUTERS.items():
    app.include_router(_router)

app.add_route("/graphql", GraphQL(schema))


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/health")
async def health():
    return {"status": "ok"}
