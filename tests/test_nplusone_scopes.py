"""Query budgets outside the request: job attempts, workflow steps, pass shifts.

The N+1 guard was request-shaped. Everything here is about the scopes that do
the heaviest ORM work and had no ledger at all -- a durable job, a workflow
step, and a chunked pass shift.

The ORM seam is `orm.session.Session._count_read`, which reads the
`query_ledger` ContextVar and calls `ledger.record("module.QualName")`. These
tests stand in for it by making that same call, which is the whole of what the
seam does; the live-ORM path is covered by the existing doctor suite.
"""

from __future__ import annotations

import asyncio

import pytest

from wreath import _nplusone
from wreath._nplusone import NPlusOneDetected, Origin, QueryLedger, watching
from wreath.jobs import JobRunner, _Claimed

MODEL = "app.models.Sighting"


@pytest.fixture
def unarmed():
    """Restore the latched `WATCHING` flag, so one test cannot arm the next."""
    previous = _nplusone.WATCHING
    _nplusone.WATCHING = False
    try:
        yield
    finally:
        _nplusone.WATCHING = previous


def _record(times: int, model: str = MODEL) -> None:
    """Exactly what the ORM seam does, `times` times."""
    ledger = _nplusone.query_ledger.get(None)
    if ledger is None:
        return
    for _ in range(times):
        ledger.record(model)


# --- stage 1: the general scope ----------------------------------------------


def test_watching_binds_a_ledger_and_resets_it(unarmed):
    origin = Origin(kind="job", label="ingest_card", identifier="41")
    assert _nplusone.query_ledger.get(None) is None
    with watching(origin, limit=3) as ledger:
        assert _nplusone.query_ledger.get(None) is ledger
        assert ledger.origin == origin
    assert _nplusone.query_ledger.get(None) is None


def test_watching_arms_the_orm_seam(unarmed):
    assert _nplusone.WATCHING is False
    with watching(Origin(kind="job", label="t"), limit=3):
        assert _nplusone.WATCHING is True


def test_watching_resets_on_exception(unarmed):
    with pytest.raises(ValueError, match="boom"), watching(Origin(kind="job"), limit=3):
        raise ValueError("boom")
    assert _nplusone.query_ledger.get(None) is None


def test_watching_observes_without_raising_by_default(unarmed):
    with watching(Origin(kind="job", label="ingest"), limit=2) as ledger:
        _record(50)
    finding = ledger.finding()
    assert finding is not None
    assert finding.worst.count == 50


def test_watching_raises_when_asked(unarmed):
    with (
        pytest.raises(NPlusOneDetected) as caught,
        watching(Origin(kind="job", label="ingest"), limit=5, raises=True),
    ):
        _record(10)
    assert caught.value.finding.worst.count == 5


def test_nested_scopes_restore_the_outer_ledger(unarmed):
    with watching(Origin(kind="job", label="outer"), limit=3) as outer:
        with watching(Origin(kind="step", label="inner"), limit=3) as inner:
            assert _nplusone.query_ledger.get(None) is inner
        assert _nplusone.query_ledger.get(None) is outer


def test_guard_binding_survives_an_unreferenced_request(unarmed):
    """The guard must bind with a token, not a suspended context manager.

    A `@contextmanager` held on `request.state` is finalized when the request
    object becomes unreachable, which runs its `finally` and unbinds the ledger
    mid-request -- so the guard would silently stop counting for any caller
    that did not keep the request in a local. A `Token` has no finalizer.
    """
    import gc

    from wreath.doctor import NPlusOneGuard

    guard = NPlusOneGuard(limit=5)
    # Driven in *this* context rather than through `asyncio.run`, which would
    # copy the context into a new task and discard the binding on exit -- the
    # tape awaits `before` inside the request's own task, so this is the
    # faithful shape.
    coroutine = guard.before(_Request())
    try:
        coroutine.send(None)
    except StopIteration:
        pass
    gc.collect()
    ledger = _nplusone.query_ledger.get(None)
    try:
        assert ledger is not None
    finally:
        _nplusone.query_ledger.set(None)


class _Request:
    """The smallest thing the guard reads: a method, a path, and a state bag."""

    method = "GET"
    path = "/llamas"

    def __init__(self):
        self.state = _State()


class _State:
    def __init__(self):
        self._values: dict[str, object] = {}

    def __setattr__(self, name, value):
        if name == "_values":
            object.__setattr__(self, name, value)
            return
        self._values[name] = value

    def get(self, name, default=None):
        return self._values.get(name, default)


# --- stage 1: the finding carries an origin ----------------------------------


