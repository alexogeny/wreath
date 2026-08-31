from __future__ import annotations

import builtins
from typing import Any

import pytest

from wreath import Wreath


class _Recorder:
    """A database/client double that records its lifecycle, and can refuse.

    `stop`/`close` are separate names because `Wreath` calls `database.stop()`
    and `client.close()`; one class covers both so a test reads as a list of
    resources rather than two parallel hierarchies.
    """

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        if self.fail:
            raise RuntimeError(f"{self.name} refused to stop")

    async def close(self) -> None:
        await self.stop()


async def _drive(app: Wreath, *types: str) -> list[dict[str, Any]]:
    """Run one lifespan cycle over the given inbound message types."""
    messages = iter([{"type": t} for t in types])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    return sent


@pytest.mark.asyncio
async def test_a_refusing_database_does_not_strand_the_startup_failure_reply() -> None:
    # Teardown runs in reverse, so `second` is stopped first and raises. Before
    # the fix that exception escaped `_lifespan` entirely: `first` was never
    # stopped and the server never received `lifespan.startup.failed`, so it sat
    # waiting on a lifespan message that would never arrive.
    app = Wreath()
    first = _Recorder("first")
    second = _Recorder("second", fail=True)
    app._databases = {"first": first, "second": second}

    @app.on_startup
    async def boom(application: Wreath) -> None:
        raise RuntimeError("no schema")

    sent = await _drive(app, "lifespan.startup")

    assert sent[0]["type"] == "lifespan.startup.failed"
    # The reply carries the *original* failure, not the teardown's.
    assert "no schema" in sent[0]["message"]
    assert first.stopped, "a database behind the refusing one was never stopped"
    assert second.stopped


@pytest.mark.asyncio
async def test_a_refusing_client_does_not_strand_the_databases_behind_it() -> None:
    app = Wreath()
    database = _Recorder("db")
    client = _Recorder("client", fail=True)
    app._databases = {"db": database}
    app._http_clients = {"client": client}

    @app.on_startup
    async def boom(application: Wreath) -> None:
        raise RuntimeError("startup handler failed")

    sent = await _drive(app, "lifespan.startup")

    assert sent[0]["type"] == "lifespan.startup.failed"
    assert client.stopped
    assert database.stopped, "the database was leaked by a refusing HTTP client"


@pytest.mark.asyncio
async def test_a_failing_shutdown_handler_still_releases_every_resource() -> None:
    # A shutdown handler that raises used to skip the whole teardown block, so a
    # single bad handler leaked every pool the app owned.
    app = Wreath()
    database = _Recorder("db")
    client = _Recorder("client")
    app._databases = {"db": database}
    app._http_clients = {"client": client}

    @app.on_shutdown
    async def boom(application: Wreath) -> None:
        raise RuntimeError("handler failed")

    sent = await _drive(app, "lifespan.startup", "lifespan.shutdown")

    assert sent[0]["type"] == "lifespan.startup.complete"
    assert sent[1]["type"] == "lifespan.shutdown.failed"
    assert "handler failed" in sent[1]["message"]
    assert database.stopped, "a failing shutdown handler leaked the database"
    assert client.stopped, "a failing shutdown handler leaked the HTTP client"


@pytest.mark.asyncio
async def test_a_refusing_database_on_shutdown_does_not_strand_the_reply() -> None:
    app = Wreath()
    first = _Recorder("first")
    second = _Recorder("second", fail=True)
    app._databases = {"first": first, "second": second}

    sent = await _drive(app, "lifespan.startup", "lifespan.shutdown")

    assert sent[1]["type"] == "lifespan.shutdown.failed"
    assert "second refused to stop" in sent[1]["message"]
    assert first.stopped


@pytest.mark.asyncio
async def test_a_failed_teardown_step_is_counted_and_not_silent() -> None:
    # The counter is the observable record. Without it a teardown failure on the
    # startup path leaves no trace at all: the reply carries the original error,
    # so the close that refused would otherwise be invisible.
    app = Wreath()
    assert app.lifespan_teardown_errors == 0
    app._databases = {"one": _Recorder("one", fail=True)}

    @app.on_startup
    async def boom(application: Wreath) -> None:
        raise RuntimeError("nope")

    await _drive(app, "lifespan.startup")

    assert app.lifespan_teardown_errors == 1


@pytest.mark.asyncio
async def test_a_clean_lifespan_counts_no_teardown_errors() -> None:
    # The counter must have something to count: a healthy app must leave it at
    # zero, or "it moved" would prove nothing.
    app = Wreath()
    database = _Recorder("db")
    app._databases = {"db": database}

    sent = await _drive(app, "lifespan.startup", "lifespan.shutdown")

    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert app.lifespan_teardown_errors == 0
    assert database.stopped


@pytest.mark.asyncio
async def test_an_app_without_orm_registries_does_not_load_orm_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = False
    original_import = builtins.__import__

    def track_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        nonlocal imported
        if name == "orm.introspection" and level == 1:
            imported = True
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", track_import)

    await _drive(Wreath(), "lifespan.startup", "lifespan.shutdown")

    assert imported is False


@pytest.mark.asyncio
async def test_lifespan_resolves_and_validates_each_orm_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath.orm import introspection

    registry = object()
    calls: list[tuple[str, object]] = []

    async def resolve(candidate: object) -> None:
        calls.append(("resolve", candidate))

    async def validate(candidate: object) -> None:
        calls.append(("validate", candidate))

    monkeypatch.setattr(introspection, "resolve_extension_types", resolve)
    monkeypatch.setattr(introspection, "validate_registry", validate)
    app = Wreath()
    app._orm_registries = {"main": registry}

    await _drive(app, "lifespan.startup", "lifespan.shutdown")

    assert calls == [("resolve", registry), ("validate", registry)]


@pytest.mark.asyncio
async def test_an_app_without_services_does_not_start_a_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.services as services

    constructed = 0

    class Supervisor:
        def __init__(self) -> None:
            nonlocal constructed
            constructed += 1

        def add(self, service: object) -> None:
            raise AssertionError("an empty app added a service")

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(services, "Supervisor", Supervisor)

    await _drive(Wreath(), "lifespan.startup", "lifespan.shutdown")

    assert constructed == 0


@pytest.mark.asyncio
async def test_lifespan_starts_registered_services_under_the_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.services as services

    service = object()
    calls: list[tuple[str, object | None]] = []

    class Supervisor:
        def add(self, candidate: object) -> None:
            calls.append(("add", candidate))

        async def start(self) -> None:
            calls.append(("start", None))

        async def stop(self) -> None:
            calls.append(("stop", None))

    monkeypatch.setattr(services, "Supervisor", Supervisor)
    app = Wreath()
    app._services = {"test": service}

    await _drive(app, "lifespan.startup", "lifespan.shutdown")

    assert calls == [("add", service), ("start", None), ("stop", None)]
