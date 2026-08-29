from __future__ import annotations

import pytest

from wreath._replay_adapters import (
    DatabaseDouble,
    FaultyHttpClient,
    ObjectStoreDouble,
    ReplayAdapters,
    installed_adapters,
    installed_boundaries,
)


class _Scope:
    """Anything with the registries an app has. Deliberately not a `Wreath`."""

    def __init__(self) -> None:
        self._databases = {"main": "real-database"}
        self._http_clients = {"api": "real-client"}
        self._object_stores = {"objects": "real-store"}
        self._dirty = False


class _Holder:
    """A boundary held on an attribute, the way `JobRunner` holds `_db`."""

    def __init__(self) -> None:
        self._db = "real-database"


def _adapters() -> ReplayAdapters:
    return ReplayAdapters(
        databases={"main": DatabaseDouble("main")},
        clients={"api": FaultyHttpClient("api")},
        object_stores={"objects": ObjectStoreDouble("objects")},
    )


def test_installed_adapters_is_the_request_spelling_of_one_installer():
    scope = _Scope()
    adapters = _adapters()
    with installed_adapters(scope, adapters):
        assert scope._databases["main"] is adapters.databases["main"]
    assert scope._databases["main"] == "real-database"


def test_a_scope_that_is_not_an_app_still_gets_its_doubles():
    scope = _Scope()
    adapters = _adapters()
    with installed_boundaries(scope, adapters):
        assert scope._databases["main"] is adapters.databases["main"]
        assert scope._http_clients["api"] is adapters.clients["api"]
        assert scope._object_stores["objects"] is adapters.object_stores["objects"]
        assert scope._dirty is True
        # Cleared here so the assertion after the block is about the *restore*
        # marking the binder dirty, not about the entry that already had.
        # A route compiled against the doubles must not survive them.
        scope._dirty = False
    assert scope._dirty is True
    assert scope._databases["main"] == "real-database"
    assert scope._http_clients["api"] == "real-client"
    assert scope._object_stores["objects"] == "real-store"


def test_a_boundary_held_on_an_attribute_is_swapped_and_restored():
    holder = _Holder()
    adapters = _adapters()
    with installed_boundaries(None, adapters, slots=((holder, "_db", "main"),)):
        assert holder._db is adapters.databases["main"]
    assert holder._db == "real-database"


def test_a_slot_naming_an_undoubled_database_is_refused():
    holder = _Holder()
    with pytest.raises(KeyError) as error:
        with installed_boundaries(None, _adapters(), slots=((holder, "_db", "ledger"),)):
            pass  # pragma: no cover - the refusal happens on entry
    message = " ".join(str(error.value).split())
    assert "no database double named 'ledger' for _Holder._db" in message
    assert "pointing at the real resource" in message
    assert holder._db == "real-database"


def test_no_databases_to_double_leaves_the_binder_alone():
    scope = _Scope()
    adapters = ReplayAdapters(clients={"api": FaultyHttpClient("api")})
    with installed_boundaries(scope, adapters):
        assert scope._http_clients["api"] is adapters.clients["api"]
        assert scope._dirty is False
    assert scope._dirty is False


def test_a_scope_with_no_registries_at_all_is_installed_into_harmlessly():
    holder = _Holder()
    with installed_boundaries(holder, _adapters(), slots=((holder, "_db", "main"),)):
        assert holder._db is not None
        assert not isinstance(holder._db, str)
    assert holder._db == "real-database"


def test_a_raising_body_still_restores_every_boundary():
    scope = _Scope()
    holder = _Holder()
    with pytest.raises(RuntimeError, match="the attempt raised"):
        with installed_boundaries(scope, _adapters(), slots=((holder, "_db", "main"),)):
            raise RuntimeError("the attempt raised")
    assert scope._databases["main"] == "real-database"
    assert scope._http_clients["api"] == "real-client"
    assert scope._object_stores["objects"] == "real-store"
    assert holder._db == "real-database"


def test_no_adapters_installs_nothing_and_leaves_the_slot_alone():
    holder = _Holder()
    scope = _Scope()
    with installed_boundaries(scope, None, slots=((holder, "_db", "main"),)):
        assert holder._db == "real-database"
        assert scope._databases["main"] == "real-database"


