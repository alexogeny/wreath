from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath.log import Batch, Cursor, Flush, PostgresLog, Record
from wreath.streams import (
    DEFAULT_RETENTION,
    KIND_CANCELLED,
    KIND_CHUNK,
    KIND_END,
    KIND_ERROR,
    KIND_SUPERSEDED,
    KIND_TIMEOUT,
    MAX_KEY_BYTES,
    StreamCursor,
    StreamEvent,
    StreamHandle,
    Streams,
    check_stream_attachment,
    declaration,
)


class _Connection:
    """Answers the two statements `wreath.streams` writes itself.

    It dispatches on the SQL rather than on call order, and refuses anything it
    does not recognise, so a change to either statement's shape shows up here as
    a failure instead of as a double that happily answers whatever it is asked.
    """

    def __init__(self, log: _Log) -> None:
        self._log = log

    async def fetchval(self, sql: str, *args: object) -> object:
        if "max(fence)" not in sql:
            raise AssertionError(f"unexpected statement through fetchval: {sql!r}")
        if "pg_snapshot_xmin" not in sql:
            raise AssertionError(
                "the max-fence seed must be gated on the same horizon the read is, "
                f"and this one is not: {sql!r}"
            )
        rows = self._log.rows_for(str(args[0]))
        return max((row.values["fence"] for row in rows), default=0)

    async def fetchrow(self, sql: str, *args: object) -> object:
        if "ORDER BY fence DESC" not in sql:
            raise AssertionError(f"unexpected statement through fetchrow: {sql!r}")
        rows = self._log.rows_for(str(args[0]))
        if not rows:
            return None
        head = max(rows, key=lambda row: (row.values["fence"], row.values["idx"]))
        return (head.values["fence"], head.values["idx"])

    async def execute(self, sql: str, *args: object) -> str:
        if "DELETE FROM" not in sql or "fence < $2" not in sql:
            raise AssertionError(f"unexpected statement through execute: {sql!r}")
        key, fence = str(args[0]), int(args[1])  # type: ignore[arg-type]
        before = len(self._log.rows)
        self._log.rows = [
            row for row in self._log.rows if not (row.stream == key and row.values["fence"] < fence)
        ]
        return f"DELETE {before - len(self._log.rows)}"


class _Database:
    def __init__(self, log: _Log) -> None:
        self._log = log
        self.acquired = 0

    async def acquire(self, workload: str) -> _Connection:
        self.acquired += 1
        return _Connection(self._log)

    async def release(self, workload: str, connection: object) -> None:
        return None


class _Log:
    """A `PostgresLog` shaped for the reader, holding rows in commit order.

    Two knobs the real log has and a list does not: `horizon` withholds the
    newest rows the way an in-flight transaction does, and `xid` advances per
    `append_many` so the `(xid, seq)` order is a real composite rather than one
    counter wearing two names.
    """

    def __init__(self, declared) -> None:
        self.declaration = declared
        self.rows: list[Record] = []
        self._dropped = 0
        self._xid = 0
        self._seq = 0
        self.database = _Database(self)
        #: Rows appended but not yet visible, counted from the end.
        self.withheld = 0

    @property
    def table(self) -> str:
        return self.declaration.qualified_table

    @property
    def dropped(self) -> int:
        return self._dropped

    def buffered(self, stream: str):
        return PostgresLog.buffered(self, stream)  # type: ignore[arg-type]

    def rows_for(self, key: str) -> list[Record]:
        visible = self.rows[: len(self.rows) - self.withheld] if self.withheld else self.rows
        return [row for row in visible if row.stream == key]

    async def append(self, stream: str, /, *, connection=None, **values):
        await self.append_many([(stream, values)])
        return self.rows[-1].cursor

    async def append_many(self, rows, /, *, connection=None) -> int:
        self._xid += 1
        for stream, values in rows:
            self._seq += 1
            self.rows.append(
                Record(
                    cursor=Cursor(self._xid, self._seq),
                    stream=stream,
                    values={name.name: values.get(name.name) for name in self.declaration.columns},
                )
            )
        return len(rows)

    async def read(self, stream=None, *, after: Cursor, limit: int = 512) -> Batch:
        selected = [row for row in self.rows_for(str(stream)) if row.cursor > after]
        page = selected[:limit]
        return Batch(tuple(page), page[-1].cursor if page else after)


class _Runner:
    """A job runner that records registrations and runs an attempt on demand."""

    def __init__(self) -> None:
        self.tasks: dict[str, object] = {}
        self.launched: list[tuple] = []
        self.cancelled: list[str] = []
        self.cancel_result = True
        self.next_id = 1

    def task(self, name: str, *, retries: int = 5, timeout=None, **_: object):
        def register(func):
            self.tasks[name] = func
            return func

        return register

    async def launch(self, task: str, *args, key=None, tenant=""):
        from wreath.jobs import TaskHandle

        self.launched.append((task, args, key, tenant))
        handle = TaskHandle(task_id=str(self.next_id))
        self.next_id += 1
        return handle

    async def cancel(self, job_id=None, *, key=None, reason="cancelled") -> bool:
        self.cancelled.append(str(key))
        return self.cancel_result

    async def attempt(self, task: str, *args, fence: int, attempt: int) -> None:
        """Run one attempt of `task` with an explicit fence, as a worker would."""
        from wreath.jobs import JobContext

        context = JobContext(job_id=1, task=task, attempt=attempt, fence=fence, tenant="", key=None)
        await self.tasks[task](context, *args)


def _streams(**options) -> tuple[Streams, _Log, _Runner]:
    declared = declaration(schema="")
    log = _Log(declared)
    runner = _Runner()
    return Streams(jobs=runner, log=log, idle=0.05, poll=0.005, **options), log, runner


