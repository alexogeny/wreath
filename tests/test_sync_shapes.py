from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from wreath.sync import Delta, Snapshot, Sync, SyncError, UnboundedShape, sync_events


def _native_sync_row_type():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class NativeSyncRow(Model, table="native_sync_rows"):
        id: Mapped[int] = column(Int64, primary_key=True)
        caption: Mapped[str] = column(Text)

    return NativeSyncRow


class FakeColumn:
    def __init__(self, index: int, name: str) -> None:
        self.index = index
        self.python_name = name


class Row:
    """The parts of an ORM instance `wreath.sync` actually reads."""

    __wreath_columns__ = (FakeColumn(0, "id"), FakeColumn(1, "caption"))

    def __init__(self, id: str, caption: str) -> None:
        self.id = id
        self.caption = caption

    def _orm_is_loaded(self, index: int) -> bool:
        return True

    def _orm_is_null(self, index: int) -> bool:
        return False

    def _orm_get(self, index: int):
        return (self.id, self.caption)[index]

    def _orm_primary_key(self):
        return (self.id,)


class Select:
    """A stand-in carrying the one attribute the bound check reads."""

    def __init__(self, limit: int | None = None, marker: str = "") -> None:
        self.limit_ = limit
        self.marker = marker


class FakeSession:
    """Answers `fetch` from a list the test replaces between evaluations."""

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.queries: list[Select] = []

    async def fetch(self, query):
        self.queries.append(query)
        return list(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class BrokenSession(FakeSession):
    """A session whose every evaluation raises, for the degradation path."""

    async def fetch(self, query):
        raise RuntimeError("column photos.caption does not exist")


@dataclass
class FakePrincipal:
    sub: str


def photo_sync(**kwargs) -> Sync:
    return Sync(Row, key=lambda row: row.id, **kwargs)


def as_session(session):
    """`sync_events` takes a factory returning an async context manager."""
    return lambda: session


def test_unbounded_shape_is_refused_where_it_is_declared():
    sync = photo_sync()

    with pytest.raises(UnboundedShape) as caught:

        @sync.shape("everything")
        def everything(principal):
            return Select(limit=None)

    # The message has to name the fix, because the reader is looking at a query
    # that works fine today against a small table.
    assert "declares no limit" in str(caught.value)
    assert ".limit(n)" in str(caught.value)
    assert "everything" not in sync.shapes


def test_a_shape_above_max_rows_is_refused_as_a_memory_bound():
    sync = photo_sync(max_rows=10)

    with pytest.raises(UnboundedShape) as caught:
        sync.add_shape("wide", lambda principal: Select(limit=50))

    assert "max_rows=10" in str(caught.value)
    assert "memory bound" in str(caught.value)


def test_a_bounded_shape_is_accepted_and_keeps_its_limit():
    sync = photo_sync()

    @sync.shape("mine")
    def mine(principal):
        return Select(limit=25)

    assert sync.shapes["mine"].limit == 25
    assert sync.get("mine").build is mine


def test_a_shape_needing_a_live_principal_may_declare_its_bound():
    sync = photo_sync()

    def mine(principal):
        return Select(limit=5, marker=principal.sub)  # raises on None

    with pytest.raises(UnboundedShape) as caught:
        sync.add_shape("mine", mine)
    assert "add_shape(..., limit=N)" in str(caught.value)

    shape = sync.add_shape("mine", mine, limit=5)
    assert shape.limit == 5


def test_a_duplicate_shape_name_is_refused():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    with pytest.raises(SyncError, match="already declared"):
        sync.add_shape("mine", lambda principal: Select(limit=5))


def test_an_unknown_shape_names_the_declared_ones():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    with pytest.raises(SyncError) as caught:
        sync.get("theirs")
    assert "declared: mine" in str(caught.value)


def test_max_rows_must_be_positive():
    with pytest.raises(ValueError, match="max_rows"):
        photo_sync(max_rows=0)


@pytest.mark.parametrize("limit", [0, -1])
def test_an_explicitly_declared_limit_must_be_positive(limit):
    sync = photo_sync()
    with pytest.raises(UnboundedShape, match="positive limit"):
        sync.add_shape("mine", lambda principal: Select(limit=5), limit=limit)


def test_a_principal_is_keyed_by_sub_then_id_then_its_string():
    from wreath.sync import _principal_id

    @dataclass
    class ById:
        id: int

    assert _principal_id(FakePrincipal("alice")) == "alice"
    assert _principal_id(ById(7)) == "7"
    assert _principal_id("anonymous") == "anonymous"


async def test_evaluate_returns_rows_and_an_authoritative_key_set():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one"), Row("b", "two")])

    result = await sync.evaluate(session, "mine", FakePrincipal("alice"))

    assert isinstance(result, Snapshot)
    assert result.keys == ("a", "b")
    assert [row["values"]["caption"] for row in result.rows] == ["one", "two"]


async def test_evaluation_truncates_to_the_declared_bound():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=2))
    session = FakeSession([Row(str(n), "x") for n in range(10)])

    result = await sync.evaluate(session, "mine", FakePrincipal("alice"))

    assert len(result.rows) == 2


