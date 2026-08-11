"""Ridgeline Atlas — hut booking for a mountain-shelter association.

The application object, its lifespan and its middleware. The lifespan is the
shape that does *not* split at the yield: the tile cache is opened on the way
up and closed on the way down, so the name crosses the boundary between what
would become ``on_startup`` and what would become ``on_shutdown``.
"""
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from ridgeline_core.tiles import TileCache

from .huts import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    tiles = TileCache(root="/var/cache/ridgeline")
    await tiles.warm()
    yield
    await tiles.close()


class RequestTimerMiddleware(BaseHTTPMiddleware):
    """Stamps every response with how long the handler took."""

    async def dispatch(self, request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["x-elapsed-ms"] = f"{elapsed:.1f}"
        return response


app = FastAPI(title="Ridgeline Atlas", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(RequestTimerMiddleware)
app.include_router(router)
