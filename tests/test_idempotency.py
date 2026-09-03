from __future__ import annotations

import pytest
from _pgfidelity import check_for

from wreath._compression import _RenderedFragments
from wreath.policy import IdempotencyPolicy
from wreath.request import Request
from wreath.response import Response

pytestmark = pytest.mark.asyncio


def test_idempotency_refuses_a_negative_response_body_ceiling() -> None:
    with pytest.raises(ValueError, match="max_body_bytes must be non-negative"):
        IdempotencyPolicy(max_body_bytes=-1)


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


class _StoreStub:
    def __init__(self, reservation: tuple[str, object | None] = ("fresh", None)) -> None:
        self.reservation = reservation
        self.releases: list[str] = []
        self.stores: list[tuple[str, object]] = []

    async def reserve(self, key: str):
        return self.reservation

    async def store(self, key: str, replay: object) -> None:
        self.stores.append((key, replay))

    async def release(self, key: str) -> None:
        self.releases.append(key)


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


async def test_idempotency_scope_distinguishes_raw_targets_a_proxy_forwards_differently() -> None:
    policy = IdempotencyPolicy()
    canonical = _request(path="/files/report")
    encoded = _request(path="/files/report")
    encoded.scope["raw_path"] = b"/files/%72eport"

    assert policy._key(canonical) != policy._key(encoded)


async def test_fragment_response_replay_keeps_the_complete_body() -> None:
    mw = IdempotencyPolicy()
    prefix = b"dynamic-prefix"
    tail = b"stable-tail" * 128

    first = _request()
    assert await mw.action(first) is None
    await mw.after(first, Response(_RenderedFragments(prefix, tail), status=201))

    replay = await mw.action(_request())

    assert replay is not None
    assert replay.body == prefix + tail
    assert bytes(replay.body) == prefix + tail
    assert dict(replay.headers)[b"content-length"] == str(len(prefix + tail)).encode()


async def test_concurrent_duplicate_gets_409() -> None:
    mw = IdempotencyPolicy()
    first = _request()
    assert await mw.action(first) is None  # reserves the key (in-flight)
    # A second identical request arrives before the first's `after` runs.
    conflict = await mw.action(_request())
    assert conflict is not None and conflict.status == 409


async def test_capacity_refusal_returns_503_without_running_the_handler() -> None:
    store = _StoreStub(("full", None))
    policy = IdempotencyPolicy(store=store)
    request = _request()

    response = await policy.action(request)

    assert response is not None and response.status == 503
    assert dict(response.headers)[b"retry-after"] == b"1"
    assert request.state.get("idempotency_key") is None


async def test_5xx_is_not_cached_and_stays_retryable() -> None:
    mw = IdempotencyPolicy()
    first = _request()
    await mw.action(first)
    await mw.after(first, Response(b"boom", status=500))
    # The key was released, so a retry proceeds instead of replaying the 500.
    assert await mw.action(_request()) is None


async def test_safe_method_and_missing_key_are_ignored() -> None:
    store = _StoreStub()
    mw = IdempotencyPolicy(store=store)
    safe = _request(method="GET")
    missing = _request(key=None)
    assert await mw.action(safe) is None
    assert await mw.action(missing) is None
    assert safe.state.get("idempotency_key") is None
    assert safe.state.get("idempotency_ignored") is None
    assert missing.state.get("idempotency_ignored") is None
    resp = Response(b"x")
    assert await mw.after(missing, resp) is resp
    assert store.stores == []


async def test_an_ignored_key_is_reported_only_for_an_unsafe_authenticated_method() -> None:
    mw = IdempotencyPolicy()
    anonymous = _request(principal=None)
    assert await mw.action(anonymous) is None
    response = Response(b"x")
    assert await mw.after(anonymous, response) is response
    assert (b"idempotency-ignored", b"unauthenticated") in response.headers
    assert mw.ignored == 1


async def test_an_ignored_key_tolerates_a_response_without_headers() -> None:
    mw = IdempotencyPolicy()
    request = _request(principal=None)
    assert await mw.action(request) is None
    response = object()
    assert await mw.after(request, response) is response


async def test_after_without_a_claim_does_not_inspect_or_store_the_response() -> None:
    store = _StoreStub()
    mw = IdempotencyPolicy(store=store)
    request = _request(key=None)
    response = object()
    assert await mw.after(request, response) is response
    assert store.releases == []
    assert store.stores == []


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


@pytest.mark.parametrize(
    "reservation",
    [
        ("done", None),
        ("fresh", (201, ((b"x-result", b"stored"),), b"wrong")),
    ],
)
async def test_only_a_complete_done_reservation_is_replayed(reservation) -> None:
    store = _StoreStub(reservation)
    mw = IdempotencyPolicy(store=store)
    request = _request()
    assert await mw.action(request) is None
    assert request.state.get("idempotency_key") is not None


async def test_streaming_response_releases_the_claim() -> None:
    from wreath.response import StreamingResponse

    store = _StoreStub()
    mw = IdempotencyPolicy(store=store)
    request = _request()
    assert await mw.action(request) is None

    async def chunks():
        yield b"created"

    response = StreamingResponse(chunks(), status=201)
    assert await mw.after(request, response) is response
    assert len(store.releases) == 1
    assert store.stores == []


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


async def test_the_memory_store_refuses_capacity_without_evicting_live_replays() -> None:
    from wreath.policy import MemoryIdempotencyStore

    store = MemoryIdempotencyStore(max_entries=2)
    assert await store.reserve("first") == ("fresh", None)
    await store.store("first", (201, (), b"first"))
    assert await store.reserve("second") == ("fresh", None)
    await store.store("second", (201, (), b"second"))

    assert await store.reserve("attacker") == ("full", None)
    assert await store.reserve("first") == ("done", (201, (), b"first"))
    assert await store.reserve("second") == ("done", (201, (), b"second"))


async def test_the_memory_store_measures_the_window_from_the_first_attempt() -> None:
    from wreath._capability_map import CapabilityMap
    from wreath.policy import MemoryIdempotencyStore

    store = MemoryIdempotencyStore(ttl=10.0)
    clock = [1000.0]
    # A steerable clock, so the assertion is about the semantics and not about
    # how long the test host took to get here.
    store._store = CapabilityMap(
        ttl=10.0,
        max_entries=4096,
        clock=lambda: clock[0],
        overflow="refuse",
    )

    assert await store.reserve("k") == ("fresh", None)
    clock[0] += 6.0  # a slow handler
    await store.store("k", (201, (), b"created"))
    assert (await store.reserve("k"))[0] == "done"

    clock[0] += 4.0  # 10s after the first attempt, 4s after the write
    assert await store.reserve("k") == ("fresh", None)


async def test_the_memory_store_treats_a_lost_claim_as_full() -> None:
    from wreath.policy import MemoryIdempotencyStore

    class LostClaim:
        def claim(self, key: str, value: object) -> bool:
            return False

        def peek(self, key: str):
            return None

    store = MemoryIdempotencyStore()
    store._store = LostClaim()
    assert await store.reserve("lost") == ("full", None)


def test_replayable_headers_drop_names_and_values_containing_nul() -> None:
    from wreath.policy.idempotency import _replayable_headers

    assert _replayable_headers(
        ((b"x-good", b"yes"), (b"x\x00bad", b"no"), (b"x-bad", b"no\x00"))
    ) == ((b"x-good", b"yes"),)


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


async def test_the_postgres_store_decodes_nullable_replay_fields(monkeypatch) -> None:
    store, _connection = await _pg_store(monkeypatch, [None, (204, None, None)])
    assert await store.reserve("k") == ("done", (204, (), b""))


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