async def test_the_shape_is_rebuilt_on_every_evaluation():
    sync = photo_sync()
    calls: list[str] = []

    def mine(principal):
        calls.append(principal.sub)
        return Select(limit=5)

    sync.add_shape("mine", mine, limit=5)
    session = FakeSession([])

    await sync.evaluate(session, "mine", FakePrincipal("alice"))
    await sync.evaluate(session, "mine", FakePrincipal("alice"))

    assert calls == ["alice", "alice"], "a cached Select would evaluate stale policy"


async def test_a_row_leaving_the_shape_produces_a_tombstone():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one"), Row("b", "two")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")

    await subscription.snapshot(session)
    assert subscription.held == {"a", "b"}

    # `b`'s owner changed, so the shape -- which filters on owner -- no longer
    # returns it. Nothing deleted the row.
    session.rows = [Row("a", "one")]
    delta = await subscription.poll(session)

    assert delta.removed == ("b",), "the revoked row must be tombstoned"
    assert delta.upserted == ()
    assert subscription.held == {"a"}


async def test_a_changed_row_is_upserted_and_an_unchanged_one_is_not():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one"), Row("b", "two")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    await subscription.snapshot(session)

    session.rows = [Row("a", "one"), Row("b", "TWO")]
    delta = await subscription.poll(session)

    assert delta.removed == ()
    assert [row["key"] for row in delta.upserted] == ["b"]


async def test_a_poll_with_nothing_moved_is_falsy():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    await subscription.snapshot(session)

    delta = await subscription.poll(session)

    assert not delta
    assert delta == Delta((), ())


async def test_a_row_falling_out_of_the_ordered_window_is_also_a_tombstone():
    sync = photo_sync()
    sync.add_shape("recent", lambda principal: Select(limit=2))
    session = FakeSession([Row("a", "1"), Row("b", "2")])
    subscription = sync.subscribe(FakePrincipal("alice"), "recent")
    await subscription.snapshot(session)

    session.rows = [Row("c", "3"), Row("a", "1")]
    delta = await subscription.poll(session)

    assert delta.removed == ("b",)
    assert [row["key"] for row in delta.upserted] == ["c"]


async def test_a_snapshot_adopts_the_result_as_what_the_client_holds():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one"), Row("b", "two")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    await subscription.snapshot(session)

    session.rows = [Row("c", "three")]
    await subscription.snapshot(session)

    assert subscription.held == {"c"}


async def test_the_registry_refuses_past_its_per_principal_cap():
    sync = photo_sync(max_per_principal=2)
    sync.add_shape("mine", lambda principal: Select(limit=5))
    principal = FakePrincipal("alice")

    first = sync.subscribe(principal, "mine")
    second = sync.subscribe(principal, "mine")
    third = sync.subscribe(principal, "mine")

    assert first is not None and second is not None
    assert third is None, "refused rather than evicting somebody else's tab"
    assert sync.subscribers == 2


