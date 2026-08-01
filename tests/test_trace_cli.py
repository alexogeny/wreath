"""Plan 01 stage 4: the trace id an operator can actually reach.

Stages 1-3 put a `traceparent` on four durable rows. That is worth nothing until
somebody at three in the morning can get from a failure to the request that
caused it, and back the other way. Three surfaces do that:

* `wreath jobs list` -- the dead letters, each with the trace id of the request
  that enqueued it. The request finished hours ago; this is the only thread back
  to it.
* `wreath passes status` -- the same, on a pass that stopped.
* `wreath doctor trace <id>` -- the join: given a trace id, every job, durable
  message, workflow instance and chunked pass carrying it, plus the recorded
  request when an Inspector socket is given.

The property this file cares about most is the last one's **`omitted` list**.
A forensic tool that quietly leaves a source out is worse than one that answers
nothing, because the reader concludes "nothing else carries this trace" from a
search that never ran. Every source that could not be read is named in the same
report as the findings, and the tests below fail if one goes quiet.
"""

from __future__ import annotations

import json
import os

import pytest

from wreath import _pytest_plugin
from wreath._cli import build_parser, execute_doctor, execute_jobs
from wreath.doctor import find_requests_with_trace, find_work_with_trace
from wreath.telemetry import trace_id_of

PARENT = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
TRACE_ID = "a" * 32

_DSN = os.environ.get(_pytest_plugin.DSN_ENV)


class TestTraceIdOf:
    def test_a_traceparent_yields_its_trace_id(self):
        assert trace_id_of(PARENT) == TRACE_ID

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "not-a-traceparent",
            "00-tooshort-" + "b" * 16 + "-01",
            "00-" + "z" * 32 + "-" + "b" * 16 + "-01",
            # All-zero is invalid per the W3C spec and is what broken
            # instrumentation emits. Treating it as real would join every such
            # row to every other.
            "00-" + "0" * 32 + "-" + "b" * 16 + "-01",
            # Four fields, not three: a truncated value must not read as valid.
            "00-" + "a" * 32 + "-" + "b" * 16,
        ],
    )
    def test_anything_malformed_reads_as_absent_rather_than_raising(self, value):
        assert trace_id_of(value) is None


class _Connection:
    """A stand-in database that answers the two shapes the lookup issues."""

    def __init__(self, *, columns: dict[tuple[str, str], set[str]], rows=()):
        self.columns = columns
        self.rows = rows
        self.queries: list[str] = []
        #: `(sql, args)` for the reads, so a test can assert a value was *bound*
        #: rather than interpolated into the statement.
        self.reads: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args):
        # The version-2 column probe as a scalar, which is how the readers that
        # hold no runner ask. `None` for absent, the shape a real
        # `SELECT true ... WHERE` produces when it matches no rows.
        self.queries.append(sql)
        relation = "jobs" if "'jobs'" in sql else "messages"
        return True if "trace_context" in self.columns.get((args[0], relation), ()) else None

    async def fetch(self, sql: str, *args):
        self.queries.append(sql)
        if "pg_attribute" in sql:
            found = self.columns.get((args[0], args[1]), set())
            return [{"attname": name} for name in sorted(found)]
        self.reads.append((sql, args))
        for relation, rows in self.rows:
            if f".{relation} " in sql:
                return list(rows)
        return []


_FULL = {"tenant", "state", "phase", "last_error", "trace_context"}


def _schema(*relations: str, schema: str = "wreath", extra=()):
    columns = {}
    for relation in relations:
        columns[(schema, relation)] = _FULL | {"id", "task", "channel", "name", "queue"}
    for key, value in extra:
        columns[key] = value
    return columns