def test_finding_carries_origin_and_keeps_request_id():
    origin = Origin(kind="job", label="ingest_card", identifier="41")
    ledger = QueryLedger(limit=2, origin=origin)
    ledger.record(MODEL)
    ledger.record(MODEL)
    finding = ledger.finding()
    assert finding.origin == origin
    assert finding.request_id == 0


def test_request_findings_still_describe_a_route():
    ledger = QueryLedger(limit=2, route="GET /llamas")
    ledger.record(MODEL)
    ledger.record(MODEL)
    finding = ledger.finding()
    assert finding.route == "GET /llamas"
    assert finding.origin.kind == "request"
    assert "GET /llamas" in finding.explain()


def test_a_trace_scan_threshold_below_one_is_refused():
    """Pre-existing refusal that no test reached: a threshold of zero would
    report every trace as a finding."""
    from wreath._nplusone import find_n_plus_one

    with pytest.raises(ValueError, match="threshold must be >= 1"):
        find_n_plus_one([], threshold=0)


def test_a_scope_with_no_identifier_describes_without_a_hash():
    """A pass shift has a name and no id -- `shift recode_species#` would be
    a dangling reference to nothing."""
    assert Origin(kind="shift", label="recode_species").explain() == "shift recode_species"
    assert Origin(kind="job", label="ingest", identifier="41").explain() == "job ingest#41"


def test_a_finding_says_how_many_other_models_it_found():
    ledger = QueryLedger(limit=2, origin=Origin(kind="job", label="ingest"))
    for _ in range(3):
        ledger.record("app.Sighting")
    for _ in range(2):
        ledger.record("app.Station")
    described = ledger.finding().explain()
    assert "Sighting" in described
    assert "(and 1 more)" in described


def test_a_finding_built_without_an_origin_still_names_its_route():
    """The older two-argument shape. Anything already constructing a `Finding`
    keeps working, and keeps reading the same way."""
    from wreath._nplusone import Finding, Repetition

    finding = Finding(route="GET /llamas", repetitions=(Repetition("Trek", 12),), queries=13)
    assert "GET /llamas" in finding.explain()


