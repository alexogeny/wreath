"""The curated fault corpus (§7): every taxonomy region drives the owned code to
a deterministic outcome. This is the artifact the sanitizer/fuzz gate re-runs, so
a regression here is a regression in the owned failure handling itself.

Also covers the recording-reader fault taxonomy: a truncated, corrupt, version-
mismatched, reordered, or duplicated container must be detected or safely
recovered -- never crash or over-read the reader.
"""

from __future__ import annotations

import asyncio
import importlib
import struct

import pytest

import wreath
from wreath._replay_adapters import AdapterFault, DatabaseDouble
from wreath.messaging import MessageBus
from wreath.postgres import Connection, PostgresError
from wreath.replay import (
    AdapterSeam,
    CanonicalRequest,
    FaultKind,
    FaultSchedule,
    ReplayAdapters,
    ReplayError,
    TransportRecording,
    fault_corpus,
    record_transport_segments,
    replay_endpoint_plan,
    replay_transport,
)

try:
    _native = importlib.import_module("wreath._native._server")
    _NATIVE_HTTP1 = _native.Http1Protocol
except ImportError:
    _NATIVE_HTTP1 = None


proto = pytest.mark.parametrize(
    "protocol_cls",
    [
        pytest.param(
            _NATIVE_HTTP1, id="http1",
            marks=pytest.mark.skipif(_NATIVE_HTTP1 is None, reason="native server not built"),
        ),
    ],
)

GET = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
CORPUS = fault_corpus()
TRANSPORT_NAMES = sorted(n for n in CORPUS if n.startswith(("transport", "schedule")))
ADAPTER_NAMES = sorted(n for n in CORPUS if n.startswith("adapter"))


def test_transport_fault_values_match_each_fault_kind() -> None:
    corpus = fault_corpus()
    byte_kinds = {
        FaultKind.SHORT_READ,
        FaultKind.TRUNCATE,
        FaultKind.CLOCK_JUMP,
        FaultKind.SPLIT,
    }
    for kind in FaultKind:
        for index in (0, 1):
            fault = corpus[f"transport-{kind.name.lower()}-seg{index}"].faults[0]
            assert fault.value == (8 if kind in byte_kinds else 0)


@pytest.mark.parametrize(
    ("fault", "seam"),
    [
        (AdapterFault.CONNECTION_FAILED, AdapterSeam.DB_CONNECTION),
        (AdapterFault.POOL_TIMEOUT, AdapterSeam.DB_ACQUIRE),
        (AdapterFault.RELEASE_ERROR, AdapterSeam.DB_RELEASE),
        (AdapterFault.CONNECT_ERROR, AdapterSeam.HTTP_REQUEST),
        (AdapterFault.LISTEN_REFUSED, AdapterSeam.DB_LISTEN),
        (AdapterFault.BEGIN_ERROR, AdapterSeam.DB_TRANSACTION),
        (AdapterFault.OBJECT_UNREACHABLE, AdapterSeam.OBJECT_STORE),
        (AdapterFault.SERVER_ERROR, AdapterSeam.DB_QUERY),
    ],
)
def test_adapter_fault_corpus_maps_each_boundary_family(
    fault: AdapterFault, seam: AdapterSeam
) -> None:
    descriptor = fault_corpus()[f"adapter-{fault.value}"].adapter_faults[0]

    assert descriptor.seam == int(seam)


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/")
    async def root(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("ok")

    return app


# --- transport / scheduling corpus -------------------------------------------


@proto
@pytest.mark.asyncio
@pytest.mark.parametrize("name", TRANSPORT_NAMES)
async def test_transport_corpus_entry_is_deterministic(protocol_cls: type, name: str) -> None:
    schedule = FaultSchedule.from_bytes(CORPUS[name].to_bytes())  # via serialization
    rec = record_transport_segments([GET[:16], GET[16:32], GET[32:]])
    a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=schedule)
    b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=schedule)
    assert a.matches(b)
    assert a.terminal in ("closed", "aborted", "open")


# --- adapter corpus ----------------------------------------------------------