async def test_an_existing_durable_owner_can_write_one_fenced_stream() -> None:
    streams, log, _runner = _streams()

    writer = await streams.writer("chat-delivery", fence=7, attempt=1)
    await writer.write("hello")
    await writer.finish()

    rows = log.rows_for("chat-delivery")
    assert [(row.values["fence"], row.values["kind"]) for row in rows] == [
        (7, KIND_CHUNK),
        (7, KIND_END),
    ]
    assert streams.started == 1


async def test_an_external_retry_uses_the_declared_stream_retry_policy() -> None:
    streams, log, _runner = _streams(on_retry="truncate")
    first = await streams.writer("chat-delivery", fence=3, attempt=1)
    await first.write("stale")
    await first.flush()

    second = await streams.writer("chat-delivery", fence=4, attempt=2)
    await second.write("fresh")
    await second.fail("provider failed")

    rows = log.rows_for("chat-delivery")
    assert [row.values["fence"] for row in rows] == [4, 4]
    assert [row.values["kind"] for row in rows] == [KIND_CHUNK, KIND_ERROR]
    assert streams.started == 1


#: How long past its own idle window a reader may take before this suite calls
#: it a failure. Without this the two controls that make `follow` never stop --
#: resetting the deadline every iteration, and never checking it -- **hang**
#: instead of failing, and a mutation run scores a hang as undecided rather than
#: killed. A reader that does not stop is a defect, so it is timed out here and
#: reported as one.
_NEVER_STOPS = 6.0


async def _collect(streams: Streams, key: str, **options) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    try:
        async with asyncio.timeout(_NEVER_STOPS):
            async for event in streams.follow(key, **options):
                events.append(event)
    except TimeoutError:
        raise AssertionError(
            f"follow({key!r}, {options}) was still going after {_NEVER_STOPS}s and "
            f"had produced {[event.kind for event in events]}; a reader that never "
            "stops holds a connection open forever"
        ) from None
    return events


def test_a_chunk_log_may_not_be_kept_forever() -> None:
    with pytest.raises(ValueError) as raised:
        declaration(retain=None)
    assert "delivery, not transcript" in str(raised.value)
    assert "no erasure path" in str(raised.value)


@pytest.mark.parametrize("retain", [float("nan"), float("inf")])
def test_a_chunk_log_retention_must_be_finite(retain: float) -> None:
    with pytest.raises(ValueError, match="finite positive number of seconds"):
        declaration(retain=retain)


def test_a_streams_over_a_keep_forever_log_is_refused_too() -> None:
    from wreath.log import KEEP_FOREVER, Log

    log = _Log(Log(table="t", retain=KEEP_FOREVER, schema=""))
    with pytest.raises(ValueError) as raised:
        Streams(jobs=_Runner(), log=log)  # type: ignore[arg-type]
    assert "retain=KEEP_FOREVER" in str(raised.value)
    assert "no erasure path" in str(raised.value)


@pytest.mark.parametrize("retain", [float("nan"), float("inf")])
def test_streams_refuses_a_custom_log_with_non_finite_retention(retain: float) -> None:
    from wreath.log import Log

    log = _Log(Log(table="t", retain=retain, schema=""))
    with pytest.raises(ValueError, match="retain must be a finite positive number"):
        Streams(jobs=_Runner(), log=log)


def test_the_default_flush_policy_is_the_one_the_reference_page_states() -> None:
    assert declaration().flush == Flush(bytes=4096, every=0.05, capacity=1024)
    # And a caller's own policy displaces it rather than merging with it.
    mine = Flush(bytes=1, every=1.0, capacity=2)
    assert declaration(flush=mine).flush is mine


def test_the_declaration_carries_the_fence_and_the_index() -> None:
    declared = declaration()
    names = [column.name for column in declared.columns]
    assert names == ["fence", "idx", "kind", "body", "detail"]
    assert declared.retain == DEFAULT_RETENTION
    # The partition column leads the stream index, which is what turns "after
    # this cursor, for this key" into a range scan.
    assert "(stream_key, xid, seq)" in declared.schema_sql()


def test_a_bad_retry_policy_names_both_of_them() -> None:
    with pytest.raises(ValueError) as raised:
        _streams(on_retry="rewind")
    assert "'supersede' keeps the replaced rows" in str(raised.value)
    assert "'truncate' deletes them" in str(raised.value)


@pytest.mark.parametrize("option", ["poll", "idle"])
def test_a_non_positive_interval_is_refused(option: str) -> None:
    declared = declaration(schema="")
    with pytest.raises(ValueError) as raised:
        Streams(jobs=_Runner(), log=_Log(declared), **{option: 0.0})  # type: ignore[arg-type]
    assert f"{option} must be a positive number of seconds" in str(raised.value)


@pytest.mark.parametrize("option", ["poll", "idle"])
@pytest.mark.parametrize("interval", [True, float("nan"), float("inf")])
def test_a_stream_interval_must_be_a_finite_number(option: str, interval: Any) -> None:
    declared = declaration(schema="")
    options: dict[str, Any] = {option: interval}
    with pytest.raises(ValueError, match=rf"{option} must be a positive number.*finite"):
        Streams(jobs=_Runner(), log=_Log(declared), **options)


@pytest.mark.parametrize("option", ["poll", "idle"])
@pytest.mark.parametrize("interval", [True, float("nan"), float("inf")])
async def test_follow_interval_overrides_must_be_finite(option: str, interval: Any) -> None:
    streams, _, _ = _streams()
    options: dict[str, Any] = {option: interval}
    with pytest.raises(ValueError, match="idle and poll must be positive finite numbers"):
        await anext(streams.follow("key", **options))