def test_trace_scan_output_is_unchanged_and_gains_an_origin():
    """`find_n_plus_one` is the older producer; its shape must not move."""
    from wreath._nplusone import find_n_plus_one

    traces = [
        {
            "request_id": 7,
            "route_id": 1,
            "phases": [{"phase": "orm_hydrate", "dependency_id": 2, "duration_us": 5}] * 12
            + [{"phase": "db_query"}] * 12,
        }
    ]
    findings = find_n_plus_one(
        traces,
        threshold=10,
        routes=[{"id": 1, "method": "GET", "path": "/llamas"}],
        models=[{"id": 2, "name": "Sighting"}],
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.route == "GET /llamas"
    assert finding.request_id == 7
    assert finding.worst.count == 12
    assert "GET /llamas" in finding.explain()
    assert finding.origin.kind == "request"


def test_job_finding_describes_the_task_not_a_route():
    ledger = QueryLedger(limit=2, origin=Origin(kind="job", label="ingest_card", identifier="41"))
    ledger.record(MODEL)
    ledger.record(MODEL)
    described = ledger.finding().explain()
    assert "ingest_card" in described
    assert "job" in described


# --- stage 2/3: the job attempt scope ----------------------------------------


class _FakeConnection:
    async def execute(self, sql, *args):
        return "OK"

    async def fetchval(self, sql, *args):
        return 1

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None


class _FakeDatabase:
    def __init__(self):
        self.connection = _FakeConnection()

    async def acquire(self, workload):
        return self.connection

    async def release(self, workload, connection):
        return None


def _claimed(runner, task, attempts=0):
    return _Claimed(
        id=41, task=task, args=[], tenant="", attempts=attempts,
        max_attempts=5, fence=1, key=None,
    )


def test_job_without_budget_and_without_a_guard_binds_nothing(unarmed):
    """The cost of this plan must be zero for an app that never asked."""
    runner = JobRunner(_FakeDatabase(), name="work")
    seen: list[object] = []

    @runner.task("plain")
    async def plain(ctx):
        seen.append(_nplusone.query_ledger.get(None))

    asyncio.run(runner._run(_claimed(runner, "plain")))
    assert seen == [None]
    assert _nplusone.WATCHING is False


def test_job_with_budget_binds_a_ledger_naming_the_attempt(unarmed):
    runner = JobRunner(_FakeDatabase(), name="work")
    seen: list[object] = []

    @runner.task("ingest", query_budget=100)
    async def ingest(ctx):
        seen.append(_nplusone.query_ledger.get(None))

    asyncio.run(runner._run(_claimed(runner, "ingest")))
    ledger = seen[0]
    assert ledger is not None
    assert ledger.origin.kind == "job"
    assert ledger.origin.label == "ingest"
    assert ledger.origin.identifier == "41"
    assert ledger.limit == 100


def test_job_over_budget_raises_inside_the_query(unarmed):
    """The traceback must name the loop, not the bookkeeping.

    Asserting the *frame* rather than the exception type is the whole point of
    raising from the seam: an error reported after the attempt would say a
    budget was crossed without saying by which line.
    """
    import traceback

    frames: list[str] = []
    runner = JobRunner(_FakeDatabase(), name="work")

    @runner.task("greedy", query_budget=5)
    async def greedy(ctx):
        try:
            for _ in range(50):
                _record(1)          # <- the loop the traceback must name
        except NPlusOneDetected as error:
            frames.extend(
                frame.name for frame in traceback.extract_tb(error.__traceback__)
            )
            raise

    asyncio.run(runner._run(_claimed(runner, "greedy")))
    assert runner.query_budget_exceeded == 1
    assert runner.nplusone_findings == 0
    # The innermost frames are the ledger's own record/raise; the one that
    # matters is the handler frame, which is where the loop is written.
    assert "greedy" in frames


def test_job_without_budget_observes_when_a_guard_exists(unarmed):
    """A guard in the process means somebody asked to be told; still never raises."""
    _nplusone.watch()
    runner = JobRunner(_FakeDatabase(), name="work")
    seen: list[object] = []

    @runner.task("observed")
    async def observed(ctx):
        seen.append(_nplusone.query_ledger.get(None))
        _record(500)

    asyncio.run(runner._run(_claimed(runner, "observed")))
    assert seen[0] is not None
    assert runner.query_budget_exceeded == 0
    assert runner.nplusone_findings == 1


def test_each_attempt_gets_a_fresh_ledger(unarmed):
    """Counts must not accumulate across retries -- an attempt is one execution."""
    runner = JobRunner(_FakeDatabase(), name="work")
    counts: list[int] = []

    @runner.task("retried", query_budget=1000)
    async def retried(ctx):
        _record(10)
        counts.append(_nplusone.query_ledger.get(None).counts[MODEL])

    asyncio.run(runner._run(_claimed(runner, "retried", attempts=0)))
    asyncio.run(runner._run(_claimed(runner, "retried", attempts=1)))
    assert counts == [10, 10]


def test_ledger_is_reset_when_the_handler_raises(unarmed):
    runner = JobRunner(_FakeDatabase(), name="work")

    @runner.task("boom", query_budget=100)
    async def boom(ctx):
        raise ValueError("nope")

    asyncio.run(runner._run(_claimed(runner, "boom")))
    assert _nplusone.query_ledger.get(None) is None


# --- stage 4: the report groups by origin ------------------------------------


def _finding(kind, label, identifier="", count=12, request_id=0):
    from wreath._nplusone import Finding, Repetition

    return Finding(
        route=label,
        repetitions=(Repetition(model="Sighting", count=count, total_us=1000),),
        queries=count + 1,
        request_id=request_id,
        origin=Origin(kind=kind, label=label, identifier=identifier),
    )


def test_report_groups_findings_by_origin(capsys):
    from wreath._cli import _print_n_plus_one

    _print_n_plus_one(
        [
            _finding("request", "GET /llamas", request_id=7),
            _finding("job", "ingest_card", "41"),
            _finding("shift", "recode_species"),
            _finding("step", "charge_card", "k9"),
        ],
        threshold=10,
    )
    out = capsys.readouterr().out
    assert "1 request(s) queried" in out
    assert "1 job attempt(s) queried" in out
    assert "1 workflow step(s) queried" in out
    assert "1 pass shift(s) queried" in out
    # Requests first: it is what a reader came for.
    assert out.index("request(s) queried") < out.index("job attempt(s) queried")
    assert "job ingest_card#41" in out


def test_report_offers_replay_only_where_there_is_a_request_to_replay(capsys):
    from wreath._cli import _print_n_plus_one

    _print_n_plus_one([_finding("job", "ingest_card", "41")], threshold=10)
    out = capsys.readouterr().out
    assert "wreath replay --request" not in out


def test_report_does_not_drop_a_scope_it_does_not_know(capsys):
    from wreath._cli import _print_n_plus_one

    _print_n_plus_one([_finding("saga", "something_new")], threshold=10)
    out = capsys.readouterr().out
    assert "saga(s) queried" in out


# --- stage 2/3: pass shifts --------------------------------------------------


def test_pass_shift_binds_a_ledger_naming_the_pass(unarmed):
    """A backfill with an N+1 per chunk is the disaster this plan exists for:
    it passes every test on a table that fits in one chunk."""
    from wreath._passes import driver

    seen: list[object] = []

    class _Walk:
        name = "recode_species"
        workload = "write"
        query_budget = 25

    async def _fake_shift(walk, connection, **kw):
        seen.append(_nplusone.query_ledger.get(None))
        return "result"

    class _Database:
        async def acquire(self, workload):
            return object()

        async def release(self, workload, connection):
            return None

    original = driver._shift
    driver._shift = _fake_shift
    try:
        asyncio.run(driver.run_shift(_Walk(), _Database()))
    finally:
        driver._shift = original

    ledger = seen[0]
    assert ledger is not None
    assert ledger.origin.kind == "shift"
    assert ledger.origin.label == "recode_species"
    assert ledger.limit == 25


def test_pass_shift_without_budget_binds_nothing(unarmed):
    from wreath._passes import driver

    seen: list[object] = []

    class _Walk:
        name = "plain"
        workload = "write"
        query_budget = None

    async def _fake_shift(walk, connection, **kw):
        seen.append(_nplusone.query_ledger.get(None))
        return "result"

    class _Database:
        async def acquire(self, workload):
            return object()

        async def release(self, workload, connection):
            return None

    original = driver._shift
    driver._shift = _fake_shift
    try:
        asyncio.run(driver.run_shift(_Walk(), _Database()))
    finally:
        driver._shift = original
    assert seen == [None]


def test_pass_query_budget_must_be_positive():
    """Refused at declaration, before the source is even looked at -- the
    traceback should name the line that wrote the budget."""
    from wreath.passes import ChunkedPass

    with pytest.raises(ValueError, match="query_budget"):
        ChunkedPass(
            "bad",
            over=object,
            units=None,
            frontier=None,
            work=None,
            query_budget=0,
        )


# --- stage 2/3: workflow steps, including compensations ----------------------


def _workflow(name="checkout"):
    from wreath.workflows import Workflow

    return Workflow(name)


def _store():
    from wreath.workflows import InMemoryWorkflowStore

    return InMemoryWorkflowStore()


def test_workflow_step_binds_a_ledger_naming_the_step(unarmed):
    flow = _workflow()
    seen: list[object] = []

    @flow.step(query_budget=50)
    async def reserve_stock(context):
        seen.append(_nplusone.query_ledger.get(None))

    asyncio.run(flow.run(store=_store(), key="k1"))
    ledger = seen[0]
    assert ledger.origin.kind == "step"
    assert ledger.origin.label == "reserve_stock"
    assert ledger.origin.identifier == "k1"
    assert ledger.limit == 50


def test_workflow_step_over_budget_fails_the_step(unarmed):
    flow = _workflow()

    @flow.step(query_budget=3)
    async def greedy(context):
        _record(20)

    with pytest.raises(NPlusOneDetected):
        asyncio.run(flow.run(store=_store(), key="k2"))


def test_compensation_is_its_own_scope(unarmed):
    """Undo paths run only when something already went wrong -- the least
    exercised code in a saga, and so the most worth watching."""
    flow = _workflow()
    seen: list[object] = []

    async def undo(context):
        seen.append(_nplusone.query_ledger.get(None))

    @flow.step(compensate=undo, query_budget=50)
    async def first(context):
        return "ok"

    @flow.step
    async def second(context):
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        asyncio.run(flow.run(store=_store(), key="k3"))
    ledger = seen[0]
    assert ledger is not None
    assert ledger.origin.kind == "step"
    assert ledger.origin.label == "first:compensate"


def test_workflow_step_without_budget_binds_nothing(unarmed):
    flow = _workflow()
    seen: list[object] = []

    @flow.step
    async def plain(context):
        seen.append(_nplusone.query_ledger.get(None))

    asyncio.run(flow.run(store=_store(), key="k4"))
    assert seen == [None]
    assert _nplusone.WATCHING is False


def test_workflow_query_budget_must_be_positive():
    flow = _workflow()
    with pytest.raises(ValueError, match="query_budget"):

        @flow.step(query_budget=-1)
        async def bad(context):
            return None


def test_query_budget_must_be_positive():
    runner = JobRunner(_FakeDatabase(), name="work")
    with pytest.raises(ValueError, match="query_budget"):

        @runner.task("bad", query_budget=0)
        async def bad(ctx):
            return None