class TestTheTraceLookupNamesWhatItCouldNotRead:
    async def test_a_missing_table_is_reported_rather_than_read_as_empty(self):
        connection = _Connection(columns=_schema("jobs"))
        lookup = await find_work_with_trace(connection, TRACE_ID)

        assert not lookup.work
        joined = " | ".join(lookup.omitted)
        assert '"wreath".messages does not exist' in joined
        assert '"wreath".passes does not exist' in joined
        assert "workflow_steps_instances does not exist" in joined

    async def test_a_version_one_schema_is_reported_as_such(self):
        columns = _schema("jobs")
        columns[("wreath", "messages")] = {"id", "channel", "state", "tenant"}
        connection = _Connection(columns=columns)
        lookup = await find_work_with_trace(connection, TRACE_ID)

        note = next(n for n in lookup.omitted if "messages" in n)
        assert "no trace_context column" in note
        assert "schema version before propagation" in note

    async def test_a_version_one_workflow_table_is_reported_as_such(self):
        """The workflow source has its own present/upgraded pair of answers.

        It lives in a different schema from the other three and is read by
        different code, so "the table is there but predates propagation" is a
        third state it has to name rather than folding into "not there".
        """
        columns = _schema("jobs", "messages", "passes")
        columns[("wreath_system", "workflow_steps_instances")] = {
            "key", "workflow", "state", "tenant",
        }
        connection = _Connection(columns=columns)
        lookup = await find_work_with_trace(connection, TRACE_ID)

        note = next(n for n in lookup.omitted if "workflow" in n)
        assert "has no trace_context column" in note
        assert "does not exist" not in note

    async def test_the_ephemeral_bus_is_always_named_as_unsearched(self):
        """The deferral, surfaced where an operator will actually read it.

        Ephemeral fan-out carries no context by decision, not by oversight. If
        the lookup stayed silent about it, "no durable work carries this trace"
        would read as "nothing does".
        """
        connection = _Connection(columns=_schema("jobs", "messages", "passes"))
        lookup = await find_work_with_trace(connection, TRACE_ID)

        assert any("ephemeral bus messages" in note for note in lookup.omitted)

    async def test_every_source_is_matched_on_the_trace_id_not_a_prefix(self):
        """`split_part`, not `LIKE`: no wildcard to escape, no span-id collision."""
        connection = _Connection(columns=_schema("jobs", "messages", "passes"))
        await find_work_with_trace(connection, TRACE_ID)

        reads = [q for q in connection.queries if "pg_attribute" not in q]
        assert reads
        assert all("split_part(trace_context, '-', 2) = $1" in q for q in reads)
        assert not any("LIKE" in q for q in reads)

    async def test_rows_are_named_by_their_own_subsystems_vocabulary(self):
        connection = _Connection(
            columns=_schema("jobs", "messages", "passes"),
            rows=(
                (
                    "jobs",
                    [{
                        "id": 7, "task": "send_receipt", "state": "dead",
                        "tenant": "acme", "last_error": "boom",
                        "trace_context": PARENT,
                    }],
                ),
                (
                    "passes",
                    [{
                        "name": "normalize_grades", "phase": "blocked",
                        "tenant": "", "last_error": "chunk gave up",
                        "trace_context": PARENT,
                    }],
                ),
            ),
        )
        lookup = await find_work_with_trace(connection, TRACE_ID)

        kinds = {item.kind: item for item in lookup.work}
        assert kinds["job"].identifier == "7"
        assert kinds["job"].label == "send_receipt"
        assert kinds["job"].state == "dead"
        assert kinds["job"].tenant == "acme"
        assert kinds["pass"].identifier == "normalize_grades"
        assert kinds["pass"].state == "blocked", (
            "a pass's state column is `phase`, not `state`"
        )


class _InspectorClient:
    def __init__(self, traces):
        self._traces = traces

    async def timeline(self, *, limit):
        return {"traces": list(self._traces)}


class TestTheRequestHalfComesFromTheRing:
    async def test_a_matching_trace_is_returned(self):
        client = _InspectorClient([
            {"request_id": 3, "route_id": 1, "status": 500, "duration_us": 900,
             "is_failure": True, "error_class": 2, "trace_id": TRACE_ID},
            {"request_id": 4, "route_id": 1, "status": 200, "duration_us": 100,
             "is_failure": False, "error_class": 0, "trace_id": "b" * 32},
        ])
        found = await find_requests_with_trace(client, TRACE_ID)

        assert [r.request_id for r in found] == [3]
        assert found[0].is_failure

    async def test_an_unsampled_request_carries_no_id_and_is_not_matched(self):
        client = _InspectorClient([
            {"request_id": 3, "route_id": 1, "status": 200, "duration_us": 1,
             "is_failure": False, "error_class": 0, "trace_id": None},
        ])
        assert await find_requests_with_trace(client, TRACE_ID) == ()