def test_a_cursor_round_trips_through_its_encoding() -> None:
    cursor = StreamCursor(StreamCursor.start("k").token, 3, Cursor(9182, 44))
    decoded = StreamCursor.decode(cursor.encode(), key="k")
    assert decoded == cursor
    assert decoded.fence == 3
    assert decoded.cursor == Cursor(9182, 44)


def test_an_absent_last_event_id_is_the_beginning() -> None:
    assert StreamCursor.decode(None, key="k") == StreamCursor.start("k")
    assert StreamCursor.decode("", key="k") == StreamCursor.start("k")


def test_a_cursor_for_another_stream_is_refused_by_that_name() -> None:
    borrowed = StreamCursor.start("other").encode()
    with pytest.raises(ValueError) as raised:
        StreamCursor.decode(borrowed, key="mine")
    assert "belongs to a different stream" in str(raised.value)
    assert "would skip rows rather than return them" in str(raised.value)


async def test_a_stream_cursor_object_is_bound_to_the_stream_key() -> None:
    streams, _, _ = _streams()
    borrowed = StreamCursor.start("other")

    with pytest.raises(ValueError, match="belongs to a different stream"):
        await anext(streams.follow("mine", since=borrowed))


#: Shapes that are not a cursor, spelled with **this** stream's token so the
#: token check cannot answer for the shape checks. An earlier version of this
#: test used a made-up token, and every case passed through the
#: different-stream refusal instead -- a mutation run found four operands in the
#: digit check that no test distinguished, because none of them ever ran.
@pytest.mark.parametrize(
    "malformed",
    [
        "abc",
        "{token}.1.2",
        "{token}.1.2.3.4",
        "{token}. 1.2.3",
        "{token}.+1.2.3",
        "{token}.-1.2.3",
        "{token}.1.2.x",
        "{token}.x.2.3",
        "{token}.1.x.3",
        "{token}..2.3",
    ],
)
def test_a_cursor_that_is_not_one_is_refused_as_malformed(malformed: str) -> None:
    value = malformed.format(token=StreamCursor.start("k").token)
    with pytest.raises(ValueError) as raised:
        StreamCursor.decode(value, key="k")
    assert str(raised.value) == f"not a stream cursor: {value!r}", (
        "this must be the malformed-shape refusal and not the different-stream "
        "one, or the shape checks are never being reached"
    )


@pytest.mark.parametrize("digit", ["７", "١"])
def test_a_unicode_digit_is_not_a_digit_here(digit: str) -> None:
    token = StreamCursor.start("k").token
    value = f"{token}.1.2.{digit}"
    with pytest.raises(ValueError) as raised:
        StreamCursor.decode(value, key="k")
    assert str(raised.value) == f"not a stream cursor: {value!r}"


def test_a_well_formed_cursor_for_this_stream_is_accepted() -> None:
    token = StreamCursor.start("k").token
    assert StreamCursor.decode(f"{token}.4.5.6", key="k") == StreamCursor(token, 4, Cursor(5, 6))


def test_a_text_chunk_frames_as_sse_data_with_its_id() -> None:
    event = StreamEvent(KIND_CHUNK, StreamCursor("tok", 1, Cursor(5, 6)), 1, 0, b"hi")
    framed = event.as_sse()
    assert framed.event == "chunk"
    assert framed.data == "hi"
    assert framed.id == "tok.1.5.6"


def test_a_chunk_that_is_not_utf8_frames_as_base64_under_its_own_event_name() -> None:
    event = StreamEvent(KIND_CHUNK, StreamCursor("tok", 1, Cursor(5, 6)), 1, 0, b"\xff\xfe")
    framed = event.as_sse()
    assert framed.event == "chunk64"
    assert framed.data == "//4="
    assert event.as_dict()["data64"] == "//4="
    assert "data" not in event.as_dict()


def test_a_terminal_event_carries_its_reason_not_a_payload() -> None:
    event = StreamEvent(KIND_ERROR, StreamCursor("tok", 1, Cursor(5, 6)), 1, 4, detail="boom")
    assert event.terminal
    assert event.as_sse().event == "error"
    assert event.as_sse().data == "boom"
    assert event.as_dict()["detail"] == "boom"


def test_a_timeout_is_terminal_for_the_reader_and_says_which_kind_it_is() -> None:
    event = StreamEvent(KIND_TIMEOUT, StreamCursor("tok", 1, Cursor(5, 6)), 1, 4)
    assert event.terminal
    assert event.kind == KIND_TIMEOUT


def test_a_handle_is_a_response_the_app_will_accept() -> None:
    from wreath.response import StreamingResponse

    handle = StreamHandle(key="k", task_id="7")
    assert isinstance(handle, StreamingResponse)
    assert handle.status == 202
    assert handle.as_dict() == {"key": "k", "task_id": "7", "state": "queued"}


async def test_a_handle_emits_its_json_body_once_and_declares_its_length() -> None:
    import json

    handle = StreamHandle(key="k", task_id="7")
    chunks = [chunk async for chunk in handle.body]
    body = b"".join(chunks)
    assert len(chunks) == 1
    assert json.loads(body) == {"key": "k", "task_id": "7", "state": "queued"}
    assert dict(handle.headers)[b"content-length"] == str(len(body)).encode()


async def test_a_kind_with_no_producer_cannot_be_started() -> None:
    streams, _log, _runner = _streams()
    with pytest.raises(ValueError) as raised:
        await streams.start("chat", key="k")
    assert "the worker that runs it does not call start()" in str(raised.value)


async def test_start_refuses_a_producer_that_is_not_the_registered_one() -> None:
    streams, _log, _runner = _streams()

    @streams.producer("chat")
    async def registered(stream, text):
        await stream.write(text)

    async def impostor(stream, text):  # pragma: no cover - never run
        await stream.write(text)

    with pytest.raises(ValueError) as raised:
        await streams.start("chat", key="k", producer=impostor)
    assert "would run two different producers" in str(raised.value)
    # And the registered one is accepted, so this is a check rather than a ban.
    assert (await streams.start("chat", key="k", producer=registered)).key == "k"