async def test_closing_a_subscription_releases_its_slot():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    assert sync.subscribers == 1

    subscription.close()

    assert sync.subscribers == 0
    assert subscription.closed


async def test_the_stream_opens_with_a_snapshot_then_emits_a_delta():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    events = sync_events(subscription, as_session(session), keepalive=5.0)

    first = await anext(events)
    assert first.event == "snapshot"
    assert '"a"' in first.data

    session.rows = [Row("a", "one"), Row("b", "two")]
    sync.notify_all("write")
    second = await asyncio.wait_for(anext(events), 2.0)

    assert second.event == "delta"
    assert '"b"' in second.data
    await events.aclose()


async def test_an_idle_stream_emits_a_keepalive_comment():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    events = sync_events(subscription, as_session(session), keepalive=0.05)
    await anext(events)

    event = await asyncio.wait_for(anext(events), 2.0)

    assert event.comment == "keepalive"
    await events.aclose()


async def test_a_failing_evaluation_is_counted_and_the_stream_survives():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    sessions: list[FakeSession] = [FakeSession([Row("a", "one")])]
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    events = sync_events(subscription, lambda: sessions[0], keepalive=0.05)
    await anext(events)
    assert sync.stale_evaluations() == 0

    sessions[0] = BrokenSession([])
    sync.notify_all("write")

    # The stream does not end: the next thing it yields is an ordinary
    # keepalive, and the failure has been counted rather than swallowed.
    event = await asyncio.wait_for(anext(events), 2.0)
    assert event.comment == "keepalive"
    assert sync.stale_evaluations() == 1

    await events.aclose()


async def test_stream_closes_when_the_document_does():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    events = sync_events(subscription, as_session(session), keepalive=5.0)
    await anext(events)

    sync.close_all()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(events), 2.0)
    assert sync.subscribers == 0


async def test_a_closed_stream_releases_its_slot_on_disconnect():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    events = sync_events(subscription, as_session(session), keepalive=5.0)
    await anext(events)
    assert sync.subscribers == 1

    await events.aclose()  # what a client disconnect does

    assert sync.subscribers == 0


async def test_the_default_key_is_the_primary_key():
    sync = Sync(Row)  # no key=
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one"), Row("b", "two")])

    result = await sync.evaluate(session, "mine", FakePrincipal("alice"))

    assert result.keys == ("a", "b")


def test_native_model_snapshot_materializes_the_public_boundary_once():
    NativeSyncRow = _native_sync_row_type()
    sync = Sync(NativeSyncRow)

    result = sync._snapshot(
        [NativeSyncRow(id=7, caption="seven"), NativeSyncRow(id=9, caption="nine")]
    )

    assert result == Snapshot(
        rows=(
            {"key": "7", "values": {"id": 7, "caption": "seven"}},
            {"key": "9", "values": {"id": 9, "caption": "nine"}},
        ),
        keys=("7", "9"),
    )


def test_a_native_model_with_a_custom_key_uses_the_public_snapshot_path():
    NativeSyncRow = _native_sync_row_type()
    sync = Sync(NativeSyncRow, key=lambda row: f"custom-{row.id}")

    result = sync._snapshot([NativeSyncRow(id=7, caption="seven")])

    assert result.keys == ("custom-7",)


def test_a_non_model_duck_type_uses_the_public_snapshot_path():
    from wreath.orm import Model

    class DuckRow(Row):
        pass

    primary_key = DuckRow._orm_primary_key
    DuckRow._orm_primary_key = Model._orm_primary_key
    sync = Sync(DuckRow)
    DuckRow._orm_primary_key = primary_key

    result = sync._snapshot([DuckRow("a", "one")])

    assert result.keys == ("a",)


def test_native_model_snapshot_keeps_the_missing_primary_key_refusal():
    NativeSyncRow = _native_sync_row_type()
    sync = Sync(NativeSyncRow)

    with pytest.raises(SyncError, match="select the key column, or pass key="):
        sync._snapshot([NativeSyncRow._orm_new()])


