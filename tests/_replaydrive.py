"""Drivers that put *real* Wreath subsystems behind the fault corpus.

The corpus was already good at naming failures. What it was not good at was
being *driven*: the corpus test asserted a generic outcome ("something
deterministic happened"), which is one grade above asserting nothing. A region
only earns its place if some owned code answers it differently from its
neighbours, and that is only checkable if the owned code actually runs.

So each driver here starts a genuine subsystem -- a `MessageBus` doorbell, a
`JobRunner`, a `ChunkedPass` shift, a `PostgresStore` claim, a `SingletonRunner`,
the request pipeline -- points a schedule at it, and reports an
:class:`Observation`: what was raised, which counters moved, what status or
state was recorded. Two properties are then checkable across the whole corpus
at once, and both of them are properties whose absence shipped:

* **No fault may produce a hang.** Every drive runs under a wall-clock bound and
  names the schedule and the driver when it blows. A defect shipped today in a
  *default* configuration where a query error was raised and printed while the
  caller waited forever; a hang has to be a red test, not a stalled suite.
* **No fault may produce silence.** Every drive is compared against its own
  no-fault control, and must differ in at least one named channel. "Nothing
  happened and nothing was recorded" is the exact shape of the doorbell that
  died and never reconnected, of `_start_passes`, of `_enqueue_next_shift`, and
  of `services._cancel_all`.

Separate from any `test_*.py` file because several of them import it, and
`import conftest` is this tree's cautionary tale about ambiguous test-adjacent
module names. The basename is repo-unique on purpose.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import wreath
from wreath._replay_adapters import AdapterFault, DatabaseDouble
from wreath.postgres import Connection
from wreath.replay import (
    AdapterSeam,
    CanonicalRequest,
    FaultSchedule,
    ReplayAdapters,
    record_transport_segments,
    replay_endpoint_plan,
    replay_transport,
)

#: The wall-clock bound every drive runs under. Generous by two orders of
#: magnitude: the slowest driver here settles in tens of milliseconds, and a
#: drive that needs seconds is a finding whatever the number is. It exists so a
#: hang fails one test instead of stalling the run, which is how
#: `catalog destination requires binary rows` sat unseen.
BOUND = 5.0

#: A sentinel seam for schedules that perturb the inbound byte stream rather
#: than an adapter. Not an `AdapterSeam` member because it is not one -- putting
#: it in the enum would make it reachable from an `AdapterFaultDescriptor`.
TRANSPORT = "transport"


class ReplayDriveTimeout(AssertionError):
    """A drive did not finish inside :data:`BOUND`. Names both coordinates."""


@dataclass(frozen=True, slots=True)
class Observation:
    """What a driver saw. Every field is a *channel* through which a subsystem
    can say that something happened; an observation with no channel populated is
    the silence this module exists to refuse.

    Deliberately concrete. "The subsystem behaved reasonably" is not an
    observation, and a property built on one is the decorative assertion this
    repository has been bitten by seven times.
    """

    #: Exception types that reached the caller, by name.
    exceptions: tuple[str, ...] = ()
    #: Named counters the subsystem itself keeps, after the drive.
    counters: Mapping[str, int] = field(default_factory=dict)
    #: Statuses / states the subsystem recorded (HTTP status, `stopped` reason,
    #: claim outcome). Strings so heterogeneous drivers stay comparable.
    states: tuple[str, ...] = ()

    def diff(self, control: Observation) -> tuple[str, ...]:
        """The channels in which this differs from its no-fault control."""
        changed: list[str] = []
        if self.exceptions != control.exceptions:
            changed.append("exceptions")
        moved = [
            name
            for name, value in self.counters.items()
            if control.counters.get(name) != value
        ]
        if moved:
            changed.append("counters:" + ",".join(sorted(moved)))
        if self.states != control.states:
            changed.append("states")
        return tuple(changed)


@dataclass(frozen=True, slots=True)
class Driver:
    """One subsystem, and the seams a schedule must touch for it to be relevant.

    ``seams`` is what makes the corpus/driver matrix self-checking: a schedule
    that no driver declares reach over is a corpus entry nothing exercises, and
    the property test turns that into a red test rather than a quiet gap.
    """

    name: str
    seams: frozenset[Any]
    run: Callable[[FaultSchedule], Awaitable[Observation]]


def schedule_seams(schedule: FaultSchedule) -> frozenset[Any]:
    """Which seams a schedule perturbs. The routing key for driver selection."""
    seams: set[Any] = {AdapterSeam(fault.seam) for fault in schedule.adapter_faults}
    if schedule.faults:
        seams.add(TRANSPORT)
    return frozenset(seams)


async def observe(driver: Driver, schedule_name: str, schedule: FaultSchedule) -> Observation:
    """Run one drive under the wall-clock bound, naming both coordinates."""
    try:
        async with asyncio.timeout(BOUND):
            return await driver.run(schedule)
    except TimeoutError:
        raise ReplayDriveTimeout(
            f"schedule {schedule_name!r} hung driver {driver.name!r}: no owned "
            f"outcome within {BOUND:g}s. A fault must fail, degrade, or be "
            "handled -- it may never leave a caller waiting."
        ) from None


def _double(schedule: FaultSchedule, target: str = "main") -> DatabaseDouble:
    """The database double a schedule reconstructs, or an unfaulted one."""
    adapters = ReplayAdapters.from_faults(schedule.adapter_faults)
    return adapters.databases.get(target) or DatabaseDouble(target)


# --- a supervisor, exactly as the real one behaves ---------------------------


class Supervisor:
    """Spawns real tasks and stops them the way `wreath.Supervisor` does.

    A test double for the *lifecycle*, not for the subsystem: the tasks it
    spawns are the subsystem's own coroutines, so a loop that gives up gives up
    for real here.
    """

    def __init__(self) -> None:
        self.stopping = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []

    def spawn(self, name: str, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def stop(self, service: Any = None) -> None:
        self.stopping.set()
        if service is not None and hasattr(service, "drain"):
            await service.drain(asyncio.get_running_loop().time() + 0.5)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def until(predicate: Callable[[], bool], *, within: float = 1.0) -> bool:
    """Poll ``predicate`` until it holds or ``within`` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