class TestTheDoctorTraceCommand:
    def test_a_bare_id_and_a_whole_traceparent_are_both_accepted(self, capsys):
        parser = build_parser()
        for value in (TRACE_ID, PARENT):
            namespace = parser.parse_args(["doctor", "trace", value, "--json"])
            assert execute_doctor(namespace) == 0
            body = json.loads(capsys.readouterr().out)
            assert body["trace_id"] == TRACE_ID

    @pytest.mark.parametrize(
        "value",
        [
            # Too short, and the right length but not hex. Both operands of the
            # refusal, because a mutant sweep showed one test only covered one.
            "nope",
            "z" * 32,
            # All hex, wrong length. Without it the length operand of the
            # refusal can be deleted and every other case still refuses.
            "abc",
        ],
    )
    def test_something_that_is_not_a_trace_id_is_refused(self, value):
        from wreath._cli import CliError

        namespace = build_parser().parse_args(["doctor", "trace", value])
        with pytest.raises(CliError):
            execute_doctor(namespace)

    def test_with_no_target_it_says_it_read_no_database(self, capsys):
        namespace = build_parser().parse_args(["doctor", "trace", TRACE_ID])
        assert execute_doctor(namespace) == 0
        out = capsys.readouterr().out
        assert "not searched:" in out
        assert "no application target was given" in out

    def test_a_populated_report_renders_every_part_it_found(self, capsys):
        """The renderer, over a lookup that actually found something.

        Added because a mutant sweep found the whole populated branch
        unexercised: every test above drives an empty result, so the lines that
        print a request, a tenant-qualified label and a row's error could all be
        deleted and nothing objected. A report nobody has seen rendered is not a
        report.
        """
        from wreath._cli import _print_trace_lookup
        from wreath.doctor import TracedRequest, TracedWork, TraceLookup

        _print_trace_lookup(
            TraceLookup(
                trace_id=TRACE_ID,
                requests=(
                    TracedRequest(
                        request_id=3, route_id=1, status=500, duration_us=912,
                        is_failure=True, error_class=2,
                    ),
                    TracedRequest(
                        request_id=4, route_id=1, status=200, duration_us=11,
                        is_failure=False, error_class=0,
                    ),
                ),
                work=(
                    TracedWork(
                        kind="job", identifier="7", label="send_receipt",
                        state="dead", tenant="acme", detail="boom",
                        traceparent=PARENT,
                    ),
                    TracedWork(
                        kind="workflow", identifier="checkout:1", label="checkout",
                        state="compensated", tenant="", detail=None,
                        traceparent=PARENT,
                    ),
                ),
                omitted=("ephemeral bus messages: they carry no trace context",),
            )
        )
        out = capsys.readouterr().out

        assert "recorded request(s)" in out
        assert "2 recorded request(s):" in out
        assert "request 3  route 1  status 500  912us  FAILED" in out
        assert "status 200  11us  ok" in out
        assert "2 durable unit(s) of work:" in out
        # Tenant-qualified only when there is a tenant, the way every other
        # report in this CLI names a multi-tenant row.
        assert "send_receipt@acme" in out
        assert "checkout@" not in out
        # One error line, for the one row that has one: the workflow entry's
        # `detail` is None and must render nothing rather than "error: None".
        assert out.count("error: ") == 1
        assert "error: boom" in out
        assert "not searched:" in out

    def test_a_report_with_nothing_omitted_prints_no_such_section(self, capsys):
        """There is no such lookup today -- ephemeral messages are always
        unsearched -- but the renderer is a general one, and a section header
        with nothing under it is the shape that erodes trust in the section.
        """
        from wreath._cli import _print_trace_lookup
        from wreath.doctor import TraceLookup

        _print_trace_lookup(TraceLookup(trace_id=TRACE_ID))
        out = capsys.readouterr().out

        assert "not searched:" not in out
        assert "recorded request(s)" not in out
        assert "no durable work carries this trace" in out

    def test_with_no_socket_it_says_the_request_was_not_searched(self, capsys):
        """The half a database cannot answer, named rather than implied.

        The request lives in the Flight Recorder's ring. Printing "no durable
        work carries this trace" without this line would read as "this trace
        does not exist".
        """
        namespace = build_parser().parse_args(["doctor", "trace", TRACE_ID])
        execute_doctor(namespace)
        assert "Flight Recorder's ring" in capsys.readouterr().out