class _Trace:
    """The two calls an observer makes, recorded in order."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def note(self, seam, target):
        self.events.append(("note", seam, target))
        return len(self.events) - 1

    def fail(self, index, error_type):
        self.events.append(("fail", index, error_type))


class _RealConnection:
    async def fetch(self, sql, *args):
        if "bad" in sql:
            raise ValueError("no")
        return [sql]

    async def execute(self, sql, *args):
        return "OK"

    def transaction(self):
        return "txn"


class _RealDatabase:
    def __init__(self) -> None:
        self.released = []

    async def acquire(self, workload="read"):
        return _RealConnection()

    async def release(self, workload, connection):
        self.released.append(connection)


class _AppScope:
    def __init__(self) -> None:
        self._databases = {"main": _RealDatabase()}
        self._dirty = False


async def test_an_observer_records_a_crossing_and_changes_nothing_about_it():
    from wreath._replay_adapters import observed_boundaries

    scope, trace = _AppScope(), _Trace()
    real = scope._databases["main"]
    with observed_boundaries(scope, trace):
        # The binder has to recompile against the observers, exactly as it does
        # against the doubles; without it a compiled route holds the real one.
        assert scope._dirty is True
        database = scope._databases["main"]
        connection = await database.acquire("read")
        assert await connection.fetch("SELECT 1") == ["SELECT 1"]
        assert connection.transaction() == "txn"
        await database.release("read", connection)
        # See the installer's twin: cleared so the assertion below is about the
        # restore, not about the entry.
        scope._dirty = False
    assert scope._databases["main"] is real
    assert scope._dirty is True
    # Every real connection was handed back unwrapped, so the pool sees what it
    # lent out rather than a proxy it cannot recognise.
    assert all(isinstance(c, _RealConnection) for c in real.released)
    assert [event[0] for event in trace.events] == ["note", "note", "note", "note"]
    assert [event[1] for event in trace.events] == [0, 1, 5, 2]


async def test_an_observer_records_which_crossing_raised_and_re_raises_it():
    from wreath._replay_adapters import observed_boundaries

    scope, trace = _AppScope(), _Trace()
    with observed_boundaries(scope, trace):
        connection = await scope._databases["main"].acquire("read")
        with pytest.raises(ValueError, match="no"):
            await connection.fetch("SELECT bad")
    assert trace.events[-1] == ("fail", 1, "ValueError")


async def test_a_scope_holding_registries_but_no_binder_is_watched_anyway():
    from wreath._replay_adapters import observed_boundaries

    class Registries:
        def __init__(self):
            self._databases = {"main": _RealDatabase()}

    scope, trace = Registries(), _Trace()
    with observed_boundaries(scope, trace):
        await scope._databases["main"].acquire("read")
    assert not hasattr(scope, "_dirty")
    assert trace.events == [("note", 0, "main")]


async def test_a_scope_with_nothing_to_watch_leaves_the_binder_alone():
    from wreath._replay_adapters import observed_boundaries

    class Bare:
        _dirty = False

    scope = Bare()
    with observed_boundaries(scope, _Trace()):
        assert scope._dirty is False
    assert scope._dirty is False


async def test_every_object_store_operation_is_observed_once():
    from wreath._replay_adapters import observed_boundaries
    from wreath.objects import MemoryObjectStore

    class Objects:
        def __init__(self):
            self._object_stores = {"files": MemoryObjectStore(url_secret=b"s" * 32)}

    async def chunks():
        yield b"stream"

    scope, trace = Objects(), _Trace()
    with observed_boundaries(scope, trace):
        store = scope._object_stores["files"]
        await store.write("one", b"1")
        await store.write_stream("two", chunks())
        await store.read("one")
        assert b"".join([chunk async for chunk in store.read_stream("two")]) == b"stream"
        await store.stat("one")
        assert await store.exists("one")
        assert [item.key async for item in store.list()] == ["one", "two"]
        assert store.url("one").startswith("memory:")
        await store.delete("one")
    assert [event[1] for event in trace.events] == [6] * 9