# --- transport: the owned HTTP/1 driver ---------------------------------------

GET = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
POST = (
    b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 12\r\n"
    b"Connection: close\r\n\r\nhello world!"
)


def _http_app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/")
    async def root(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("ok")

    @app.post("/echo")
    async def echo(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse((await request.body()).decode("latin-1"))

    return app


def _thirds(raw: bytes) -> list[bytes]:
    cut = len(raw) // 3
    return [raw[:cut], raw[cut : cut * 2], raw[cut * 2 :]]


def transport_driver(protocol_cls: type, label: str) -> Driver:
    """Two recordings, not one.

    A bodyless GET split into thirds is not enough to make every transport
    region visible: duplicating the *middle* of its header block happens to
    reparse into a valid request, so `DUPLICATE` at a mid-stream segment
    compared identical to its control and the region proved nothing. A request
    with a `Content-Length` body has somewhere for a duplicated or reordered
    read to actually go wrong. Both are driven, and the observation carries
    both, so a region only counts as lossless when it is lossless for *both*.
    """

    async def one(recording_bytes: bytes, tag: str, schedule: FaultSchedule) -> Observation:
        recording = record_transport_segments(_thirds(recording_bytes))
        result = await replay_transport(
            _http_app(), recording, protocol_cls=protocol_cls, faults=schedule
        )
        status = result.normalized.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        return Observation(
            counters={
                f"{tag}:write_count": result.write_count,
                f"{tag}:segments_fed": result.segments_fed,
                f"{tag}:response_bytes": len(result.normalized),
            },
            states=(f"{tag}:{result.terminal}", f"{tag}:{status}"),
        )

    async def run(schedule: FaultSchedule) -> Observation:
        get = await one(GET, "get", schedule)
        post = await one(POST, "post", schedule)
        return Observation(
            counters={**get.counters, **post.counters},
            states=get.states + post.states,
        )

    return Driver(name=f"transport-{label}", seams=frozenset({TRANSPORT}), run=run)


# --- the request pipeline over the pool seams ---------------------------------


def _db_app() -> wreath.Wreath:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/db")
    async def db(request: wreath.Request, conn: Connection) -> dict:
        return {"n": len(await conn.fetch("SELECT id FROM things"))}

    return app


async def _run_endpoint(schedule: FaultSchedule) -> Observation:
    double = _double(schedule)
    app = _db_app()
    # **Twice.** A statement whose parameter type was inferred on the first
    # execution and cannot be encoded on the second is invisible to any driver
    # that runs each statement once -- which is how `$1::regclass` reached a
    # default code path. Every driver here issues its work twice for that
    # reason, and reports both outcomes.
    statuses = []
    for _ in range(2):
        result = await replay_endpoint_plan(
            app,
            CanonicalRequest("GET", "/db"),
            adapters=ReplayAdapters(databases={"main": double}),
        )
        statuses.append(str(result.status))
    return Observation(
        counters={"acquired": double.acquired, "released": double.released},
        states=tuple(statuses),
    )


ENDPOINT = Driver(
    name="endpoint-plan",
    seams=frozenset(
        {
            AdapterSeam.DB_ACQUIRE,
            AdapterSeam.DB_QUERY,
            AdapterSeam.DB_RELEASE,
            AdapterSeam.DB_CONNECTION,
        }
    ),
    run=_run_endpoint,
)


# --- the outbound HTTP client -------------------------------------------------


async def _run_http_client(schedule: FaultSchedule) -> Observation:
    adapters = ReplayAdapters.from_faults(schedule.adapter_faults)
    from wreath.replay import FaultyHttpClient

    client = adapters.clients.get("api") or FaultyHttpClient("api")
    raised: tuple[str, ...] = ()
    status = "none"
    try:
        response = await client.request("GET", "/upstream")
    except Exception as error:  # noqa: BLE001 -- the injected fault is the subject
        # Broad because the region under test *is* which exception comes out;
        # naming a set here would assert the injector's taxonomy rather than
        # observing the client's.
        raised = (type(error).__name__,)
    else:
        status = str(response.status)
    return Observation(exceptions=raised, states=(status,))


HTTP_CLIENT = Driver(
    name="http-client",
    seams=frozenset({AdapterSeam.HTTP_REQUEST}),
    run=_run_http_client,
)


# --- the LISTEN/NOTIFY doorbell, under a real supervisor ----------------------


async def _run_bus_doorbell(schedule: FaultSchedule) -> Observation:
    from wreath.messaging import MessageBus

    double = _double(schedule)
    bus = MessageBus(double, name="drive", poll_interval=60.0)

    async def handler(message: Any) -> None:
        return None

    bus.subscribe("things")(handler)
    supervisor = Supervisor()
    try:
        await bus.start(supervisor)
        # The owned recovery is *retrying*, so the observation is the retry, not
        # a single blip. Waiting for two says the supervisor did not stop after
        # the first -- which is the historical bug in one assertion.
        await until(lambda: bus.doorbell_reconnects >= 2 or double.streams >= 2)
    finally:
        await supervisor.stop(bus)
    return Observation(
        counters={
            "doorbell_reconnects": bus.doorbell_reconnects,
            "listens": double.listens,
            "streams": double.streams,
            "handler_errors": bus.handler_errors,
        },
        states=(f"listened={len(double.listened)}",),
    )


BUS_DOORBELL = Driver(
    name="bus-doorbell",
    seams=frozenset({AdapterSeam.DB_LISTEN}),
    run=_run_bus_doorbell,
)


async def _run_jobs_doorbell(schedule: FaultSchedule) -> Observation:
    from wreath.jobs import JobRunner

    double = _double(schedule)
    runner = JobRunner(double, name="drive", concurrency=1, poll_interval=60.0)

    @runner.task("noop")
    async def noop(ctx: Any) -> None:
        return None

    supervisor = Supervisor()
    try:
        await runner.start(supervisor)
        await until(lambda: runner.doorbell_reconnects >= 2 or double.streams >= 2)
    finally:
        await supervisor.stop(runner)
    return Observation(
        counters={
            "doorbell_reconnects": runner.doorbell_reconnects,
            "listens": double.listens,
            "streams": double.streams,
            "sweep_errors": runner.sweep_errors,
        },
        states=(f"listened={len(double.listened)}",),
    )


JOBS_DOORBELL = Driver(
    name="jobs-doorbell",
    seams=frozenset({AdapterSeam.DB_LISTEN}),
    run=_run_jobs_doorbell,
)


# --- a chunked pass over the transaction seam ---------------------------------


def chunked_pass() -> Any:
    from wreath.passes import ChunkedPass, DutyCycle, Key, Purge, Rows, Sealed, Table

    return ChunkedPass(
        "drive_purge",
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


async def _run_pass_shift(schedule: FaultSchedule) -> Observation:
    from wreath._passes.driver import run_shift

    double = _double(schedule)
    walk = chunked_pass()
    raised: list[str] = []
    stopped: list[str] = []
    for _ in range(2):
        try:
            result = await run_shift(walk, double, budget=0.05)
        except Exception as error:  # noqa: BLE001 -- a shift that raises is an outcome too
            # `run_shift` is documented to *report*; a schedule that makes it
            # raise instead is still an observation, and pinning the type here
            # would assert the injector rather than the driver.
            raised.append(type(error).__name__)
            stopped.append("raised")
        else:
            stopped.append(str(result.stopped))
    return Observation(
        exceptions=tuple(raised),
        counters={"acquired": double.acquired, "released": double.released},
        states=tuple(stopped),
    )


#: Deliberately **not** `DB_TRANSACTION`. `_passes/driver.py::_run_chunk` is the
#: only owned consumer of `connection.transaction()`, and reaching it needs the
#: walk to get past a ledger read that a `DatabaseDouble` cannot script without
#: baking the ledger's column list into this file -- a list that changed twice
#: in one day. Declaring reach this driver does not have is how a suite acquires
#: a check with nothing to check, so the transaction seam is covered by the
#: DSN-gated driver in `tests/test_replay_live_faults.py` instead.
PASS_SHIFT = Driver(
    name="pass-shift",
    seams=frozenset({AdapterSeam.DB_QUERY, AdapterSeam.DB_ACQUIRE}),
    run=_run_pass_shift,
)


# --- a keyed store claim ------------------------------------------------------


def keyed_store(double: DatabaseDouble) -> Any:
    from wreath.store import Column, Keyed, PostgresStore

    return PostgresStore(
        double,
        Keyed(
            table="drive_claims",
            columns=(Column("value", "int"),),
            ttl=60.0,
            claim=True,
            prefix="drive",
        ),
    )


async def _run_store_claim(schedule: FaultSchedule) -> Observation:
    # A row is scripted so the *unfaulted* control genuinely claims. Without
    # one, "no row came back" would be the control as well as the fault, and
    # the assertion would be comparing silence with silence.
    double = _double(schedule)
    double.results = ({"key": "k"},)
    store = keyed_store(double)
    raised: list[str] = []
    held: list[str] = []
    for _ in range(2):  # twice; see `_run_endpoint`
        try:
            held.append("held" if await store.claim("k") else "refused")
        except Exception as error:  # noqa: BLE001 -- which exception is the region
            raised.append(type(error).__name__)
            held.append("raised")
    return Observation(
        exceptions=tuple(raised),
        counters={"acquired": double.acquired, "released": double.released},
        states=tuple(held),
    )


STORE_CLAIM = Driver(
    name="store-claim",
    seams=frozenset(
        {
            AdapterSeam.DB_QUERY,
            AdapterSeam.DB_ACQUIRE,
            AdapterSeam.DB_RELEASE,
            AdapterSeam.DB_CONNECTION,
        }
    ),
    run=_run_store_claim,
)


# --- object storage -----------------------------------------------------------


async def _run_object_store(schedule: FaultSchedule) -> Observation:
    """Three probes, each on its **own** store, so every fault lands at its
    coordinate.

    The store seam's coordinate is "the Nth operation", not "the Nth operation
    of this kind", so a single script of write-then-read puts the write fault
    and the read fault at different indices and one of them silently never
    fires. Three fresh stores keep coordinate 0 meaning the operation under
    test. The first cut of this driver did it the other way and
    `object_read_short` compared identical to its control.
    """
    from wreath._replay_adapters import ObjectStoreDouble

    payload = b"0123456789"
    raised: list[str] = []
    states: list[str] = []

    def fresh() -> Any:
        adapters = ReplayAdapters.from_faults(schedule.adapter_faults)
        return adapters.object_stores.get("objects") or ObjectStoreDouble("objects")

    store = fresh()
    try:
        await store.write("k", payload)
    except Exception as error:  # noqa: BLE001 -- the storage fault is the subject
        raised.append(f"write:{type(error).__name__}")
    states.append(f"exists={await store.exists('k')}")
    if await store.exists("k"):
        states.append(f"intact={await store._inner.read('k') == payload}")

    store = fresh()
    await store._inner.write("k", payload)
    try:
        states.append(f"read={len(await store.read('k'))}")
    except Exception as error:  # noqa: BLE001 -- as above
        raised.append(f"read:{type(error).__name__}")

    store = fresh()
    await store._inner.write("k", payload)
    try:
        stat = await store.stat("k")
        states.append(f"stat={stat.size}")
    except Exception as error:  # noqa: BLE001 -- as above
        raised.append(f"stat:{type(error).__name__}")
    return Observation(exceptions=tuple(raised), states=tuple(states))


OBJECT_STORE = Driver(
    name="object-store",
    seams=frozenset({AdapterSeam.OBJECT_STORE}),
    run=_run_object_store,
)


# --- jobs.launch over the query seam ------------------------------------------


async def _run_job_launch(schedule: FaultSchedule) -> Observation:
    from wreath.jobs import JobRunner
    from wreath.progress import ProgressRegistry

    double = _double(schedule)
    # The id the INSERT ... RETURNING hands back on the happy path, so the
    # control seeds a task and the faulted run has something to *not* seed.
    double.results = (41,)
    registry = ProgressRegistry()
    runner = JobRunner(double, name="drive", progress=registry)

    @runner.task("import_herd")
    async def import_herd(ctx: Any) -> None:
        return None

    raised: list[str] = []
    task_ids: list[str] = []
    for _ in range(2):  # twice; see `_run_endpoint`
        try:
            handle = await runner.launch("import_herd")
        except Exception as error:  # noqa: BLE001 -- which failure is the region
            raised.append(type(error).__name__)
            task_ids.append("raised")
        else:
            task_ids.append(handle.task_id)
    seeded = registry.get("41")
    return Observation(
        exceptions=tuple(raised),
        counters={"acquired": double.acquired, "released": double.released},
        states=(*task_ids, "queued" if seeded is not None else "unseeded"),
    )


JOB_LAUNCH = Driver(
    name="job-launch",
    seams=frozenset(
        {
            AdapterSeam.DB_QUERY,
            AdapterSeam.DB_ACQUIRE,
            AdapterSeam.DB_RELEASE,
            AdapterSeam.DB_CONNECTION,
        }
    ),
    run=_run_job_launch,
)


def transport_drivers() -> tuple[Driver, ...]:
    """The HTTP/1 drivers available in this build (the native one is optional)."""
    import importlib

    from wreath._pure.server import Http1Protocol as pure

    drivers = [transport_driver(pure, "pure")]
    try:
        native = importlib.import_module("wreath._native._server")
    except ImportError:
        return tuple(drivers)
    drivers.append(transport_driver(native.Http1Protocol, "native"))
    return tuple(drivers)


def all_drivers() -> tuple[Driver, ...]:
    return (
        *transport_drivers(),
        ENDPOINT,
        HTTP_CLIENT,
        BUS_DOORBELL,
        JOBS_DOORBELL,
        PASS_SHIFT,
        STORE_CLAIM,
        OBJECT_STORE,
        JOB_LAUNCH,
    )


def drivers_for(schedule: FaultSchedule) -> tuple[Driver, ...]:
    """Every driver whose declared reach covers this schedule's seams."""
    seams = schedule_seams(schedule)
    return tuple(driver for driver in all_drivers() if seams & driver.seams)


__all__ = [
    "BOUND",
    "TRANSPORT",
    "AdapterFault",
    "Driver",
    "Observation",
    "ReplayDriveTimeout",
    "Supervisor",
    "all_drivers",
    "chunked_pass",
    "drivers_for",
    "keyed_store",
    "observe",
    "schedule_seams",
    "until",
]
