"""The record an erasure leaves, and the refusal that keeps it honest.

The live suite proves the row lands in PostgreSQL. These prove the decisions
around it, which is where the compliance argument actually lives: that a record
is written only when the ledger says every walk finished, that it carries the
subject and the digest and *nothing about what was erased*, that a redelivered
job does not produce a second one, and that the read and the write share one
transaction so a completion cannot be established on one connection and
recorded on another.

A fake driver, deliberately. Everything asserted here is a decision this module
makes; a real database would only be able to confirm that PostgreSQL still
implements INSERT.
"""

from __future__ import annotations

from typing import Any

import pytest
from _pgfidelity import check_for

from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.privacy import Erase, ErasureIncomplete, Privacy, record_erasure


class FakeDatabase:
    """Enough of `wreath.postgres.Database` for `Registry` and the recorder."""

    name = "main"

    def __init__(self, connection: Any = None) -> None:
        self.connection = connection
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, workload: str) -> Any:
        self.acquired.append(workload)
        return self.connection

    async def release(self, workload: str, connection: Any) -> None:
        self.released.append(workload)


class FakeTransaction:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeTransaction:
        self.connection.opened += 1
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.connection.closed += 1
        return False

    async def fetch(self, sql: str, *args: object) -> list[tuple]:
        check_for(self, sql, args)
        self.connection.statements.append((sql, args))
        return self.connection.ledger

    async def fetchval(self, sql: str, *args: object) -> object:
        check_for(self, sql, args)
        self.connection.statements.append((sql, args))
        return 1 if self.connection.already else None

    async def fetchrow(self, sql: str, *args: object) -> tuple:
        check_for(self, sql, args)
        self.connection.appended.append((sql, args))
        return (7, 1)


class FakeConnection:
    """One connection whose transaction records every statement it was given."""

    def __init__(self, ledger: list[tuple], *, already: bool = False) -> None:
        self.ledger = ledger
        self.already = already
        self.statements: list[tuple] = []
        self.appended: list[tuple] = []
        self.opened = 0
        self.closed = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)