async def test_a_duplicate_kind_is_refused() -> None:
    streams, _log, _runner = _streams()

    @streams.producer("chat")
    async def one(stream):  # pragma: no cover - never run
        pass

    with pytest.raises(ValueError) as raised:

        @streams.producer("chat")
        async def two(stream):  # pragma: no cover - never run
            pass

    assert "duplicate stream kind" in str(raised.value)


async def test_start_deduplicates_on_the_key_so_one_key_is_one_producer() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream):  # pragma: no cover - never run
        pass

    await streams.start("chat", key="conversation-7")
    assert runner.launched[0][2] == "stream:conversation-7"


@pytest.mark.parametrize("key", ["", "x" * (MAX_KEY_BYTES + 1), "a\x00b"])
async def test_an_unusable_key_is_refused(key: str) -> None:
    streams, _log, _runner = _streams()

    @streams.producer("chat")
    async def produce(stream):  # pragma: no cover - never run
        pass

    with pytest.raises(ValueError):
        await streams.start("chat", key=key)


@pytest.mark.parametrize("key", ["a\r\nx-admin: yes", "a\nb", "a\x7fb"])
async def test_a_key_carrying_a_control_character_cannot_append_a_header(key: str) -> None:
    streams, _log, _runner = _streams()
    response = streams.attach(key, public=True)
    assert response.status == 400
    assert b"would end the field" in response.body
    assert all(b"\r" not in value and b"\n" not in value for _n, value in response.headers)
    assert streams.stats()["cursor_refusals"] == 0, (
        "a bad key is the handler's argument being wrong, not a client replaying "
        "somebody else's Last-Event-ID"
    )


async def test_a_stream_whose_producer_never_ran_blocks_then_times_out() -> None:
    streams, _log, _runner = _streams()
    started = asyncio.get_running_loop().time()
    events = await _collect(streams, "never", idle=0.08, poll=0.01)
    elapsed = asyncio.get_running_loop().time() - started
    assert [event.kind for event in events] == [KIND_TIMEOUT]
    assert "reconnect with this id to resume" in events[0].detail
    assert elapsed >= 0.08, "it returned without waiting, so it did not block"


async def test_a_completed_stream_replays_in_order_and_ends() -> None:
    streams, log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a", "b", "c"]], fence=1, attempt=1)
    events = await _collect(streams, "k")
    assert [event.kind for event in events] == [KIND_CHUNK] * 3 + [KIND_END]
    assert [event.data for event in events[:3]] == [b"a", b"b", b"c"]
    assert [event.index for event in events] == [0, 1, 2, 3]
    assert log.dropped == 0


async def test_a_retried_attempt_supersedes_the_first_and_the_reader_says_so() -> None:
    streams, log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a", "b"]], fence=1, attempt=1)
    # The rows of attempt 1 survive; attempt 2 writes its own under fence 2.
    await runner.attempt("stream_chat", "k", [["A", "B", "C"]], fence=2, attempt=2)

    events = await _collect(streams, "k")
    assert [event.kind for event in events] == [KIND_CHUNK] * 3 + [KIND_END]
    assert b"".join(event.data for event in events) == b"ABC"
    assert streams.superseded_rows == 3  # two chunks and the first `end`
    # And the rows really are still there: this is `supersede`, not `truncate`.
    assert len(log.rows_for("k")) == 7


async def test_a_client_resuming_under_a_replaced_fence_is_told_before_the_new_bytes() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a", "b"]], fence=1, attempt=1)
    first = await _collect(streams, "k", idle=0.03, poll=0.005)
    resume_from = first[1].cursor  # after chunk "b", under fence 1
    assert resume_from.fence == 1

    await runner.attempt("stream_chat", "k", [["A", "B"]], fence=2, attempt=2)
    events = await _collect(streams, "k", since=resume_from)
    assert events[0].kind == KIND_SUPERSEDED
    assert "discard it and render what follows" in events[0].detail
    assert b"".join(event.data for event in events) == b"AB"


async def test_a_late_row_from_a_fenced_worker_is_skipped_not_delivered() -> None:
    streams, log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a"]], fence=1, attempt=1)
    await runner.attempt("stream_chat", "k", [["A", "B"]], fence=2, attempt=2)
    # The fence-1 worker wakes up and flushes what it still had.
    await log.append_many(
        [("k", {"fence": 1, "idx": 9, "kind": KIND_CHUNK, "body": b"zombie", "detail": ""})]
    )

    events = await _collect(streams, "k")
    assert b"zombie" not in b"".join(event.data for event in events)
    assert [event.kind for event in events] == [KIND_CHUNK, KIND_CHUNK, KIND_END]


async def test_two_attachers_one_from_zero_and_one_mid_stream_both_complete() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)
            await asyncio.sleep(0)

    async def run_producer() -> None:
        await asyncio.sleep(0.02)
        await runner.attempt("stream_chat", "k", [["a", "b", "c", "d"]], fence=1, attempt=1)

    from_zero = asyncio.create_task(_collect(streams, "k", idle=0.2, poll=0.005))
    producing = asyncio.create_task(run_producer())
    await producing
    whole = await from_zero
    assert b"".join(event.data for event in whole) == b"abcd"
    assert whole[-1].kind == KIND_END

    # The mid-stream attacher: resuming from the cursor after "b", it gets the
    # rest and nothing it already had.
    mid = await _collect(streams, "k", since=whole[1].cursor, idle=0.05, poll=0.005)
    assert b"".join(event.data for event in mid) == b"cd"
    assert mid[-1].kind == KIND_END

    # And a third arriving after the stream finished still gets all of it,
    # because the rows are the delivery rather than a live fan-out.
    second = await _collect(streams, "k", idle=0.05, poll=0.005)
    assert b"".join(event.data for event in second) == b"abcd"
    assert second[-1].kind == KIND_END


