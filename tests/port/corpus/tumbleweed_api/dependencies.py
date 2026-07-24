"""FastAPI dependencies (Depends).

Exercises: a dependency that returns a list (router-level), a ``yield``-cleanup
dependency, a DB-session dependency, and a plain pagination dependency.
"""
from typing import AsyncIterator

from fastapi import Depends, Request

from tumbleweed_core import base_ormar_config

from .auth import authenticate


def get_depends():
    return [Depends(authenticate)]


async def db_session() -> AsyncIterator[None]:
    async with base_ormar_config.database:
        yield


async def current_wrangler(request: Request):
    return request.state.wrangler


def pagination(limit: int = 20, offset: int = 0):
    return {"limit": limit, "offset": offset}