class Person(Model, table="people"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    email: Mapped[str] = column(Text)


class Photo(Model, table="photos"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    owner_id: Mapped[int | None] = column(
        Int64, references=Person.id, on_delete="set null", nullable=True
    )
    caption: Mapped[str] = column(Text)


@pytest.fixture
def privacy() -> Privacy:
    registry = Registry(FakeDatabase(), [Person, Photo], validate_schema="off")
    item = Privacy(registry)
    item.subject(Person, key="id", delete=True)
    item.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
    return item


def _done(names: list[str], rows: int = 4) -> list[tuple]:
    return [(name, "done", rows) for name in names]


async def test_a_finished_erasure_appends_one_row_with_the_counts(
    privacy: Privacy,
) -> None:
    prepared = privacy.prepare("4711")
    names = [walk.name for walk in prepared.passes]
    connection = FakeConnection(_done(names, rows=4))

    assert await record_erasure(prepared, FakeDatabase(connection)) is True
    (_sql, values), = connection.appended
    assert values[0] == "4711", "the stream is the subject"
    assert prepared.plan.digest in values
    assert values[-2:] == (len(names), 4 * len(names))


async def test_the_record_carries_the_subject_and_the_digest_and_no_values(
    privacy: Privacy,
) -> None:
    """A record of *what* was erased would be a re-identification store.

    Asserted over the whole bound parameter list rather than over one field:
    the failure this guards against is a column somebody adds later, and a
    positional check on two fields would not see it.
    """
    prepared = privacy.prepare("4711")
    connection = FakeConnection(_done([walk.name for walk in prepared.passes]))
    await record_erasure(prepared, FakeDatabase(connection))

    (_sql, values), = connection.appended
    assert values == (
        "4711",
        "Person",
        "id",
        prepared.plan.digest,
        2,
        8,
    )


async def test_nothing_is_recorded_when_a_pass_never_ran(privacy: Privacy) -> None:
    """The refusal that makes the record worth reading at all."""
    prepared = privacy.prepare("4711")
    connection = FakeConnection([])

    with pytest.raises(ErasureIncomplete) as caught:
        await record_erasure(prepared, FakeDatabase(connection))
    assert "2 of 2 pass(es)" in str(caught.value)
    assert connection.appended == []


async def test_nothing_is_recorded_when_a_pass_stopped_part_way(
    privacy: Privacy,
) -> None:
    """`blocked` is not `done`, and the difference is the whole point.

    This is the shape the module actually shipped with: a pass that halted on a
    foreign-key violation while `erase` returned its prepared plan and said
    nothing. The subject would have been told they were erased.
    """
    prepared = privacy.prepare("4711")
    names = [walk.name for walk in prepared.passes]
    connection = FakeConnection([(names[0], "done", 3), (names[1], "blocked", 0)])

    with pytest.raises(ErasureIncomplete) as caught:
        await record_erasure(prepared, FakeDatabase(connection))
    assert names[1] in str(caught.value)
    assert "1 of 2" in str(caught.value)
    assert connection.appended == []


async def test_a_redelivered_erasure_does_not_record_itself_twice(
    privacy: Privacy,
) -> None:
    """Job delivery is at-least-once; two receipts for one erasure is a lie."""
    prepared = privacy.prepare("4711")
    connection = FakeConnection(
        _done([walk.name for walk in prepared.passes]), already=True
    )

    assert await record_erasure(prepared, FakeDatabase(connection)) is False
    assert connection.appended == []


async def test_the_completion_read_and_the_append_share_one_transaction(
    privacy: Privacy,
) -> None:
    """A completion read on another connection is one that could have moved.

    The property is not decorative: the ledger row a pass writes is what says
    the walk finished, and reading it outside the record's transaction would
    let another worker reopen the pass between the read and the insert.
    """
    prepared = privacy.prepare("4711")
    connection = FakeConnection(_done([walk.name for walk in prepared.passes]))
    await record_erasure(prepared, FakeDatabase(connection))

    assert connection.opened == 1
    assert connection.closed == 1
    ledger_read = connection.statements[0][0]
    assert "passes" in ledger_read and "rows_done" in ledger_read


async def test_the_ledger_read_names_the_erasures_own_passes(privacy: Privacy) -> None:
    """Every declared pass, and nothing else -- a `LIKE` would sweep in siblings."""
    prepared = privacy.prepare("4711")
    names = [walk.name for walk in prepared.passes]
    connection = FakeConnection(_done(names))
    await record_erasure(prepared, FakeDatabase(connection))

    sql, values = connection.statements[0]
    assert values == tuple(names)
    assert sql.count("$") == len(names)


async def test_the_record_reads_the_ledger_the_passes_wrote_to(
    privacy: Privacy,
) -> None:
    """One `schema=`, so a record cannot be derived from somebody else's ledger."""
    prepared = privacy.prepare("4711", schema="tenant_seven")
    connection = FakeConnection(_done([walk.name for walk in prepared.passes]))
    await record_erasure(prepared, FakeDatabase(connection))

    assert "tenant_seven" in connection.statements[0][0]
    assert "tenant_seven" in connection.appended[0][0]


async def test_an_erasure_with_no_passes_is_recorded_without_a_ledger_read(
    orm_only: Privacy,
) -> None:
    """Nothing to erase is still an answer, and the answer is evidence too.

    A subject-access-style registry that classifies nothing produces a plan
    with no writes; recording it says "we looked and there was nothing", which
    is a different fact from never having looked.
    """
    prepared = orm_only.prepare("4711")
    assert prepared.passes == ()
    connection = FakeConnection([])
    assert await record_erasure(prepared, FakeDatabase(connection)) is True
    assert len(connection.appended) == 1
    assert connection.appended[0][1][-2:] == (0, 0)


@pytest.fixture
def orm_only() -> Privacy:
    registry = Registry(FakeDatabase(), [Person, Photo], validate_schema="off")
    item = Privacy(registry)
    item.subject(Person, key="id")
    return item


class FakeWalk:
    """Enough of a `ChunkedPass` to see whether `erase` drove it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.runs = 0

    async def run(self, database: object) -> object:
        self.runs += 1
        return object()


async def test_erase_drives_every_pass_and_skips_the_steps_that_have_none(
    privacy: Privacy,
) -> None:
    """A `CASCADE` or `RETAIN` step is listed and must not be driven.

    Driving one would issue a delete the plan did not promise, on top of the
    delete the database already does.
    """
    from wreath._privacy.execute import PreparedErasure

    prepared = privacy.prepare("4711")
    walks = [FakeWalk(step.name) for step in prepared.passes]
    steps = tuple(
        (action, walks.pop(0) if step is not None else None)
        for action, step in prepared.steps
    )
    driven = PreparedErasure(
        plan=prepared.plan,
        steps=steps,
        record=prepared.record,
        schema=prepared.schema,
        workload=prepared.workload,
    )
    connection = FakeConnection(_done([walk.name for _a, walk in steps if walk]))
    await record_erasure(driven, FakeDatabase(connection))

    assert [walk.runs for _action, walk in steps if walk is not None] == [0, 0]
    assert len(connection.appended) == 1


async def test_erase_runs_the_passes_then_records(privacy: Privacy) -> None:
    """End to end on a fake driver: every walk driven once, one receipt."""
    driven: list[str] = []

    class Walk:
        def __init__(self, inner: object) -> None:
            self.inner = inner
            self.name = inner.name

        async def run(self, database: object) -> object:
            driven.append(self.name)
            return object()

    prepared = privacy.prepare("4711")
    names = [walk.name for walk in prepared.passes]
    connection = FakeConnection(_done(names))
    database = FakeDatabase(connection)

    from wreath._privacy import execute as execute_module

    original = execute_module.prepare

    def _wrapped(*args: object, **kwargs: object) -> object:
        made = original(*args, **kwargs)
        return execute_module.PreparedErasure(
            plan=made.plan,
            steps=tuple(
                (action, None if step is None else Walk(step))
                for action, step in made.steps
            ),
            record=made.record,
            schema=made.schema,
            workload=made.workload,
        )

    execute_module.prepare = _wrapped
    try:
        await privacy.erase(database, "4711")
    finally:
        execute_module.prepare = original

    assert driven == names
    assert len(connection.appended) == 1


def test_the_read_side_binds_a_database_and_the_write_side_never_does() -> None:
    """Retention and inspection are pooled work; a receipt is not.

    `ErasureRecord` holds no `Database` on purpose -- a write that *could*
    reach for a pooled connection is a write that could commit on its own, and
    the record's whole value is that it cannot. `bind` is the one place a
    database belongs, and `purge()` lives there.
    """
    from wreath._privacy.record import ERASURE_TABLE, ErasureRecord

    registry = Registry(FakeDatabase(), [Person, Photo], validate_schema="off")
    log = Privacy(registry, erasure_record_retain=86400).erasure_records(
        FakeDatabase()
    )
    assert ERASURE_TABLE in log.table
    assert "DELETE FROM" in log.sql("purge")
    assert not hasattr(ErasureRecord(), "purge")


def test_the_record_table_is_declared_with_the_retention_the_operator_set() -> None:
    """No default: the honest one is "as long as your oldest backup"."""
    from wreath._privacy.record import ErasureRecord

    assert ErasureRecord().declaration.retain is None
    assert ErasureRecord(retain=30 * 86400).declaration.retain == 30 * 86400


def test_an_unset_window_is_reported_as_unbounded_rather_than_left_silent() -> None:
    registry = Registry(FakeDatabase(), [Person, Photo], validate_schema="off")
    lines = Privacy(registry).retention()
    assert any("erasure records: UNBOUNDED" in line for line in lines)


def test_a_set_window_is_reported_as_a_number() -> None:
    registry = Registry(FakeDatabase(), [Person, Photo], validate_schema="off")
    lines = Privacy(registry, erasure_record_retain=30 * 86400).retention()
    assert any("erasure records: deleted 30d" in line for line in lines)


def test_the_ddl_declares_the_age_index_only_when_rows_are_purged() -> None:
    """An index nothing reads is an index the writes pay for anyway."""
    from wreath.privacy import schema_sql

    assert "wreath_erasures_at_idx" not in schema_sql("wreath")
    assert "wreath_erasures_at_idx" in schema_sql("wreath", retain=86400)


class Cascaded(Model, table="cascaded"):
    """Removed by the parent's own referential action: listed, never driven."""

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    owner_id: Mapped[int] = column(Int64, references=Person.id, on_delete="cascade")
    note: Mapped[str] = column(Text)


async def test_erase_skips_a_step_the_database_carries_out_itself() -> None:
    """A `CASCADE` step is in the plan and has no pass; driving one is a bug.

    It would issue a delete the plan did not promise, on top of the delete the
    database already does -- and it is the one step in a prepared erasure where
    `None` is the correct value rather than an omission.
    """
    registry = Registry(FakeDatabase(), [Person, Cascaded], validate_schema="off")
    privacy = Privacy(registry)
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Cascaded, subject="owner_id", personal={"note": Erase.REDACT})

    prepared = privacy.prepare("4711")
    dispositions = {action.table: step for action, step in prepared.steps}
    assert dispositions["cascaded"] is None
    assert dispositions["people"] is not None

    driven: list[str] = []

    class Walk:
        def __init__(self, inner: object) -> None:
            self.name = inner.name

        async def run(self, database: object) -> object:
            driven.append(self.name)
            return object()

    from wreath._privacy import execute as execute_module

    original = execute_module.prepare

    def _wrapped(*args: object, **kwargs: object) -> object:
        made = original(*args, **kwargs)
        return execute_module.PreparedErasure(
            plan=made.plan,
            steps=tuple(
                (action, None if step is None else Walk(step))
                for action, step in made.steps
            ),
            record=made.record,
            schema=made.schema,
            workload=made.workload,
        )

    execute_module.prepare = _wrapped
    connection = FakeConnection(_done([walk.name for walk in prepared.passes]))
    try:
        await privacy.erase(FakeDatabase(connection), "4711")
    finally:
        execute_module.prepare = original

    assert driven == [walk.name for walk in prepared.passes]
    assert len(connection.appended) == 1


def test_the_blocked_refusal_counts_a_cycle_only_when_it_blocks() -> None:
    """A deferrable loop is not a blocking one, and the count must not say it is."""
    from wreath._privacy.execute import ErasureBlocked

    class Household(Model, table="households_record"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        head_id: Mapped[int | None] = column(Int64, nullable=True)
        label: Mapped[str] = column(Text)

    class Member(Model, table="members_record"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        household_id: Mapped[int] = column(Int64, references=Household.id)
        person_id: Mapped[int] = column(Int64, references=Person.id)
        nickname: Mapped[str] = column(Text)

    Household.__wreath_column_map__["head_id"].references = Member.id
    registry = Registry(
        FakeDatabase(), [Person, Household, Member], validate_schema="off"
    )
    privacy = Privacy(registry)
    privacy.subject(Person, key="id")
    privacy.classify(Household, personal={"label": Erase.REDACT})
    privacy.classify(Member, personal={"nickname": Erase.REDACT})

    with pytest.raises(ErasureBlocked) as caught:
        privacy.prepare("4711")
    assert "1 blocking foreign-key cycle(s)" in str(caught.value)


def test_the_blocked_refusal_does_not_count_a_deferrable_cycle(
) -> None:
    """A loop one transaction can carry is not a reason the erasure refuses.

    The plan here is blocked by an unreachable table while a *deferrable* cycle
    is also present; counting every cycle rather than the blocking ones would
    send an operator to make a foreign key deferrable that already is.
    """
    from wreath._privacy.execute import ErasureBlocked

    class Household(Model, table="households_def_record"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        head_id: Mapped[int | None] = column(Int64, nullable=True)
        label: Mapped[str] = column(Text)

    class Member(Model, table="members_def_record"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        household_id: Mapped[int] = column(
            Int64, references=Household.id, deferrable=True
        )
        person_id: Mapped[int] = column(Int64, references=Person.id, deferrable=True)
        nickname: Mapped[str] = column(Text)

    class Loose(Model, table="loose_record"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        contact_string: Mapped[str] = column(Text)

    Household.__wreath_column_map__["head_id"].references = Member.id
    Household.__wreath_column_map__["head_id"].deferrable = True
    registry = Registry(
        FakeDatabase(), [Person, Household, Member, Loose], validate_schema="off"
    )
    privacy = Privacy(registry)
    privacy.subject(Person, key="id")
    privacy.classify(Household, personal={"label": Erase.REDACT})
    privacy.classify(Member, personal={"nickname": Erase.REDACT})
    privacy.classify(Loose, personal={"contact_string": Erase.REDACT})

    plan = privacy.plan("4711")
    assert [cycle.deferrable for cycle in plan.cycles] == [True]
    with pytest.raises(ErasureBlocked) as caught:
        privacy.prepare("4711")
    assert "1 unreachable classified table(s)" in str(caught.value)
    assert "0 blocking foreign-key cycle(s)" in str(caught.value)