async def test_a_reader_resuming_from_the_last_id_sees_no_gap_and_no_duplicate() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt(
        "stream_chat", "k", [[f"{index}" for index in range(20)]], fence=1, attempt=1
    )
    first = await _collect(streams, "k")
    cut = 7
    resumed = await _collect(streams, "k", since=first[cut].cursor)
    joined = b"".join(event.data for event in first[: cut + 1]) + b"".join(
        event.data for event in resumed
    )
    assert joined == b"".join(event.data for event in first)


async def test_a_failing_producer_writes_a_terminal_error_only_on_its_last_attempt() -> None:
    streams, log, runner = _streams()

    @streams.producer("chat", retries=1)
    async def produce(stream):
        await stream.write("partial")
        raise RuntimeError("model refused")

    with pytest.raises(RuntimeError):
        await runner.attempt("stream_chat", "k", [], fence=1, attempt=1)
    kinds = [row.values["kind"] for row in log.rows_for("k")]
    assert KIND_ERROR not in kinds, "attempt 1 of 2 must not close the stream"

    with pytest.raises(RuntimeError):
        await runner.attempt("stream_chat", "k", [], fence=2, attempt=2)
    events = await _collect(streams, "k")
    assert events[-1].kind == KIND_ERROR
    assert "RuntimeError: model refused" in events[-1].detail


async def test_a_cancelled_attempt_writes_no_terminal_row_and_re_raises() -> None:
    streams, log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream):
        await stream.write("one")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await runner.attempt("stream_chat", "k", [], fence=1, attempt=1)
    assert [row.values["kind"] for row in log.rows_for("k")] == []
    assert log.dropped == 1, "the unflushed row is counted, not silently gone"


async def test_cancel_writes_the_terminal_row_before_it_fences_the_job() -> None:
    streams, log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a", "b"]], fence=1, attempt=1)
    # Drop the `end` the completed attempt wrote, so this is a live stream.
    log.rows = [row for row in log.rows if row.values["kind"] != KIND_END]

    assert await streams.cancel("k", reason="the user closed the tab") is True
    assert runner.cancelled == ["stream:k"]
    events = await _collect(streams, "k")
    assert events[-1].kind == KIND_CANCELLED
    assert events[-1].detail == "the user closed the tab"


async def test_cancelling_a_finished_job_still_lets_every_reader_go() -> None:
    streams, _log, runner = _streams()
    runner.cancel_result = False
    assert await streams.cancel("k") is False
    events = await _collect(streams, "k")
    assert [event.kind for event in events] == [KIND_CANCELLED]


async def test_truncate_deletes_the_replaced_rows_and_the_reader_still_says_so() -> None:
    streams, log, runner = _streams(on_retry="truncate")

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a", "b"]], fence=1, attempt=1)
    await runner.attempt("stream_chat", "k", [["A"]], fence=2, attempt=2)
    assert all(row.values["fence"] == 2 for row in log.rows_for("k"))
    events = await _collect(streams, "k")
    assert b"".join(event.data for event in events) == b"A"


async def test_a_row_still_in_flight_is_not_read_and_does_not_move_the_fence() -> None:
    streams, log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a"]], fence=1, attempt=1)
    await runner.attempt("stream_chat", "k", [["A"]], fence=2, attempt=2)
    log.withheld = 2  # attempt 2's chunk and its `end` are still in flight
    events = await _collect(streams, "k", idle=0.03, poll=0.005)
    assert b"".join(event.data for event in events) == b"a"


async def test_started_is_counted_against_attached_and_a_gap_is_a_finding() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream):  # pragma: no cover - never run
        pass

    for index in range(4):
        await streams.start("chat", key=f"k{index}")
    assert streams.stats() == {
        "started": 4,
        "attached": 0,
        "resumed": 0,
        "superseded_rows": 0,
        "cursor_refusals": 0,
        "dropped": 0,
    }
    findings = check_stream_attachment(streams)
    assert findings and "0 of 4 streams started on this worker" in findings[0]

    await _collect(streams, "k0", idle=0.02, poll=0.005)
    await _collect(streams, "k1", idle=0.02, poll=0.005)
    assert streams.attached == 2
    # Attaching twice to one key counts one stream, not two attaches.
    await _collect(streams, "k0", idle=0.02, poll=0.005)
    assert streams.attached == 2
    assert check_stream_attachment(streams) == []


async def test_a_refused_cursor_answers_400_and_is_counted() -> None:
    streams, _log, _runner = _streams()
    response = streams.attach(
        "mine", since=StreamCursor.start("other").encode(), public=True
    )
    assert response.status == 400
    assert b"belongs to a different stream" in response.body
    assert streams.stats()["cursor_refusals"] == 1
    findings = check_stream_attachment(streams)
    assert any("Last-Event-ID value(s) were refused" in finding for finding in findings)


async def test_an_unauthorized_attach_is_a_404_identical_to_an_unknown_stream() -> None:
    streams, _log, _runner = _streams()
    refused = streams.attach("k", authorize=lambda key: False)
    assert refused.status == 404
    assert b"unknown or expired stream" in refused.body


def test_attach_requires_an_exact_authorization_decision() -> None:
    streams, _log, _runner = _streams()

    def ambiguous(_key: str) -> Any:
        return "allow"

    refused = streams.attach("k", authorize=ambiguous)
    assert refused.status == 404