def test_compiled_model_primary_key_override_keeps_the_public_path():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Overridden(Model, table="overridden_sync_rows"):
        id: Mapped[int] = column(Int64, primary_key=True)
        caption: Mapped[str] = column(Text)

        def _orm_primary_key(self):
            return ("custom", self.id)

    result = Sync(Overridden)._snapshot([Overridden(id=7, caption="seven")])

    assert result.keys == ("custom:7",)


async def test_a_supplied_key_is_used_instead_of_the_primary_key():
    sync = Sync(Row, key=lambda row: f"photo-{row.id}")
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one")])

    result = await sync.evaluate(session, "mine", FakePrincipal("alice"))

    assert result.keys == ("photo-a",)


async def test_a_composite_primary_key_becomes_one_colon_joined_key():

    class Composite(Row):
        def _orm_primary_key(self):
            return (self.id, "eu")

    sync = Sync(Composite)
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Composite("a", "one")])

    result = await sync.evaluate(session, "mine", FakePrincipal("alice"))

    assert result.keys == ("a:eu",)


def test_the_doorbell_channel_is_derived_from_the_model_or_given():
    assert photo_sync().document.channel == "wreath_sync_row"
    assert photo_sync(channel="photos_feed").document.channel == "photos_feed"


def test_a_bool_limit_is_not_a_limit():
    sync = photo_sync()

    with pytest.raises(UnboundedShape, match="declares no limit"):
        sync.add_shape("mine", lambda principal: Select(limit=True))


async def test_a_stream_with_no_keepalive_takes_the_documents_default():
    sync = photo_sync(keepalive=0.05)
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")

    assert subscription.keepalive == 0.05

    events = sync_events(subscription, as_session(session))  # no keepalive=
    assert (await anext(events)).event == "snapshot"

    # Reached only if the stream inherited 0.05 rather than waiting on `None`,
    # which would block here forever.
    event = await asyncio.wait_for(anext(events), 2.0)

    assert event.comment == "keepalive"
    await events.aclose()


async def test_a_wake_up_that_moved_nothing_emits_no_event():
    sync = photo_sync()
    sync.add_shape("mine", lambda principal: Select(limit=5))
    session = FakeSession([Row("a", "one")])
    subscription = sync.subscribe(FakePrincipal("alice"), "mine")
    events = sync_events(subscription, as_session(session), keepalive=0.05)
    await anext(events)

    sync.notify_all("write")  # nothing in `session.rows` changed

    # The next thing the client sees is an idle tick, not an empty delta.
    event = await asyncio.wait_for(anext(events), 2.0)
    assert event.comment == "keepalive"
    assert event.event is None
    await events.aclose()


def test_a_row_version_is_stable_and_order_independent():
    from wreath.sync import _version_of

    assert _version_of({}) == "cae66941d9efbd404e4d88758ea67670"
    assert _version_of({"a": 1, "b": "x"}) == "3e154a17e5f4cbc40b83a8f4ff5168de"
    assert _version_of({"a": 1, "b": "x"}) == _version_of({"b": "x", "a": 1})
    assert _version_of({"a": 1}) != _version_of({"a": 2})


def test_native_sync_digest_lanes_match_the_scalar_tail_across_blocks():
    from wreath._native import _core

    rows = tuple(
        {
            "key": f"row-{index}",
            "values": {
                "caption": (f"value-{index}-" * 40),
                "ordinal": index,
            },
        }
        for index in range(4)
    )
    held = _core.sync_state(rows)
    _state, upserted, removed = _core.sync_state_diff(held, rows[:3])

    assert upserted == ()
    assert removed == ("row-3",)


def test_a_row_without_a_primary_key_is_refused_by_the_default_key():
    from wreath.sync import _default_key

    class Keyless(Row):
        def _orm_primary_key(self):
            return None

    with pytest.raises(SyncError, match="no loaded primary key"):
        _default_key(Keyless("a", "one"))