class _StartableDatabase:
    def __init__(self, rows, *, trace_column: bool = True):
        columns = _schema("jobs", "messages", "passes")
        if not trace_column:
            for key in columns:
                columns[key] = columns[key] - {"trace_context"}
        self.connection = _Connection(columns=columns, rows=rows)
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        pass

    async def acquire(self, workload):
        return self.connection

    async def release(self, workload, connection):
        pass


class _Runner:
    def __init__(self, database, schema="wreath"):
        self._db = database
        self._schema = schema
        self._passes: list = []


class _Application:
    def __init__(self, database):
        self._databases = {"main": database}
        self._job_runners = {"work": _Runner(database)}


class TestWreathJobsPrintsTheTrace:
    @pytest.fixture
    def application(self, monkeypatch):
        database = _StartableDatabase(
            rows=(
                (
                    "jobs",
                    [
                        {
                            "id": 7, "queue": "work", "task": "send_receipt",
                            "tenant": "", "state": "dead", "attempts": 5,
                            "max_attempts": 5, "run_at": "2026-08-01",
                            "updated_at": "2026-08-01", "last_error": "boom",
                            "trace_context": PARENT,
                        },
                        {
                            "id": 8, "queue": "work", "task": "sweep",
                            "tenant": "", "state": "dead", "attempts": 5,
                            "max_attempts": 5, "run_at": "2026-08-01",
                            "updated_at": "2026-08-01", "last_error": "nope",
                            "trace_context": None,
                        },
                        {
                            "id": 9, "queue": "work", "task": "reindex",
                            "tenant": "", "state": "ready", "attempts": 0,
                            "max_attempts": 5, "run_at": "2026-08-01",
                            "updated_at": "2026-08-01", "last_error": None,
                            "trace_context": PARENT,
                        },
                        # Untraced *and* not dead. The explanation of why a row
                        # has no trace is worth a line on a dead letter, where
                        # somebody is looking for a cause, and is noise on a job
                        # that has not failed -- so this row must produce none.
                        {
                            "id": 10, "queue": "work", "task": "warm_cache",
                            "tenant": "", "state": "ready", "attempts": 0,
                            "max_attempts": 5, "run_at": "2026-08-01",
                            "updated_at": "2026-08-01", "last_error": None,
                            "trace_context": None,
                        },
                    ],
                ),
            )
        )
        monkeypatch.setattr(
            "wreath._cli.load_application",
            lambda target, factory=False: _Application(database),
        )
        return database

    def test_a_failed_row_prints_the_trace_id_and_how_to_use_it(
        self, application, capsys
    ):
        namespace = build_parser().parse_args(["jobs", "list", "app:app"])
        assert execute_jobs(namespace) == 0
        out = capsys.readouterr().out

        assert "send_receipt" in out
        # No tenant on this row, so the label is bare. Asserted as an absence
        # because "send_receipt" is a substring of "send_receipt@acme": the
        # positive check alone cannot tell the two renderings apart.
        assert "send_receipt@" not in out
        assert f"trace: {TRACE_ID}" in out
        assert f"wreath doctor trace {TRACE_ID}" in out
        # Exactly one of the three rows lacks a trace, so exactly one gets the
        # "why it has none" line. A count, not a membership test: the guard that
        # withholds it from a traced row is only visible as an absence.
        assert out.count("trace: none") == 1

    def test_a_dead_row_with_no_trace_says_why_rather_than_going_quiet(
        self, application, capsys
    ):
        """Two different absences, and the operator has to be able to tell.

        A job enqueued outside a traced request and a database still on the
        schema version before the column both read as `None` here. Printing
        nothing would leave the reader thinking the tool had failed.
        """
        namespace = build_parser().parse_args(["jobs", "list", "app:app"])
        execute_jobs(namespace)
        out = capsys.readouterr().out

        assert "trace: none" in out
        assert "predates the trace_context column" in out
        # Two of the three rows carry an error; the third has none and must
        # render no line at all rather than "error: None".
        assert out.count("error: ") == 2

    def test_json_carries_both_the_traceparent_and_the_id(
        self, application, capsys
    ):
        namespace = build_parser().parse_args(["jobs", "list", "app:app", "--json"])
        execute_jobs(namespace)
        body = json.loads(capsys.readouterr().out)

        assert body["jobs"][0]["trace_context"] == PARENT
        assert body["jobs"][0]["trace_id"] == TRACE_ID
        assert body["jobs"][1]["trace_id"] is None
        assert body["jobs"][0]["run_at"] == "2026-08-01"
        # The projection asked for the column, which is the half a fake cannot
        # infer from the value: it hands back whatever dict it holds.
        read = next(q for q in application.connection.queries if ".jobs" in q)
        assert "trace_context" in read

    def test_the_default_is_the_dead_letters(self, application, capsys):
        namespace = build_parser().parse_args(["jobs", "list", "app:app"])
        execute_jobs(namespace)
        read = [q for q in application.connection.queries if "pg_attribute" not in q]
        assert any("state IN ($1)" in q for q in read)

    def test_all_reads_every_state(self, application, capsys):
        namespace = build_parser().parse_args(["jobs", "list", "app:app", "--all"])
        execute_jobs(namespace)
        read = [q for q in application.connection.queries if "pg_attribute" not in q]
        assert not any("state IN" in q for q in read)
        # No clauses means no `WHERE` at all, not an empty one. A fake will
        # accept `... FROM jobs WHERE  ORDER BY ...`; a server will not.
        assert not any(" WHERE " in q for q in read)

    def test_repeating_state_widens_the_filter(self, application, capsys):
        namespace = build_parser().parse_args(
            ["jobs", "list", "app:app", "--state", "dead", "--state", "ready"]
        )
        execute_jobs(namespace)
        read = [q for q in application.connection.queries if "pg_attribute" not in q]
        assert any("state IN ($1, $2)" in q for q in read)

    def test_a_queue_filter_is_a_bind_not_an_interpolation(self, application, capsys):
        namespace = build_parser().parse_args(
            ["jobs", "list", "app:app", "--queue", "work"]
        )
        execute_jobs(namespace)
        sql, args = next(
            (sql, args)
            for sql, args in application.connection.reads
            if ".jobs" in sql
        )
        assert "queue = $2" in sql
        assert "work" in args

    def test_an_empty_queue_says_which_states_it_looked_at(self, monkeypatch, capsys):
        database = _StartableDatabase(rows=())
        monkeypatch.setattr(
            "wreath._cli.load_application",
            lambda target, factory=False: _Application(database),
        )
        execute_jobs(build_parser().parse_args(["jobs", "list", "app:app"]))
        assert "no jobs in the queue (dead)" in capsys.readouterr().out

    def test_an_empty_queue_with_all_says_any_state(self, monkeypatch, capsys):
        database = _StartableDatabase(rows=())
        monkeypatch.setattr(
            "wreath._cli.load_application",
            lambda target, factory=False: _Application(database),
        )
        execute_jobs(build_parser().parse_args(["jobs", "list", "app:app", "--all"]))
        assert "no jobs in the queue (any state)" in capsys.readouterr().out

    def test_a_tenant_qualifies_the_task_label(self, monkeypatch, capsys):
        database = _StartableDatabase(
            rows=(
                (
                    "jobs",
                    [{
                        "id": 7, "queue": "work", "task": "send_receipt",
                        "tenant": "acme", "state": "dead", "attempts": 5,
                        "max_attempts": 5, "run_at": "2026-08-01",
                        "updated_at": "2026-08-01", "last_error": "boom",
                        "trace_context": PARENT,
                    }],
                ),
            )
        )
        monkeypatch.setattr(
            "wreath._cli.load_application",
            lambda target, factory=False: _Application(database),
        )
        execute_jobs(build_parser().parse_args(["jobs", "list", "app:app"]))
        out = capsys.readouterr().out
        assert "send_receipt@acme" in out
        assert "error: boom" in out

    def test_a_build_meeting_a_version_one_schema_reads_untraced(
        self, monkeypatch, capsys
    ):
        """Losing the trace is a degradation; failing the read is not."""
        database = _StartableDatabase(
            rows=(
                (
                    "jobs",
                    [{
                        "id": 7, "queue": "work", "task": "send_receipt",
                        "tenant": "", "state": "dead", "attempts": 5,
                        "max_attempts": 5, "run_at": "2026-08-01",
                        "updated_at": "2026-08-01", "last_error": "boom",
                    }],
                ),
            ),
            trace_column=False,
        )
        monkeypatch.setattr(
            "wreath._cli.load_application",
            lambda target, factory=False: _Application(database),
        )
        execute_jobs(build_parser().parse_args(["jobs", "list", "app:app", "--json"]))
        body = json.loads(capsys.readouterr().out)

        assert body["jobs"][0]["trace_id"] is None
        read = next(q for q in database.connection.queries if ".jobs" in q)
        assert "trace_context" not in read

    def test_an_application_with_no_runner_is_refused(self, monkeypatch):
        from wreath._cli import CliError

        class _Empty:
            _databases: dict = {}
            _job_runners: dict = {}

        monkeypatch.setattr(
            "wreath._cli.load_application", lambda target, factory=False: _Empty()
        )
        with pytest.raises(CliError):
            execute_jobs(build_parser().parse_args(["jobs", "list", "app:app"]))


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_a_trace_lookup_joins_four_subsystems_on_a_seeded_database() -> None:
    """The whole point of the plan, asserted end to end against a real server.

    One trace id, written onto a job, a durable message, a workflow instance and
    a pass by four different subsystems that share no code, and one query that
    finds all four.
    """
    from wreath.postgres import Database

    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    schema = f"wreath_trace_lookup_{worker}"
    workflow_schema = f"{schema}_wf"

    database = Database("test", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        from wreath._passes import ledger as _ledger
        from wreath.jobs import JobRunner
        from wreath.messaging import MessageBus
        from wreath.workflows import PostgresWorkflowStore

        runner = JobRunner(database, name="work", schema=schema)
        bus = MessageBus(database, name="events", schema=schema)

        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await connection.execute(f'DROP SCHEMA IF EXISTS "{workflow_schema}" CASCADE')
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            await connection.execute(f'CREATE SCHEMA "{workflow_schema}"')
            for component in (runner.component(), bus.component(), _ledger.component(schema)):
                for statement in component.statements():
                    await connection.execute(statement)
            for statement in PostgresWorkflowStore.schema_sql(
                schema=workflow_schema
            ).split(";\n"):
                if statement.strip():
                    await connection.execute(statement.strip())

            await connection.execute(
                f'INSERT INTO "{schema}".jobs '
                "(queue, task, args, tenant, state, run_at, max_attempts, "
                "last_error, trace_context) "
                "VALUES ('work', 'send_receipt', '[]'::jsonb, '', 'dead', now(), "
                "5, 'boom', $1)",
                PARENT,
            )
            await connection.execute(
                f'INSERT INTO "{schema}".messages '
                '(channel, "group", payload, tenant, state, trace_context) '
                "VALUES ('orders', 'billing', '{}'::jsonb, '', 'dead', $1)",
                PARENT,
            )
            await connection.execute(
                f'INSERT INTO "{schema}".passes '
                "(name, tenant, phase, chunk_limit, trace_context) "
                "VALUES ('normalize_grades', '', 'blocked', 1000, $1)",
                PARENT,
            )
            await connection.execute(
                f'INSERT INTO "{workflow_schema}".workflow_steps_instances '
                "(key, tenant, workflow, steps, trace_context) "
                "VALUES ('checkout:1', '', 'checkout', '[]'::jsonb, $1)",
                PARENT,
            )

            lookup = await find_work_with_trace(
                connection, TRACE_ID, schema=schema,
                workflow_schema=workflow_schema,
            )
        finally:
            await database.release("write", connection)

        assert {item.kind for item in lookup.work} == {
            "job", "message", "pass", "workflow"
        }, f"found only {[item.kind for item in lookup.work]}"
        assert all(item.traceparent == PARENT for item in lookup.work)
        # Only the ephemeral bus remains unsearchable, and it says so.
        assert [n for n in lookup.omitted] == [
            n for n in lookup.omitted if "ephemeral" in n
        ]
    finally:
        await database.stop()