def test_public_must_be_an_exact_boolean() -> None:
    streams, _log, _runner = _streams()
    options: dict[str, Any] = {"public": 1}
    with pytest.raises(ValueError, match="public must be a boolean"):
        streams.attach("k", **options)


def test_attach_refuses_a_stream_cursor_object_for_another_key_synchronously() -> None:
    streams, _log, _runner = _streams()
    response = streams.attach("mine", since=StreamCursor.start("other"), public=True)
    assert response.status == 400
    assert b"belongs to a different stream" in response.body


async def test_attach_returns_an_sse_response_that_frames_the_reader() -> None:
    from wreath.response import SSEResponse

    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["hello ", "world"]], fence=1, attempt=1)
    response = streams.attach("k", idle=0.03, poll=0.005, public=True)
    assert isinstance(response, SSEResponse)
    body = b"".join([chunk async for chunk in response.body])
    assert b"event: chunk\n" in body
    assert b"data: hello \n" in body
    assert b"event: end\n" in body
    assert dict(response.headers)[b"x-stream-key"] == b"k"
    assert dict(response.headers)[b"x-accel-buffering"] == b"no"


async def test_push_stream_sends_one_json_frame_per_event() -> None:
    import json

    class _Socket:
        def __init__(self) -> None:
            self.frames: list[str] = []

        async def send_text(self, text: str) -> None:
            self.frames.append(text)

    from wreath.streams import push_stream

    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a", "b"]], fence=1, attempt=1)
    socket = _Socket()
    await push_stream(socket, streams, "k", idle=0.03, poll=0.005, public=True)
    decoded = [json.loads(frame) for frame in socket.frames]
    assert [frame["kind"] for frame in decoded] == ["chunk", "chunk", "end"]
    assert [frame["data"] for frame in decoded[:2]] == ["a", "b"]
    assert all(frame["id"].startswith(StreamCursor.start("k").token) for frame in decoded)


@pytest.mark.parametrize("decision", [False, "allow"])
async def test_push_stream_requires_an_exact_true_authorization(decision: Any) -> None:
    from wreath.streams import push_stream

    class _Socket:
        async def send_text(self, text: str) -> None:
            raise AssertionError(f"an unauthorized stream sent {text!r}")

    streams, _log, _runner = _streams()

    def authorize(_key: str) -> Any:
        return decision

    assert await push_stream(_Socket(), streams, "k", authorize=authorize) is False


async def test_push_stream_accepts_an_exact_true_authorization() -> None:
    from wreath.streams import push_stream

    class _Socket:
        async def send_text(self, text: str) -> None:
            return None

    streams, _log, _runner = _streams()
    assert (
        await push_stream(
            _Socket(), streams, "k", authorize=lambda _key: True, idle=0.001, poll=0.001
        )
        is True
    )


async def test_a_writer_batches_rather_than_writing_a_row_per_token() -> None:
    from wreath.log import Flush as _Flush

    declared = declaration(schema="", flush=Flush(bytes=1 << 20, every=1e6, capacity=64))
    assert isinstance(declared.flush, _Flush)
    log = _Log(declared)
    streams = Streams(jobs=_Runner(), log=log, idle=0.05, poll=0.005)  # type: ignore[arg-type]

    @streams.producer("chat")
    async def produce(stream, count):
        for index in range(count):
            await stream.write(f"t{index}")
        assert stream.pending == 64 - (64 - count) if count < 64 else True

    runner = streams._jobs
    await runner.attempt("stream_chat", "k", [200], fence=1, attempt=1)
    # 200 chunks plus a terminal row through a 64-row buffer: the buffer fills
    # and is drained rather than dropping, and a row per token never happens.
    assert len(log.rows_for("k")) == 201
    assert log.dropped == 0


async def test_a_full_buffer_is_drained_rather_than_dropping_a_chunk() -> None:
    declared = declaration(schema="", flush=Flush(bytes=1 << 20, every=1e6, capacity=2))
    log = _Log(declared)
    streams = Streams(jobs=_Runner(), log=log, idle=0.05, poll=0.005)  # type: ignore[arg-type]

    @streams.producer("chat")
    async def produce(stream):
        for index in range(9):
            await stream.write(f"{index}")

    await streams._jobs.attempt("stream_chat", "k", [], fence=1, attempt=1)
    assert log.dropped == 0
    events = await _collect(streams, "k")
    assert b"".join(event.data for event in events) == b"012345678"


# Each test below exists because `wreath mutant` changed a line in
# `wreath.streams` and every test still passed. They are grouped rather than
# scattered so the next reader can see which parts of the module had no witness.


async def test_a_producer_may_write_bytes_as_well_as_text() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream):
        await stream.write("text ")
        await stream.write(b"\xff\xfe")

    await runner.attempt("stream_chat", "k", [], fence=1, attempt=1)
    events = await _collect(streams, "k")
    assert [event.data for event in events[:2]] == [b"text ", b"\xff\xfe"]
    assert events[1].as_sse().event == "chunk64"


async def test_the_byte_threshold_flushes_mid_stream_without_the_buffer_filling() -> None:
    declared = declaration(schema="", flush=Flush(bytes=120, every=1e6, capacity=4096))
    log = _Log(declared)
    streams = Streams(jobs=_Runner(), log=log, idle=0.05, poll=0.005)  # type: ignore[arg-type]
    seen: list[tuple[int, int]] = []

    @streams.producer("chat")
    async def produce(stream):
        for _ in range(9):
            await stream.write("0123456789")
            seen.append((stream.flushes, stream.pending))

    await streams._jobs.attempt("stream_chat", "k", [], fence=1, attempt=1)
    assert seen[0][0] == 0, "one row is under the threshold"
    assert seen[-1][0] >= 2, (
        f"nine rows crossed a 120-byte threshold {seen[-1][0]} time(s); the byte "
        "trigger is not reaching the flush"
    )
    # And the capacity never came near, so the byte threshold is the only thing
    # that can have issued a statement here.
    assert max(pending for _flushes, pending in seen) < 4096
    assert len(log.rows_for("k")) == 10


