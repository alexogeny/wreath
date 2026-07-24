"""Structural check that user_router wires the expected lifecycle routes.

Needs the built wreath package (imports the router/binding/response glue).
"""
from __future__ import annotations

import pytest

from wreath.users import InMemoryUserStore, OrmUserStore, default_user_model, user_router


def _routes(router):
    return {(r.path, m) for r in router.routes for m in r.methods}


def test_user_router_exposes_lifecycle_routes():
    router = user_router(InMemoryUserStore(), secret="s", base_url="https://app")
    routes = _routes(router)
    assert ("/users/register", "POST") in routes
    assert ("/users/login", "POST") in routes
    assert ("/users/logout", "POST") in routes
    assert ("/users/verify", "POST") in routes
    assert ("/users/verify/{token}", "GET") in routes
    assert ("/users/forgot-password", "POST") in routes
    assert ("/users/reset-password", "POST") in routes
    assert ("/users/me", "GET") in routes
    assert ("users",) == router.routes[0].tags


def test_user_router_requires_secret():
    with pytest.raises(ValueError):
        user_router(InMemoryUserStore(), secret="")


def test_custom_prefix():
    router = user_router(InMemoryUserStore(), secret="s", prefix="/accounts")
    assert ("/accounts/register", "POST") in _routes(router)


async def test_ormuserstore_write_path_uses_unit_of_work():
    """create -> add()+flush(); update -> flush() on the loaded row (no session.update)."""
    Model = default_user_model()
    inst = Model(email="seed@x.co", hashed_password="h")
    assert inst.id is not None  # uuid default applies on instantiation

    class FakeSession:
        def __init__(self):
            self.added, self.flushes, self._row = [], 0, inst

        def add(self, i):
            self.added.append(i)

        async def flush(self):
            self.flushes += 1

        async def get(self, model, pk):
            return self._row

    s = FakeSession()
    store = OrmUserStore(s, Model)
    rec = await store.create("A@B.co", "hash")
    assert len(s.added) == 1 and s.flushes == 1 and rec.email == "a@b.co"

    from wreath.users import UserRecord

    await store.update(UserRecord(str(inst.id), "new@b.co", "h2", True, True))
    assert s.flushes == 2 and inst.email == "new@b.co"