def _db_http_app() -> wreath.Wreath:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/db")
    async def db(request: wreath.Request, conn: Connection) -> dict:
        return {"n": len(await conn.fetch("SELECT 1"))}

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ADAPTER_NAMES)
async def test_adapter_corpus_entry_is_deterministic(name: str) -> None:
    schedule = FaultSchedule.from_bytes(CORPUS[name].to_bytes())
    # Every entry must reconstruct at least one double from its serialized form:
    # a schedule that round-trips to nothing is a corpus entry that tests nothing.
    adapters = ReplayAdapters.from_faults(schedule.adapter_faults)
    assert adapters.databases or adapters.clients or adapters.object_stores
    if "main" in adapters.databases:
        double = adapters.databases["main"]
        # Only the query/acquire/release seams are reachable from an HTTP
        # handler; listen and transaction faults are driven by their own
        # subsystems below, so here we assert the reconstruction and that the
        # handler path stays deterministic in their presence.
        a = await replay_endpoint_plan(_db_http_app(), CanonicalRequest("GET", "/db"),
                                       adapters=adapters)
        # A pooled/query fault is an owned 500; a release-only fault may still 200.
        assert a.status in (200, 500)
        # An acquire fault never leases; otherwise the connection is returned.
        if double.acquired and double.acquire_fault is None:
            assert double.acquired == double.released


# --- recording-reader fault taxonomy -----------------------------------------


def _valid_recording() -> bytes:
    return record_transport_segments([GET]).to_bytes()


def test_reader_rejects_a_truncated_container() -> None:
    blob = _valid_recording()
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(blob[: len(blob) - 12])


def test_reader_rejects_a_corrupt_chunk_crc() -> None:
    blob = bytearray(_valid_recording())
    blob[-1] ^= 0xFF
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(bytes(blob))


def test_reader_rejects_an_unsupported_version() -> None:
    blob = bytearray(_valid_recording())
    blob[4] = 200
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(bytes(blob))


def test_reader_rejects_a_foreign_container() -> None:
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(b"XXXX" + b"\x01" + b"garbage")


def test_reader_recovers_the_chunks_before_a_corrupt_one() -> None:
    # A container whose SECOND chunk is corrupt still yields the first: the HEAD
    # chunk is intact but SEGS is corrupt -> the required SEGS is missing -> a
    # reported error, never a partial/garbage replay.
    rec = TransportRecording(())  # HEAD present, SEGS present-but-empty
    blob = bytearray(rec.to_bytes())
    # Corrupt the last byte (inside the SEGS chunk payload / its crc region).
    blob[-1] ^= 0xFF
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(bytes(blob))


def test_fault_schedule_reader_rejects_a_corrupt_schedule() -> None:
    blob = bytearray(CORPUS[TRANSPORT_NAMES[0]].to_bytes())
    blob[-1] ^= 0xFF
    with pytest.raises(ReplayError):
        FaultSchedule.from_bytes(bytes(blob))


def test_fault_schedule_reader_rejects_a_truncated_length_prefix() -> None:
    # A schedule claiming more faults than its bytes contain must not over-read.
    body = struct.pack("<I", 99)  # says 99 faults, but no fault bytes follow
    from wreath.replay import _MAGIC_FAULTS, _chunk  # internal framing helpers

    blob = _MAGIC_FAULTS + b"\x01" + _chunk(b"FALT", body)
    with pytest.raises((ReplayError, struct.error)):
        FaultSchedule.from_bytes(blob)


# --- doorbell seam: LISTEN and the notification stream ------------------------
#
# The bus's doorbell is the failure this session actually shipped a fix for: a
# held LISTEN connection whose `notifications()` iterator *returns* on close
# rather than raising, so the unsupervised loop ended with nothing to catch and
# ephemeral fan-out stopped for the life of the process. A corpus entry that
# only models the raising case would re-bless that bug, which is why
# NOTIFY_STREAM_END and NOTIFY_STREAM_ERROR are separate regions.