async def test_a_full_buffer_is_drained_before_it_can_refuse_a_row() -> None:
    declared = declaration(schema="", flush=Flush(bytes=1 << 20, every=1e6, capacity=4))
    log = _Log(declared)
    streams = Streams(jobs=_Runner(), log=log, idle=0.05, poll=0.005)  # type: ignore[arg-type]
    counted: list[int] = []

    @streams.producer("chat")
    async def produce(stream):
        for _ in range(12):
            await stream.write("x")
        counted.append(stream.flushes)

    await streams._jobs.attempt("stream_chat", "k", [], fence=1, attempt=1)
    # Twelve rows through a four-row buffer: two drains before the producer
    # returns, not twelve -- the buffer fills at offers 5 and 9.
    assert counted == [2]
    assert log.dropped == 0


async def test_a_buffer_that_refuses_after_a_drain_raises_rather_than_dropping() -> None:
    from wreath.streams import StreamWriter

    log = _Log(declaration(schema=""))
    writer = StreamWriter(log, "k", fence=1, resume_from=0)

    class _Refusing:
        pending = 0
        due = False

        def offer(self, **_values):
            return False

    writer._buffer = _Refusing()  # type: ignore[assignment]
    with pytest.raises(RuntimeError) as raised:
        await writer.write("x")
    assert "a dropped chunk is a hole in a stream that promises none" in str(raised.value)


async def test_follow_refuses_a_key_it_would_not_accept_at_start() -> None:
    streams, _log, _runner = _streams()
    with pytest.raises(ValueError) as raised:
        await _collect(streams, "")
    assert "non-empty string" in str(raised.value)


@pytest.mark.parametrize("option", ["idle", "poll"])
async def test_follow_refuses_a_non_positive_interval(option: str) -> None:
    streams, _log, _runner = _streams()
    with pytest.raises(ValueError) as raised:
        await _collect(streams, "k", **{option: 0.0})
    assert "idle and poll must be positive" in str(raised.value)


async def test_the_poll_interval_decides_how_often_the_log_is_read() -> None:
    streams, log, _runner = _streams()
    reads = 0
    inner = log.read

    async def counting(stream=None, *, after, limit=512):
        nonlocal reads
        reads += 1
        return await inner(stream, after=after, limit=limit)

    log.read = counting  # type: ignore[assignment]
    await _collect(streams, "quiet", idle=0.12, poll=0.04)
    coarse = reads
    reads = 0
    await _collect(streams, "quiet", idle=0.12, poll=0.005)
    assert reads > coarse * 2, (
        f"a five-millisecond poll read {reads} times and a forty-millisecond one "
        f"{coarse}; the interval is not reaching the loop"
    )


async def test_resuming_is_counted_apart_from_attaching() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a", "b"]], fence=1, attempt=1)
    first = await _collect(streams, "k")
    assert streams.resumed == 0, "a fresh attach is not a resume"
    await _collect(streams, "k", since=first[0].cursor)
    assert streams.resumed == 1


async def test_attaching_to_a_stream_this_worker_never_started_is_not_counted() -> None:
    streams, _log, _runner = _streams()

    @streams.producer("chat")
    async def produce(stream):  # pragma: no cover - never run
        pass

    await streams.start("chat", key="mine")
    await _collect(streams, "somebody-elses", idle=0.02, poll=0.005)
    assert streams.attached == 0
    await _collect(streams, "mine", idle=0.02, poll=0.005)
    assert streams.attached == 1


async def test_a_retry_that_starts_while_a_client_is_tailing_is_announced_live() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    @streams.producer("halfway", retries=1)
    async def half(stream, words):
        for word in words:
            await stream.write(word)
        await stream.flush()
        raise RuntimeError("the worker went away")

    async def attempts() -> None:
        # Attempt 1 fails rather than completing, which is the only way a retry
        # happens at all -- a completed attempt writes `end` and the reader
        # stops there.
        with pytest.raises(RuntimeError):
            await runner.attempt("stream_halfway", "k", [["a", "b"]], fence=1, attempt=1)
        await asyncio.sleep(0.04)
        await runner.attempt("stream_chat", "k", [["A", "B"]], fence=2, attempt=2)

    reading = asyncio.create_task(_collect(streams, "k", idle=0.3, poll=0.005))
    await asyncio.sleep(0.01)
    await attempts()
    events = await reading
    kinds = [event.kind for event in events]
    assert KIND_SUPERSEDED in kinds, "the tailing client was never told"
    at = kinds.index(KIND_SUPERSEDED)
    assert b"".join(event.data for event in events[:at]) == b"ab"
    assert b"".join(event.data for event in events[at:]) == b"AB"
    assert events[-1].kind == KIND_END


async def test_a_row_whose_detail_is_null_reads_as_an_empty_reason() -> None:
    streams, log, _runner = _streams()
    await log.append_many(
        [("k", {"fence": 1, "idx": 0, "kind": KIND_END, "body": None, "detail": None})]
    )
    events = await _collect(streams, "k")
    assert events[0].detail == ""
    assert events[0].as_sse().data == ""


async def test_an_authorized_attach_opens_the_stream() -> None:
    from wreath.response import SSEResponse

    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["hi"]], fence=1, attempt=1)
    response = streams.attach("k", authorize=lambda key: key == "k", idle=0.03, poll=0.005)
    assert isinstance(response, SSEResponse)
    body = b"".join([chunk async for chunk in response.body])
    assert b"data: hi\n" in body


