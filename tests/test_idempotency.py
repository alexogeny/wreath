from __future__ import annotations

import pytest
from _pgfidelity import check_for

from wreath.policy import IdempotencyPolicy
from wreath.request import Request
from wreath.response import Response

pytestmark = pytest.mark.asyncio


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request(
    method="POST",
    path="/orders",
    key: str | None = "k1",
    principal: str | None = "alice",
    principal_type: str = "User",
) -> Request:
    """A request, authenticated as ``principal`` unless it is ``None``.

    Authenticated by default because idempotency only applies to an
    authenticated principal -- see
    `test_anonymous_requests_are_not_guarded_and_never_replay_each_other`.
    """
    from wreath._auth.models import Identity

    headers = [(b"host", b"x")]
    if key is not None:
        headers.append((b"idempotency-key", key.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
    }
    request = Request(scope, _receive)
    if principal is not None:
        request._set_identity(Identity(id=principal, type=principal_type, roles=frozenset()))
    return request


async def test_first_call_passes_through_then_replays() -> None:
    mw = IdempotencyPolicy()

    first = _request()
    assert await mw.action(first) is None  # not seen -> proceed
    await mw.after(first, Response(b"created", status=201))

    second = _request()  # same key
    replay = await mw.action(second)
    assert replay is not None
    assert replay.status == 201 and replay.body == b"created"
    assert (b"idempotency-replayed", b"true") in replay.headers


async def test_concurrent_duplicate_gets_409() -> None:
    mw = IdempotencyPolicy()
    first = _request()
    assert await mw.action(first) is None  # reserves the key (in-flight)
    # A second identical request arrives before the first's `after` runs.
    conflict = await mw.action(_request())
    assert conflict is not None and conflict.status == 409


async def test_5xx_is_not_cached_and_stays_retryable() -> None:
    mw = IdempotencyPolicy()
    first = _request()
    await mw.action(first)
    await mw.after(first, Response(b"boom", status=500))
    # The key was released, so a retry proceeds instead of replaying the 500.
    assert await mw.action(_request()) is None


async def test_safe_method_and_missing_key_are_ignored() -> None:
    mw = IdempotencyPolicy()
    assert await mw.action(_request(method="GET")) is None
    assert await mw.action(_request(key=None)) is None
    # Neither reserved a key, so `after` is a passthrough.
    resp = Response(b"x")
    assert await mw.after(_request(key=None), resp) is resp


async def test_key_is_scoped_by_principal() -> None:
    mw = IdempotencyPolicy()
    alice = _request(principal="alice")
    await mw.action(alice)
    await mw.after(alice, Response(b"alice-order", status=201))

    bob = _request(principal="bob")  # same key value, different user
    # Bob must NOT get alice's stored response.
    assert await mw.action(bob) is None


async def test_scope_components_cannot_shift_across_principals() -> None:
    mw = IdempotencyPolicy()
    victim = _request(path="/resource/a b", principal="c")
    await mw.action(victim)
    await mw.after(victim, Response(b"victim-secret", status=201))

    attacker = _request(path="/resource/a", principal="b c")
    assert await mw.action(attacker) is None


async def test_same_id_in_different_principal_types_has_a_distinct_scope() -> None:
    mw = IdempotencyPolicy()
    user = _request(principal="42", principal_type="User")
    await mw.action(user)
    await mw.after(user, Response(b"user-secret", status=201))

    service = _request(principal="42", principal_type="Service")
    assert await mw.action(service) is None


async def test_anonymous_requests_are_not_guarded_and_never_replay_each_other() -> None:
    mw = IdempotencyPolicy()

    first = _request(path="/signup", principal=None)
    assert await mw.action(first) is None
    await mw.after(first, Response(b'{"token":"alice-secret"}', status=201))

    second = _request(path="/signup", principal=None)  # same key, other caller
    assert await mw.action(second) is None  # reaches the handler...
    # ... and nothing of the first caller's response came back.
    assert not hasattr(second.state, "idempotency_key")

    # Not even a concurrent duplicate is claimed, so no anonymous caller can
    # take a key that locks another out with a 409.
    third = _request(path="/signup", principal=None)
    assert await mw.action(third) is None

    # And `after` stays a passthrough: nothing anonymous is ever stored.
    response = Response(b'{"token":"bob-secret"}', status=201)
    assert await mw.after(second, response) is response
    assert await mw.action(_request(path="/signup", principal=None)) is None


# The in-process store only covers retries that land on the worker that served
# the original. A shared store covers the rest, which is the difference between
# "usually replays" and a guarantee. These pin the store contract against a fake
# connection; the SQL itself is only meaningful against a real server.


async def test_a_shared_store_replays_across_workers() -> None:
    from wreath.policy import MemoryIdempotencyStore

    store = MemoryIdempotencyStore()
    worker_a = IdempotencyPolicy(store=store)
    worker_b = IdempotencyPolicy(store=store)

    first = _request()
    assert await worker_a.action(first) is None
    await worker_a.after(first, Response(b"created", status=201))

    replay = await worker_b.action(_request())
    assert replay is not None and replay.body == b"created"


async def test_the_memory_store_reclaims_an_expired_key() -> None:
    import asyncio

    from wreath.policy import MemoryIdempotencyStore

    store = MemoryIdempotencyStore(ttl=0.02)
    assert await store.reserve("k") == ("fresh", None)
    await store.store("k", (201, (), b"x"))
    assert (await store.reserve("k"))[0] == "done"

    await asyncio.sleep(0.03)
    assert await store.reserve("k") == ("fresh", None)


async def test_the_memory_store_measures_the_window_from_the_first_attempt() -> None:
    from wreath.policy import MemoryIdempotencyStore
    from wreath.store import MemoryStore

    store = MemoryIdempotencyStore(ttl=10.0)
    clock = [1000.0]
    # A steerable clock, so the assertion is about the semantics and not about
    # how long the test host took to get here.
    store._store = MemoryStore(ttl=10.0, clock=lambda: clock[0])

    assert await store.reserve("k") == ("fresh", None)
    clock[0] += 6.0  # a slow handler
    await store.store("k", (201, (), b"created"))
    assert (await store.reserve("k"))[0] == "done"

    clock[0] += 4.0  # 10s after the first attempt, 4s after the write
    assert await store.reserve("k") == ("fresh", None)


async def test_the_postgres_store_rejects_an_unsafe_table_name() -> None:
    from wreath.policy import PostgresIdempotencyStore

    for bad in ("a; DROP TABLE users", "", "1abc", "sch.ema"):
        with pytest.raises(ValueError, match="plain SQL identifier"):
            PostgresIdempotencyStore(object(), table=bad)


async def test_the_postgres_store_offers_its_schema_as_a_migration() -> None:
    from wreath.policy import PostgresIdempotencyStore

    sql = PostgresIdempotencyStore(object(), table="replays").schema_sql()
    assert "CREATE TABLE IF NOT EXISTS replays" in sql
    assert "key text PRIMARY KEY" in sql
    assert "expires timestamptz NOT NULL" in sql


class _FakeConnection:
    def __init__(self, rows: list) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rows = rows

    async def execute(self, sql: str, *args: object) -> str:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args: object):
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return self.rows.pop(0) if self.rows else None

    async def close(self) -> None:
        return None


