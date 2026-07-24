"""Distributed advisory locking via sqlalchemy-dlock (Postgres advisory locks)."""
from contextlib import asynccontextmanager

from sqlalchemy_dlock.asyncio import create_async_sadlock

from tumbleweed_core import base_ormar_config


@asynccontextmanager
async def ranch_lock(key: str):
    async with base_ormar_config.database.connection() as conn:
        async with create_async_sadlock(conn, key) as lock:
            yield lock