async def test_attach_accepts_a_cursor_object_as_well_as_a_header() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, words):
        for word in words:
            await stream.write(word)

    await runner.attempt("stream_chat", "k", [["a", "b"]], fence=1, attempt=1)
    events = await _collect(streams, "k")
    response = streams.attach(
        "k", since=events[0].cursor, idle=0.03, poll=0.005, public=True
    )
    body = b"".join([chunk async for chunk in response.body])
    assert b"data: b\n" in body
    assert b"data: a\n" not in body, "the cursor was ignored and the stream replayed"


async def test_start_names_the_kinds_this_process_does_have() -> None:
    streams, _log, _runner = _streams()
    with pytest.raises(ValueError) as raised:
        await streams.start("chat", key="k")
    assert "(this process has none)" in str(raised.value)

    @streams.producer("summarise")
    async def one(stream):  # pragma: no cover - never run
        pass

    @streams.producer("translate")
    async def two(stream):  # pragma: no cover - never run
        pass

    with pytest.raises(ValueError) as raised:
        await streams.start("chat", key="k")
    assert "(this process has summarise, translate)" in str(raised.value)


async def test_a_lost_chunk_is_a_finding_because_it_is_a_hole_in_a_stream() -> None:
    streams, log, _runner = _streams()
    log._dropped = 3
    findings = check_stream_attachment(streams)
    assert any("3 buffered chunk(s) were lost" in finding for finding in findings)
    assert any("Flush(capacity=...)" in finding for finding in findings)


def test_a_worker_that_started_nothing_has_nothing_to_report() -> None:
    streams, _log, _runner = _streams()
    assert streams.stats()["started"] == 0
    assert check_stream_attachment(streams) == []


async def test_a_stream_that_keeps_producing_outlives_the_idle_window() -> None:
    streams, _log, runner = _streams()

    @streams.producer("chat")
    async def produce(stream, count):
        for index in range(count):
            await stream.write(f"{index}")
            await stream.flush()
            await asyncio.sleep(0.02)

    idle = 0.06
    producing = asyncio.create_task(runner.attempt("stream_chat", "k", [8], fence=1, attempt=1))
    started = asyncio.get_running_loop().time()
    events = await _collect(streams, "k", idle=idle, poll=0.005)
    elapsed = asyncio.get_running_loop().time() - started
    await producing
    assert events[-1].kind == KIND_END, (
        f"the reader gave up after {elapsed:.3f}s on a stream that took longer "
        f"than its {idle}s idle window but was never silent for one"
    )
    assert b"".join(event.data for event in events) == b"01234567"
    assert elapsed > idle * 2, "the producer did not outrun the idle window at all"


async def test_a_stream_that_goes_quiet_mid_generation_still_times_out() -> None:
    streams, log, _runner = _streams()
    await log.append_many(
        [("k", {"fence": 1, "idx": 0, "kind": KIND_CHUNK, "body": b"half", "detail": ""})]
    )
    events = await _collect(streams, "k", idle=0.05, poll=0.005)
    assert [event.kind for event in events] == [KIND_CHUNK, KIND_TIMEOUT]
    # The timeout carries the cursor of the last real row, so the client can
    # reconnect from exactly where it stopped rather than from the beginning.
    assert events[-1].cursor == events[0].cursor


@pytest.mark.parametrize("kind", ["", "chat stream", "chat-stream", "café", "x" * 64])
async def test_a_kind_that_is_not_a_task_name_is_refused_at_registration(kind: str) -> None:
    streams, _log, _runner = _streams()
    with pytest.raises(ValueError) as raised:
        streams.producer(kind)
    assert "stream kind" in str(raised.value)


@pytest.mark.parametrize("key", [None, 7, b"bytes"])
async def test_a_key_that_is_not_a_string_is_refused_as_one(key) -> None:
    streams, _log, _runner = _streams()

    @streams.producer("chat")
    async def produce(stream):  # pragma: no cover - never run
        pass

    with pytest.raises(ValueError) as raised:
        await streams.start("chat", key=key)
    assert str(raised.value) == "a stream key must be a non-empty string"


async def test_a_slow_producers_chunk_waits_for_its_next_write_unless_it_flushes() -> None:
    declared = declaration(schema="", flush=Flush(bytes=1 << 20, every=0.001, capacity=64))
    log = _Log(declared)
    streams = Streams(jobs=_Runner(), log=log, idle=0.05, poll=0.005)  # type: ignore[arg-type]

    @streams.producer("chat")
    async def produce(stream):
        await stream.write("first")
        await asyncio.sleep(0.02)  # well past `every`, and nothing flushes it
        assert stream.pending == 1
        assert log.rows_for("k") == []
        await stream.write("second")  # ... until the next write checks the clock
        assert stream.pending == 0
        assert len(log.rows_for("k")) == 2

    await streams._jobs.attempt("stream_chat", "k", [], fence=1, attempt=1)
    events = await _collect(streams, "k")
    assert b"".join(event.data for event in events) == b"firstsecond"


async def test_a_producer_that_flushes_itself_does_not_wait() -> None:
    declared = declaration(schema="", flush=Flush(bytes=1 << 20, every=1e6, capacity=64))
    log = _Log(declared)
    streams = Streams(jobs=_Runner(), log=log, idle=0.05, poll=0.005)  # type: ignore[arg-type]

    @streams.producer("chat")
    async def produce(stream):
        await stream.write("alone")
        assert log.rows_for("k") == []
        assert await stream.flush() == 1
        assert len(log.rows_for("k")) == 1
        assert stream.pending == 0

    await streams._jobs.attempt("stream_chat", "k", [], fence=1, attempt=1)
    assert len(log.rows_for("k")) == 2
