from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.testing import TestClient
from wreath.users import InMemoryUserStore


def _paths(app: Wreath) -> set[tuple[str, str]]:
    return {(r.path, m) for r in app._routes for m in r.methods}


def test_flags_factory_registers_provider() -> None:
    app = Wreath()
    provider = app.flags(beta="on", gamma="off")
    assert app.state.flags is provider
    assert provider.enabled("beta") and not provider.enabled("gamma")


def test_health_factory_mounts_routes() -> None:
    app = Wreath()
    app.health()
    assert ("/health", "GET") in _paths(app)
    assert ("/ready", "GET") in _paths(app)


@pytest.mark.asyncio
async def test_health_liveness_serves_200() -> None:
    app = Wreath()
    app.health()
    async with TestClient(app) as client:
        assert (await client.get("/health")).status == 200


def test_metrics_factory_mounts_scrape_route() -> None:
    class _Source:
        def snapshot(self) -> Any:
            return None

    app = Wreath()
    app.metrics(_Source())
    assert ("/metrics", "GET") in _paths(app)


def test_users_factory_mounts_lifecycle() -> None:
    app = Wreath()
    app.users(InMemoryUserStore(), secret="s" * 32, base_url="https://app")
    paths = _paths(app)
    assert ("/users/register", "POST") in paths
    assert ("/users/login", "POST") in paths
    assert ("/users/me", "GET") in paths