async def _pg_store(monkeypatch, rows: list):
    from wreath.policy import PostgresIdempotencyStore
    from wreath.postgres import Database, PoolConfig

    connection = _FakeConnection(rows)

    async def connect(dsn: str) -> _FakeConnection:
        return connection

    monkeypatch.setattr("wreath.postgres.connect", connect)
    database = Database("app", "postgresql://x/y", pools={"write": PoolConfig(min_size=1)})
    store = PostgresIdempotencyStore(database)
    await database.start()
    return store, connection


def _statements(connection: _FakeConnection, prefix: str) -> list:
    return [call for call in connection.calls if call[0].startswith(prefix)]


async def test_the_postgres_store_claims_a_key_in_one_statement(monkeypatch) -> None:
    store, connection = await _pg_store(monkeypatch, [(0,)])  # a row: we won

    assert await store.reserve("k") == ("fresh", None)

    sql, args = _statements(connection, "INSERT INTO")[0]
    assert args[0] == "k"
    assert sql.count(";") == 0
    assert "ON CONFLICT (key) DO UPDATE" in sql
    # A row comes back only when we inserted or reclaimed an expired one, so
    # "a row came back" *is* the claim. No owner column, no second round trip.
    assert "RETURNING" in sql
    # Postgres owns the clock; workers must not disagree about expiry.
    assert "clock_timestamp()" in sql and "now()" not in sql


async def test_the_postgres_store_reports_a_concurrent_duplicate(monkeypatch) -> None:
    # No row from the claim (someone holds it), then a row with a null status.
    store, connection = await _pg_store(monkeypatch, [None, (None, None, None)])
    assert await store.reserve("k") == ("in_flight", None)


async def test_the_postgres_store_returns_a_stored_response(monkeypatch) -> None:
    store, connection = await _pg_store(
        monkeypatch,
        [None, (201, [["content-type", "application/json"]], b'{"id":7}')],
    )
    state, entry = await store.reserve("k")
    assert state == "done"
    status, headers, body = entry
    assert status == 201
    assert headers == ((b"content-type", b"application/json"),)
    assert body == b'{"id":7}'


async def test_the_postgres_store_writes_the_response_without_moving_expiry(
    monkeypatch,
) -> None:
    store, connection = await _pg_store(monkeypatch, [])
    await store.store("k", (201, ((b"x", b"y"),), b"body"))

    sql, args = _statements(connection, "INSERT INTO")[-1]
    assert "ON CONFLICT (key) DO UPDATE" in sql
    assert "expires = " not in sql.split("DO UPDATE")[1]
    assert args[0] == "k" and args[1] == 201


async def test_the_postgres_store_releases_a_key(monkeypatch) -> None:
    store, connection = await _pg_store(monkeypatch, [])
    await store.release("k")
    sql, args = connection.calls[-1]
    assert sql.startswith("DELETE FROM") and args == ("k",)


async def test_the_postgres_store_purges_expired_replays(monkeypatch) -> None:
    store, connection = await _pg_store(monkeypatch, [])
    await store.purge()
    sql, _args = connection.calls[-1]
    assert sql.startswith("DELETE FROM") and "expires < clock_timestamp()" in sql