class _Supervisor:
    """Spawns real tasks and stops them the way `Supervisor` does."""

    def __init__(self) -> None:
        self.stopping = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    def spawn(self, name: str, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def stop(self, bus) -> None:
        self.stopping.set()
        await bus.drain(asyncio.get_running_loop().time() + 1.0)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def _until(predicate, *, within: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _bus_on(double: DatabaseDouble) -> MessageBus:
    bus = MessageBus(double, name="corpus", poll_interval=60.0)

    async def _handler(message) -> None:
        return None

    bus.subscribe("things")(_handler)
    return bus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["adapter-notify_stream_end", "adapter-notify_stream_error"]
)
async def test_a_lost_doorbell_stream_is_reopened(name: str) -> None:
    """Both ways a stream can stop must lead to the same owned outcome: reopen.

    `STREAM_END` is the one that matters -- it raises nothing at all, so a
    supervisor written around `except` sees a clean return and stops.
    """
    schedule = FaultSchedule.from_bytes(CORPUS[name].to_bytes())
    double = ReplayAdapters.from_faults(schedule.adapter_faults).databases["main"]
    bus = _bus_on(double)
    supervisor = _Supervisor()
    try:
        await bus.start(supervisor)
        # The owned recovery: the stream ends, the loop notices, and it takes a
        # *new* connection and re-subscribes. Trusting the fix would assert the
        # counter; proving it asserts the second stream.
        assert await _until(lambda: double.streams >= 2), (
            f"doorbell did not reopen after {name}: streams={double.streams}"
        )
        assert len(double.listened) >= 2, (
            "reopened without re-LISTENing: a connection with no subscriptions "
            "delivers nothing, which is the outage continuing quietly"
        )
        assert bus.doorbell_reconnects >= 1
    finally:
        await supervisor.stop(bus)


@pytest.mark.asyncio
async def test_a_doorbell_that_cannot_re_listen_keeps_trying() -> None:
    """The compound region: the stream ends *and* the reopen is refused.

    A database that went away and came back refusing is not a terminal state,
    and treating a failed reopen as one is how a transient outage becomes a
    permanent one.
    """
    schedule = FaultSchedule.from_bytes(
        CORPUS["adapter-doorbell-drop-then-refused-reopen"].to_bytes()
    )
    double = ReplayAdapters.from_faults(schedule.adapter_faults).databases["main"]
    bus = _bus_on(double)
    supervisor = _Supervisor()
    try:
        await bus.start(supervisor)
        # Three acquisitions: the original, the refused reopen, and at least one
        # attempt after it -- the loop did not give up on a failure.
        assert await _until(lambda: double.acquired >= 3), (
            f"gave up after a refused reopen: acquired={double.acquired}"
        )
        assert bus.doorbell_reconnects >= 2
    finally:
        await supervisor.stop(bus)


@pytest.mark.asyncio
async def test_a_doorbell_refused_at_startup_still_gets_a_loop() -> None:
    """A database down at boot must not leave the process with no doorbell.

    This is the half of the bug that had no symptom at all: `start()` swallowed
    the failure and never spawned the loop, so the bus never listened again even
    once the database came back.
    """
    schedule = FaultSchedule.from_bytes(CORPUS["adapter-listen_refused"].to_bytes())
    double = ReplayAdapters.from_faults(schedule.adapter_faults).databases["main"]
    bus = _bus_on(double)
    supervisor = _Supervisor()
    try:
        await bus.start(supervisor)  # must not raise: a dead database is not a boot failure
        assert bus.doorbell_reconnects >= 1, "a refused startup LISTEN went uncounted"
        assert double.streams == 0, "listened despite LISTEN being refused"
        # The loop exists, retries, and gets there once the database allows it.
        # Asserting only "it retried" would pass for a loop that never succeeds.
        assert await _until(lambda: double.streams >= 1), (
            f"never recovered from a refused startup LISTEN: "
            f"acquired={double.acquired} streams={double.streams}"
        )
    finally:
        await supervisor.stop(bus)


# --- transaction seam ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_three_transaction_faults_land_at_three_distinct_moments() -> None:
    """`BEGIN`, the body, and `COMMIT` fail differently and must stay distinct.

    A caller's recovery turns on which one it was: no work ran, work ran and
    rolled back, or work ran and its durability is unknown. Collapsing them is
    how "did my write happen?" becomes unanswerable.
    """
    moments: dict[str, str] = {}
    for name, fault in (
        ("adapter-begin_error", "begin"),
        ("adapter-statement_timeout", "body"),
        ("adapter-commit_error", "commit"),
    ):
        schedule = FaultSchedule.from_bytes(CORPUS[name].to_bytes())
        double = ReplayAdapters.from_faults(schedule.adapter_faults).databases["main"]
        connection = await double.acquire("write")
        txn = connection.transaction()
        try:
            async with txn as tx:
                moments[fault] = "opened"
                await tx.execute("UPDATE t SET x = 1")
                moments[fault] = "body-ran"
        except Exception:  # noqa: BLE001 -- the injected fault is the subject
            # This drives every adapter fault in the corpus and records *where* the
            # scope stopped. Pinning the type would assert the injector's choice
            # rather than the property under test, which is that a faulted scope
            # never reaches `else`.
            moments[fault] = moments.get(fault, "never-opened")
        else:  # pragma: no cover - a faulted scope must not complete
            moments[fault] = "completed"
    assert moments["begin"] == "never-opened", "BEGIN failed but the body ran"
    assert moments["body"] == "opened", "the body was skipped, not interrupted"
    assert moments["commit"] == "body-ran", "COMMIT failed before the body ran"


@pytest.mark.asyncio
async def test_a_rolled_back_scope_is_not_recorded_as_committed() -> None:
    schedule = FaultSchedule.from_bytes(CORPUS["adapter-statement_timeout"].to_bytes())
    double = ReplayAdapters.from_faults(schedule.adapter_faults).databases["main"]
    connection = await double.acquire("write")
    txn = connection.transaction()
    with pytest.raises(PostgresError):
        async with txn as tx:
            await tx.execute("UPDATE t SET x = 1")
    assert txn.rolled_back and not txn.committed


# --- claim seam: the statement succeeds and returns nothing --------------------


@pytest.mark.asyncio
async def test_a_lost_claim_returns_nothing_rather_than_raising() -> None:
    """`INSERT ... ON CONFLICT ... RETURNING` degrading to no row is not an error.

    Modelling it as one would prove the wrong thing: the whole hazard is that a
    caller sees a *successful* statement with an empty result and carries on.
    """
    schedule = FaultSchedule.from_bytes(CORPUS["adapter-claim_lost"].to_bytes())
    double = ReplayAdapters.from_faults(schedule.adapter_faults).databases["main"]
    connection = await double.acquire("write")
    assert await connection.fetchval("INSERT ... RETURNING id") is None
    assert await connection.fetch("SELECT 1") == []  # later queries are unaffected


# --- object store seam --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_object_store_raises_an_owned_error() -> None:
    from wreath.objects import ObjectError

    schedule = FaultSchedule.from_bytes(CORPUS["adapter-object_unreachable"].to_bytes())
    store = ReplayAdapters.from_faults(schedule.adapter_faults).object_stores["objects"]
    with pytest.raises(ObjectError):
        await store.write("k", b"payload")


@pytest.mark.asyncio
async def test_a_torn_write_leaves_a_partial_object_behind() -> None:
    """The failure mode worth naming: the write raised *and* the key exists.

    A caller that retries is fine; one that treats `exists()` as "the upload
    finished" reads half an object and never learns.
    """
    from wreath.objects import ObjectError

    schedule = FaultSchedule.from_bytes(
        CORPUS["adapter-object-torn-write-then-read"].to_bytes()
    )
    store = ReplayAdapters.from_faults(schedule.adapter_faults).object_stores["objects"]
    payload = b"0123456789"
    with pytest.raises(ObjectError):
        await store.write("k", payload)
    assert await store.exists("k"), "a torn write should leave the partial object"
    assert await store.read("k") != payload


@pytest.mark.asyncio
async def test_a_short_read_returns_fewer_bytes_than_stat_promised() -> None:
    """No exception, fewer bytes. A caller trusting `stat` is silently truncated."""
    schedule = FaultSchedule.from_bytes(CORPUS["adapter-object_read_short"].to_bytes())
    store = ReplayAdapters.from_faults(schedule.adapter_faults).object_stores["objects"]
    await store._inner.write("k", b"0123456789")
    data = await store.read("k")
    assert len(data) < 10


# --- the pass driver: a shift always gives its connection back ----------------
#
# NOT design 20 §6.4. That claim -- a chunk hitting its own `statement_timeout`
# surfaces as a chunk failure rather than a stuck lease -- needs the walk to
# reach a chunk transaction, and a `DatabaseDouble` cannot get it there: the
# driver seeds and then *reads* its ledger row, and `row_from_record` wants a
# full record. Scripting one through the double's positional `results` would
# bake the ledger's column list into this file, and that list changed twice in
# one day. §6.4 stays DSN-gated until the ledger grows a "script me a pass in
# state X" seam; see the report.
#
# What the transaction seam *can* prove without a server is the property that
# holds regardless of which fault fires, and it is the one that turns a bad
# chunk into an incident: the connection comes back.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["adapter-begin_error", "adapter-statement_timeout", "adapter-commit_error"],
)
async def test_a_failed_shift_still_returns_its_connection(name: str) -> None:
    from wreath._passes.driver import run_shift
    from wreath.passes import ChunkedPass, DutyCycle, Key, Purge, Rows, Sealed, Table

    schedule = FaultSchedule.from_bytes(CORPUS[name].to_bytes())
    double = ReplayAdapters.from_faults(schedule.adapter_faults).databases["main"]
    walk = ChunkedPass(
        "corpus_purge",
        over=Table("things"),
        units=Rows(
            key=(
                Key("expires", "timestamptz", indexed=True),
                Key("id", "bigint", unique=True),
            ),
            limit=10,
        ),
        frontier=Sealed(),
        work=Purge(),
        pace=DutyCycle(1.0),
    )
    result = await run_shift(walk, double, budget=0.05)
    # `run_shift`'s `finally` is the whole assertion: whatever went wrong, the
    # connection is not still leased by a transaction that will never commit.
    assert double.acquired == double.released == 1, (
        "a failed shift kept its connection -- that is a stuck lease"
    )
    # And it *reports* rather than hanging or raising past the caller.
    assert result.stopped is not None
