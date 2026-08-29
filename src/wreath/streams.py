"""Resumable streams over durable work: the producer outlives the connection.

An LLM response is a five-minute HTTP response, and HTTP has no resume.
`EventSource` reconnects; the *generation* does not, so a five-minute agent task
that drops at minute four restarts from zero. The answer everybody converges on
is a **durable session** that decouples delivery from the connection: a
reconnecting client, on any device, resumes from the last delivered chunk
without re-invoking the producer.

Wreath already had both halves and never joined them. `wreath.jobs` is durable
execution with attempt identity and fencing; `wreath.log` is a total order under
a cursor that cannot skip, with a batched append. This is the join: a request
that **attaches to** a running job's output instead of producing it.

```python
streams = Streams(jobs=runner, log=PostgresLog(database, declaration()))

@streams.producer("chat")
async def answer(stream: StreamWriter, question: str) -> None:
    async for token in some_model(question):
        await stream.write(token)

@app.post("/chat")
async def ask(body: Ask) -> StreamHandle:
    return await streams.start("chat", key=body.stream_id, args=(body.text,))

@app.get("/chat/{key}")
async def resume(request: Request, key: str) -> Response:
    return streams.attach(key, since=request.header("last-event-id"))
```

**No provider client ships here.** Providers change monthly,
`wreath.http_client` already pools, retries and rate-limits, and the
framework-shaped part is this primitive -- which is provider-agnostic.

## The four things this module is careful about

**Exactly-once is a client-side property.** The wire is at-least-once. A
reconnecting client can be handed a chunk it already has, and the *sequence* is
what lets it drop the duplicate. Nothing on this side promises otherwise, and a
server that did would be lying about what a transport can keep.

**A retried producer must not append a second copy of chunks 1..400.** So every
row carries the attempt's **fence**, which `wreath.jobs` already bumps on each
claim, and a reader skips any row whose fence is below the highest fence that
stream has reached. That is the whole idempotency scheme -- see `follow` for the
one rule -- and it is affordable only because the fence already existed. The
reader emits an explicit `superseded` event when it happens, because a client
that concatenates a replaced range renders duplicated text and blames the model.
`Streams(on_retry="truncate")` is the simpler discipline the plan kept in
reserve: the superseded rows are deleted at the start of the retried attempt.
Both tell the reader; they differ only in whether the old bytes survive.

**The chunk log is delivery, not transcript.** `declaration()` refuses
`KEEP_FOREVER` outright. Prompts and completions are the newest large pile of
unclassified personal data, and a buffer that cannot expire is the shape that
makes them unerasable; conversation history is application data in the ORM,
where it has an owner and a retention decision of its own. `retention_pass`
executes the window as a counted `wreath.passes` walk.

**A stream nobody attaches to still costs.** The producer runs regardless --
that is the entire point -- but an application starting streams nobody reads has
built a token furnace with no meter on it. `Streams.stats` counts started
against attached and `check_stream_attachment` turns that into a finding.

## What this does not do

No fan-out of its own. Two clients attaching to one key both read the same
rows out of the same log, which is what makes a second reader free and a second
*worker* unnecessary; there is no in-memory subscriber set to lose on a restart.

No transcript, no projections, no re-derived state. See `wreath.log`'s module
docstring for the rest of that boundary -- this is one of its four callers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, NamedTuple

from ._b64 import b64_encode
from ._jobcore import validate_identifier
from .log import DEFAULT_LIMIT, Column, Cursor, Flush, Log, PostgresLog
from .response import JSONResponse, Response, ServerSentEvent, SSEResponse, StreamingResponse

__all__ = [
    "DEFAULT_IDLE",
    "DEFAULT_POLL",
    "DEFAULT_RETENTION",
    "MAX_KEY_BYTES",
    "SSE_PROXY_HEADERS",
    "StreamCursor",
    "StreamEvent",
    "StreamHandle",
    "StreamWriter",
    "Streams",
    "check_stream_attachment",
    "declaration",
    "push_stream",
]

#: How long a delivery buffer's rows live by default. An hour is long enough for
#: a client to lose a train tunnel and come back, and short enough that the table
#: is not a transcript nobody declared. Deliberately not a day: the longer this
#: is, the more it looks like conversation history, and the sooner somebody
#: starts reading it as though it were.
DEFAULT_RETENTION: Final = 3600.0

#: Seconds between polls of the log while tailing. Small, because this is the
#: latency a token appears with; the poll is one indexed range scan against a
#: cursor, not a table scan.
DEFAULT_POLL: Final = 0.05

#: Seconds a reader waits with nothing new before it gives up and says so.
#: Covers both the stream whose producer never ran -- which must block and then
#: time out rather than return an empty success -- and the live stream whose
#: producer has stalled. An SSE client reconnects with its `Last-Event-ID` and
#: loses nothing by being sent away.
DEFAULT_IDLE: Final = 30.0

#: Longest stream key accepted. A key reaches SQL as a parameter, never as text,
#: so this is not an injection bound -- it is a bound on a value that indexes a
#: table and is echoed into every event id.
MAX_KEY_BYTES: Final = 512

#: Rows one replay page carries. `wreath.log`'s own default: large enough that a
#: client resuming four thousand chunks behind is not making a round trip per
#: handful, small enough that one page fits in memory.
REPLAY_LIMIT: Final = DEFAULT_LIMIT

#: What a reverse proxy must not do to an SSE stream. Nginx buffers proxied
#: responses by default, which holds every token until the response ends -- and
#: the response ends when the generation does, so the feature appears to work in
#: development and delivers one five-minute blob in production.
#: `SSEResponse` already sends `x-accel-buffering: no`; these are the settings on
#: the *proxy* that the header cannot reach.
SSE_PROXY_HEADERS: Final = (
    "proxy_buffering off;",
    "proxy_cache off;",
    "proxy_read_timeout 3600s;",
    "proxy_http_version 1.1;",
)

#: Row kinds written to the chunk log. `chunk` carries payload; the other three
#: are terminal and carry a reason instead.
KIND_CHUNK: Final = "chunk"
KIND_END: Final = "end"
KIND_ERROR: Final = "error"
KIND_CANCELLED: Final = "cancelled"

#: Terminal row kinds: a reader that delivers one of these stops.
TERMINAL_KINDS: Final = frozenset({KIND_END, KIND_ERROR, KIND_CANCELLED})

#: Event kinds a reader *synthesises*, which are never rows. `superseded` says a
#: retried attempt replaced everything delivered so far; `timeout` says nothing
#: arrived for `idle` seconds and the reader stopped rather than holding a
#: connection open for a producer that may never write.
KIND_SUPERSEDED: Final = "superseded"
KIND_TIMEOUT: Final = "timeout"

#: How a reader answers a retried attempt. `supersede` keeps the replaced rows
#: and skips them on read; `truncate` deletes them when the new attempt starts.
RETRY_POLICIES: Final = ("supersede", "truncate")


def declaration(
    table: str = "stream_chunks",
    *,
    schema: str = "wreath",
    retain: float = DEFAULT_RETENTION,
    flush: Flush | None = None,
) -> Log:
    """The `wreath.log` shape a chunk buffer needs.

    Four payload columns beside the log's own `(xid, seq)`:

    * `fence` -- the attempt that wrote this row, straight off
      `wreath.jobs.JobContext.fence`. It is what makes a retry idempotent to a
      reader without the producer having to be.
    * `idx` -- this row's position *within its attempt*, from zero. Not a
      cursor: the cursor is the log's, and two attempts both have an `idx` 0.
      It is what a client counts, and what `resume_from` reports to a producer
      that can pick up where the last attempt stopped.
    * `kind` -- `chunk`, or one of the three terminal kinds.
    * `body` -- the payload, `NULL` on a terminal row, where the reason travels
      in `detail` instead.

    Args:
        table: the backing table; interpolated, so a plain identifier.
        schema: where it lives. Wreath's own furniture goes in `wreath`.
        retain: seconds a chunk lives. **`KEEP_FOREVER` is refused**, by name:
            this table is delivery, and a delivery buffer that never expires is
            a transcript of prompts and completions that nobody declared, in a
            place with no erasure story.
        flush: the buffering policy `StreamWriter` batches under.

    Raises:
        ValueError: on `retain=KEEP_FOREVER`.
    """
    if retain is None:
        raise ValueError(
            "a stream chunk log is delivery, not transcript, and retain=KEEP_FOREVER "
            "would make it one: prompts and completions would accumulate in a table "
            "with no window and no erasure path. Keep conversation history in the "
            "ORM, where it has an owner, and give this buffer a number of seconds."
        )
    return Log(
        table=table,
        retain=retain,
        columns=(
            Column("fence", "bigint", null=False),
            Column("idx", "bigint", null=False),
            Column("kind", "text", null=False),
            Column("body", "bytea", null=True),
            Column("detail", "text", null=True),
        ),
        stream="stream_key",
        schema=schema,
        flush=flush if flush is not None else Flush(bytes=4096, every=0.05, capacity=1024),
        prefix="wreath_stream",
    )


def _token(key: str) -> str:
    """Eight hex characters identifying a stream key, for the event id.

    Not a secret and not a signature -- it is a *typo check* with teeth. A
    `Last-Event-ID` is client-supplied and is an index into a log, so the one
    thing it must never do is address another stream's rows. The `WHERE
    stream_key = $1` in `wreath.log`'s read already makes reading them
    impossible; what this catches is the quieter failure, where a cursor lifted
    from stream A is replayed against stream B and silently *skips* B's rows
    below it, because `(xid, seq)` is a whole-log order.
    """
    from hashlib import blake2b

    return blake2b(key.encode("utf-8"), digest_size=4).hexdigest()


class StreamCursor(NamedTuple):
    """Where a client got to: which attempt, and where in the log.

    Three parts and a stream tag, because all four are load-bearing. The log
    cursor `(xid, seq)` is the resume point and cannot skip (see
    `wreath.log`'s cursor contract); `fence` is the
    attempt the client last read under, which is what lets `follow` say *your
    content was replaced* instead of leaving the client to notice; and `token`
    binds the whole thing to one stream key.

    Opaque: build one with `start`, take one off a `StreamEvent`, round-trip it
    through `encode`/`decode`, and never do arithmetic on it.
    """

    token: str
    fence: int
    cursor: Cursor

    @classmethod
    def start(cls, key: str) -> StreamCursor:
        """The cursor before the first row of `key`."""
        return cls(_token(key), 0, Cursor.start())

    def encode(self) -> str:
        """The form a client echoes back as a `Last-Event-ID`."""
        return f"{self.token}.{self.fence}.{self.cursor.xid}.{self.cursor.seq}"

    @classmethod
    def decode(cls, value: str | None, *, key: str) -> StreamCursor:
        """Parse `encode`'s form for `key`, refusing anything else.

        An empty or absent value is "from the beginning", which is what an
        `EventSource` sends on its first connection.

        Everything else is refused rather than repaired, for the reason
        `wreath.log.Cursor.decode` refuses: the value arrives in a header
        somebody else wrote. `str.isdigit` rather than `int(...)` in a `try`,
        because `int` accepts leading whitespace, a sign, and Unicode digits
        from other scripts -- `int("７")` really is 7, and none of those are
        cursors this ever emitted.

        Raises:
            ValueError: naming *which* check failed. A cursor for another stream
                is called out by name rather than folded into "malformed",
                because the two have different causes: one is a corrupted
                header, the other is a client with its keys crossed.
        """
        if not value:
            return cls.start(key)
        if not value.isascii():
            raise ValueError(f"not a stream cursor: {value!r}")
        parts = value.split(".")
        if len(parts) != 4:
            raise ValueError(f"not a stream cursor: {value!r}")
        token, fence, xid, seq = parts
        if not (fence.isdigit() and xid.isdigit() and seq.isdigit()):
            raise ValueError(f"not a stream cursor: {value!r}")
        if token != _token(key):
            raise ValueError(
                f"this cursor belongs to a different stream than {key!r}; a "
                "Last-Event-ID is an index into one stream's rows and replaying "
                "it against another would skip rows rather than return them"
            )
        return cls(token, int(fence), Cursor(int(xid), int(seq)))


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One thing a reader has to say, whether or not it was a row.

    `chunk` is payload. `end`, `error` and `cancelled` are the three ways a
    stream finishes and are rows too. `superseded` and `timeout` are synthesised
    by `Streams.follow` and carry the cursor of the last real row, so a client
    that stores the id it last saw is never handed one it cannot resume from.
    """

    kind: str
    cursor: StreamCursor
    fence: int
    index: int
    data: bytes = b""
    detail: str = ""

    @property
    def terminal(self) -> bool:
        """Nothing further will arrive on this stream.

        Broader than "the producer finished": a `timeout` ends the *reader*
        while the producer may still be running, and the distinction is exactly
        what a client needs in order to decide between reconnecting and giving
        up. `kind` says which.
        """
        return self.kind in TERMINAL_KINDS or self.kind == KIND_TIMEOUT

    @property
    def id(self) -> str:
        """This event's `Last-Event-ID`."""
        return self.cursor.encode()

    def as_sse(self) -> ServerSentEvent:
        """This event as one Server-Sent Event.

        A `chunk` whose bytes are valid UTF-8 is `event: chunk` with the text as
        `data`; one that is not is `event: chunk64` with base64, because SSE
        `data` is text and silently replacing an undecodable byte would corrupt
        a payload rather than report it. `wreath.rooms` makes the same call on
        the same evidence, and the two spell it the same way on purpose.
        """
        if self.kind == KIND_CHUNK:
            try:
                return ServerSentEvent(data=self.data.decode("utf-8"), event=KIND_CHUNK, id=self.id)
            except UnicodeDecodeError:
                return ServerSentEvent(
                    data=b64_encode(self.data),
                    event="chunk64",
                    id=self.id,
                )
        return ServerSentEvent(data=self.detail, event=self.kind, id=self.id)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe form, for the WebSocket transport and for tests.

        `data` is the UTF-8 text where the payload is text and base64 under
        `data64` where it is not, for the reason `as_sse` gives.
        """
        payload: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "fence": self.fence,
            "index": self.index,
        }
        if self.kind == KIND_CHUNK:
            try:
                payload["data"] = self.data.decode("utf-8")
            except UnicodeDecodeError:
                payload["data64"] = b64_encode(self.data)
        elif self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True, slots=True)
class _Producer:
    kind: str
    func: Callable[..., Awaitable[None]]
    task: str
    max_attempts: int


class StreamHandle(StreamingResponse):
    """What `Streams.start` hands back: an id to attach to, and where it is.

    A `StreamingResponse` rather than a plain object, because `wreath.app`'s
    coercion ends in a closed `isinstance` check and a duck-typed value with a
    correct `__call__(send)` dies there with *handlers must return a
    response-compatible value*. Subclassing also picks up the deferred-cleanup
    contract that releases a database connection a handler borrowed.

    The body is one JSON object, so the length is known and is sent -- a
    streaming response is the *shape* this needs, not the framing.
    """

    __slots__ = ("key", "state", "task_id")

    def __init__(self, *, key: str, task_id: str, state: str = "queued") -> None:
        self.key = key
        self.task_id = task_id
        self.state = state
        body = JSONResponse(self.as_dict()).body
        super().__init__(
            _one(body),
            status=202,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        )

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "task_id": self.task_id, "state": self.state}


async def _one(body: bytes) -> AsyncIterator[bytes]:
    yield body


class StreamWriter:
    """The producer's end of one stream, for one attempt.

    Handed to a registered producer as its first argument. `write` buffers and
    lets `wreath.log`'s `_Buffer` decide when to issue a statement, so a
    token-at-a-time producer costs one multi-row `INSERT` per flush rather than
    one round trip per token -- the write amplification this whole design is
    challenged on, and the reason `append_many` exists.

    **Every row carries this attempt's fence.** That is what a reader uses to
    skip a superseded range, and it is why nothing here has to be idempotent.
    """

    __slots__ = ("_buffer", "_capacity", "_fence", "_flushes", "_index", "_key", "_written")

    def __init__(self, log: PostgresLog, key: str, *, fence: int, resume_from: int) -> None:
        self._buffer = log.buffered(key)
        self._capacity = log.declaration.flush.capacity
        self._key = key
        self._fence = fence
        self._index = resume_from
        self._written = 0
        self._flushes = 0

    @property
    def key(self) -> str:
        """The stream this writes to."""
        return self._key

    @property
    def fence(self) -> int:
        """This attempt's fence, from `wreath.jobs.JobContext.fence`."""
        return self._fence

    @property
    def index(self) -> int:
        """The `idx` the next row will carry."""
        return self._index

    @property
    def written(self) -> int:
        """Rows offered to the buffer by this attempt."""
        return self._written

    @property
    def flushes(self) -> int:
        """Statements' worth of batches this attempt has issued.

        `written / flushes` is chunks-per-flush, which is the number this
        design's write amplification is measured in.
        """
        return self._flushes

    @property
    def pending(self) -> int:
        """Rows buffered and not yet written."""
        return self._buffer.pending

    async def write(self, data: str | bytes) -> None:
        """Append one chunk, flushing when the policy says to.

        **The flush policy is checked here and nowhere else**, which matters for
        a producer that writes *rarely*. There is no timer task behind this: the
        `Flush(every=...)` threshold is read on the next `write`, so a producer
        emitting one line per tool call leaves that line in the buffer until the
        call after it. At token rates that delay is a few milliseconds and
        invisible; at one chunk a minute it is a minute. A producer whose gaps
        are long calls `flush` itself, exactly as a caller of
        `wreath.log.PostgresLog.buffered` does -- the buffer is driven by its
        owner, and a background task per stream would be a task per stream.

        Args:
            data: `str` is encoded UTF-8; `bytes` travel unchanged. A reader
                delivers bytes and decides how to frame them, so a producer that
                emits binary is not forced through a text round trip.
        """
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        await self._offer(KIND_CHUNK, body=payload, detail="")

    async def flush(self) -> int:
        """Write everything buffered now, returning how many rows landed."""
        landed = await self._buffer.flush()
        if landed:
            self._flushes += 1
        return landed

    async def _offer(self, kind: str, *, body: bytes | None, detail: str) -> None:
        # The precondition, guarded rather than caught: `_Buffer.offer` answers
        # `False` on a full buffer and *counts the row as dropped*, and a dropped
        # row is a hole in a stream whose whole promise is that it has none. So
        # the buffer is drained before it can refuse, and the raise below is the
        # backstop for a capacity this arithmetic got wrong -- a real `raise`,
        # because `python -O` strips every assert and this is a framing rule.
        if self._buffer.pending >= self._capacity:
            await self.flush()
        offered = self._buffer.offer(
            fence=self._fence, idx=self._index, kind=kind, body=body, detail=detail
        )
        if not offered:
            raise RuntimeError(
                f"the buffer for stream {self._key!r} refused a row with "
                f"{self._buffer.pending} of {self._capacity} pending after a flush; "
                "a dropped chunk is a hole in a stream that promises none"
            )
        self._index += 1
        self._written += 1
        if self._buffer.due:
            await self.flush()

    async def _terminate(self, kind: str, detail: str = "") -> None:
        """Write the terminal row and flush, so a reader stops rather than idles."""
        await self._offer(kind, body=None, detail=detail[:2000])
        await self.flush()

    def abandon(self) -> int:
        """Give up on what is buffered, counting it as dropped.

        What a cancelled attempt calls. The rows are lost either way; this makes
        the loss a number on `PostgresLog.dropped` rather than a buffer nobody
        looks at again.
        """
        return self._buffer.abandon()


class Streams:
    """Durable streams over one job queue and one chunk log.

    Args:
        jobs: a `wreath.jobs.JobRunner`. Producers register as tasks on it, so a
            stream inherits retries, fencing, leases, the deadline, and
            cancellation -- there is deliberately no second execution path.
        log: a `wreath.log.PostgresLog` over `declaration()`.
        poll: seconds between reads while tailing.
        idle: seconds a reader waits with nothing new before it says `timeout`.
        on_retry: `"supersede"` (keep the replaced rows, skip them on read) or
            `"truncate"` (delete them when the retried attempt starts). Both
            tell the reader; only one keeps the bytes.
        started_capacity: how many started keys are remembered for the
            attached-versus-started count.
    """

    __slots__ = (
        "_attached_keys",
        "_idle",
        "_jobs",
        "_log",
        "_on_retry",
        "_poll",
        "_producers",
        "_started_keys",
        "attached",
        "cursor_refusals",
        "resumed",
        "started",
        "superseded_rows",
    )

    def __init__(
        self,
        *,
        jobs: Any,
        log: PostgresLog,
        poll: float = DEFAULT_POLL,
        idle: float = DEFAULT_IDLE,
        on_retry: str = "supersede",
        started_capacity: int = 4096,
    ) -> None:
        if poll <= 0:
            raise ValueError("poll must be a positive number of seconds")
        if idle <= 0:
            raise ValueError("idle must be a positive number of seconds")
        if on_retry not in RETRY_POLICIES:
            raise ValueError(
                f"on_retry must be one of {', '.join(RETRY_POLICIES)}, not {on_retry!r}: "
                "'supersede' keeps the replaced rows and skips them on read, "
                "'truncate' deletes them when the retried attempt starts"
            )
        if log.declaration.retain is None:
            raise ValueError(
                f"{log.table} was declared retain=KEEP_FOREVER; a chunk log is "
                "delivery and a delivery buffer that never expires is a transcript "
                "of prompts and completions with no erasure path. Build it with "
                "wreath.streams.declaration()."
            )
        self._jobs = jobs
        self._log = log
        self._poll = poll
        self._idle = idle
        self._on_retry = on_retry
        self._producers: dict[str, _Producer] = {}
        from .cache import BoundedCache

        #: Keys this worker started, so `attached` can be counted against
        #: `started` rather than guessed at. Bounded and TTL'd, because a
        #: process that started a million streams must not remember a million
        #: keys to answer one health question.
        self._started_keys: BoundedCache = BoundedCache(
            max_entries=started_capacity, ttl=log.declaration.retain
        )
        self._attached_keys: BoundedCache = BoundedCache(
            max_entries=started_capacity, ttl=log.declaration.retain
        )
        #: Streams started by this worker.
        self.started = 0
        #: Streams started by this worker that something later attached to. The
        #: gap between this and `started` is the token furnace: work produced and
        #: never read. Both are per worker, like `RoomRegistry.members`.
        self.attached = 0
        #: Attaches that arrived with a `Last-Event-ID` this reader honoured.
        self.resumed = 0
        #: Rows skipped because a later attempt superseded them. Non-zero means
        #: producers are being retried, which is not a fault -- it is the number
        #: that says the fence is doing something.
        self.superseded_rows = 0
        #: `Last-Event-ID` values refused. A client with its keys crossed, or a
        #: header something rewrote. Counted because the reader recovers by
        #: replaying from the start, which is otherwise a silent duplicate.
        self.cursor_refusals = 0

    @property
    def log(self) -> PostgresLog:
        """The chunk log. Its `component`, `retention_pass` and `dropped` are the
        schema, the retention walk, and the honest completeness number."""
        return self._log

    def component(self) -> Any:
        """This module's claim on the wreath schema."""
        return self._log.schema_claim("streams")

    def retention_pass(self, *, name: str = "stream_chunks", **options: Any) -> Any:
        """The counted walk that executes the chunk log's retention window."""
        return self._log.retention_pass(name=name, **options)

    def producer(
        self,
        kind: str,
        *,
        retries: int = 2,
        timeout: float | None = None,
    ) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
        """Register `kind`'s producer as a durable task on the job runner.

        The decorated function is `producer(stream: StreamWriter, *args)`, and
        it runs **detached from any connection** -- in a worker, under a lease,
        with a fence. That is the property everything else here depends on: it
        keeps producing when nobody is attached, and it is re-run by an ordinary
        job retry when the worker holding it dies.

        Registration happens at import time in *every* process, which is why the
        producer is not simply passed to `start`: the worker that runs it never
        calls `start`, so a callable handed over there would have registered
        nowhere that matters.

        `retries` defaults low. A stream retry re-runs the producer, and for a
        priced external call that is a second charge; the fence makes it
        *correct*, not free.

        Args:
            kind: names the task, so it is a bounded SQL-safe identifier.
            retries: retries after the first attempt.
            timeout: seconds one attempt may run, or `None` for the runner's
                default. Must be shorter than the lease; `JobRunner.task`
                checks that.
        """
        validate_identifier(kind, "stream kind")
        if kind in self._producers:
            raise ValueError(f"duplicate stream kind: {kind!r}")
        task = f"stream_{kind}"
        max_attempts = retries + 1

        def register(
            func: Callable[..., Awaitable[None]],
        ) -> Callable[..., Awaitable[None]]:
            entry = _Producer(kind=kind, func=func, task=task, max_attempts=max_attempts)
            self._producers[kind] = entry

            async def run(context: Any, key: str, args: Sequence[Any]) -> None:
                await self._produce(entry, context, key, list(args))

            self._jobs.task(task, retries=retries, timeout=timeout)(run)
            return func

        return register

    def registered(self, kind: str) -> bool:
        """Whether `kind` has a producer in *this* process."""
        return kind in self._producers

    async def start(
        self,
        kind: str,
        *,
        key: str,
        producer: Callable[..., Awaitable[None]] | None = None,
        args: Sequence[Any] = (),
        tenant: str = "",
    ) -> StreamHandle:
        """Enqueue `kind`'s producer for `key` and hand back something to attach to.

        Idempotent in the key: the enqueue carries `key` as its dedup key, so a
        second `start` for a stream already in flight returns the handle of the
        first rather than starting a second producer. One key is one stream --
        compose it (`f"{conversation}:{turn}"`) when a conversation has several.

        `producer=` is accepted for the shape the call site reads best, and is
        checked rather than used: a callable that is not the one registered
        under `kind` is refused by name, because silently preferring one of two
        would mean the worker ran the other.

        Raises:
            ValueError: `kind` has no registered producer in this process, or
                `producer=` is not that producer, or `key` is not a usable key.
        """
        entry = self._producers.get(kind)
        if entry is None:
            known = ", ".join(sorted(self._producers)) or "none"
            raise ValueError(
                f"no producer registered for stream kind {kind!r} (this process has "
                f"{known}); register one with @streams.producer({kind!r}) at import "
                "time, in every process, because the worker that runs it does not "
                "call start()"
            )
        if producer is not None and producer is not entry.func:
            raise ValueError(
                f"start({kind!r}, producer=...) was given "
                f"{getattr(producer, '__name__', producer)!r}, but "
                f"{getattr(entry.func, '__name__', entry.func)!r} is registered under "
                "that kind; the worker runs the registered one, so honouring this "
                "argument here would run two different producers"
            )
        _check_key(key)
        handle = await self._jobs.launch(
            entry.task, key, list(args), key=_dedup(key), tenant=tenant
        )
        self.started += 1
        self._started_keys.set(key, True)
        return StreamHandle(key=key, task_id=handle.task_id, state=handle.state)

    async def cancel(self, key: str, *, reason: str = "cancelled") -> bool:
        """End `key` now: write the terminal row, then cancel the job.

        Terminal row **first**, deliberately. The invariant is that attaching to
        a cancelled stream returns the terminal record rather than hanging, and
        a cancel that fenced the job and then failed to write would leave every
        reader idling until its own timeout.

        There is no third cancellation path: the job's fence is bumped, so
        whatever the running attempt does next matches no row, and its late
        chunks sit behind a terminal record no reader passes.

        Returns:
            Whether a queue row moved. `False` means the job had already
            finished or dead-lettered -- the terminal row is written either way,
            because a reader still needs to be let go.
        """
        _check_key(key)
        fence, index = await self._head(key)
        await self._log.append(
            key,
            fence=fence,
            idx=index + 1,
            kind=KIND_CANCELLED,
            body=None,
            detail=reason[:2000],
        )
        return await self._jobs.cancel(key=_dedup(key))

    async def _produce(self, entry: _Producer, context: Any, key: str, args: list[Any]) -> None:
        """One attempt at one stream, inside a durable job.

        Which of the two retry disciplines applies is decided here, once, on the
        attempt number rather than on anything the producer said -- a first
        attempt supersedes nothing, and a retry either truncates what came
        before or leaves it for the reader to skip.
        """
        if context.attempt > 1 and self._on_retry == "truncate":
            await self._truncate_below(key, context.fence)
        writer = StreamWriter(self._log, key, fence=context.fence, resume_from=0)
        try:
            await entry.func(writer, *args)
        except asyncio.CancelledError:
            # Never swallowed, and never turned into a terminal record: a
            # cancellation here is the supervisor stopping or the deadline
            # firing, and both mean this attempt ends while the *stream* carries
            # on under the next one. Writing `end` would tell every client the
            # generation finished when it is about to be retried. What is
            # buffered cannot be flushed on a cancelled task, so it is counted.
            writer.abandon()
            raise
        except Exception as error:
            # The last attempt is the one that has to say so. Earlier failures
            # are retried, and a terminal row written on attempt 1 of 3 would
            # close every reader on a stream that is about to resume.
            if context.attempt >= entry.max_attempts:
                await writer._terminate(KIND_ERROR, f"{type(error).__name__}: {error}")
            else:
                writer.abandon()
            raise
        await writer._terminate(KIND_END)

    async def _truncate_below(self, key: str, fence: int) -> None:
        """Delete `key`'s rows from attempts before `fence`.

        The simpler discipline, for a stream that declares it tolerates
        restarts. It loses whatever the previous attempt produced -- which is
        the trade -- and a reader that had already consumed part of it is still
        *told*, because the fence rise is what `follow` reads and the rows going
        away does not change that.
        """
        declaration = self._log.declaration
        await self._run(
            f"DELETE FROM {self._log.table} WHERE {declaration.stream} = $1 AND fence < $2",
            key,
            fence,
        )

    async def follow(
        self,
        key: str,
        *,
        since: StreamCursor | str | None = None,
        idle: float | None = None,
        poll: float | None = None,
        limit: int = REPLAY_LIMIT,
    ) -> AsyncIterator[StreamEvent]:
        """Replay from `since`, then tail, until the stream ends or goes quiet.

        The one reader. SSE (`attach`), WebSocket (`push_stream`) and MCP's
        notification stream are three framings of this generator, so there is
        one cursor discipline and one supersede rule rather than three that
        drift.

        **The supersede rule, in full.** A stream's rows are ordered by the log,
        which is commit order, and every row carries the fence of the attempt
        that wrote it. The reader holds the highest fence it has seen for this
        stream -- seeded from the table, so a client arriving after a retry
        never receives the replaced range at all -- and then:

        * a row **below** that fence is skipped. Not hypothetical: a worker
          whose lease expired is still alive and still flushing while its
          replacement produces, and its late rows land *after* the newer ones;
        * a row **above** it means a retry, so `superseded` is emitted before
          the new content and the fence moves up. A client concatenating a
          replaced range renders duplicated text and blames the model, so it is
          told rather than left to notice.

        Args:
            since: a `StreamCursor`, its encoded form, or `None` for the start.
                A string is validated as an index into *this* stream; anything
                else raises.
            idle: seconds with nothing new before `timeout` ends the stream.
            poll: seconds between reads.
            limit: rows one replay page carries.

        Raises:
            ValueError: `since` is not a cursor for `key`.
        """
        _check_key(key)
        start = since if isinstance(since, StreamCursor) else StreamCursor.decode(since, key=key)
        idle_for = self._idle if idle is None else idle
        poll_for = self._poll if poll is None else poll
        if idle_for <= 0 or poll_for <= 0:
            raise ValueError("idle and poll must be positive numbers of seconds")
        if start.cursor != Cursor.start():
            self.resumed += 1
        if self._started_keys.get(key) is not None and self._attached_keys.get(key) is None:
            self._attached_keys.set(key, True)
            self.attached += 1

        fence = max(await self._max_fence(key), start.fence)
        position = StreamCursor(start.token, fence, start.cursor)
        index = -1
        if fence > start.fence > 0:
            # The client read under an attempt that has since been replaced. It
            # learns before it is handed a byte of the new one.
            yield StreamEvent(KIND_SUPERSEDED, position, fence, 0, detail=_SUPERSEDED)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + idle_for
        while True:
            batch = await self._log.read(key, after=position.cursor, limit=limit)
            if batch:
                deadline = loop.time() + idle_for
            for record in batch:
                row_fence = int(record["fence"])
                if row_fence < fence:
                    self.superseded_rows += 1
                    continue
                position = StreamCursor(start.token, row_fence, record.cursor)
                index = int(record["idx"])
                if row_fence > fence:
                    if fence > 0:
                        yield StreamEvent(
                            KIND_SUPERSEDED,
                            position,
                            row_fence,
                            index,
                            detail=_SUPERSEDED,
                        )
                    fence = row_fence
                kind = record["kind"]
                yield StreamEvent(
                    kind,
                    position,
                    row_fence,
                    index,
                    data=bytes(record["body"] or b""),
                    detail=record["detail"] or "",
                )
                if kind in TERMINAL_KINDS:
                    return
            position = StreamCursor(start.token, fence, batch.cursor)
            if loop.time() >= deadline:
                # Not a failure, and not silence either. A producer that never
                # ran and a producer that stalled look identical from here, and
                # both are cases where holding the connection open forever is
                # the wrong answer; the client reconnects with this id and
                # resumes exactly where it stopped.
                yield StreamEvent(KIND_TIMEOUT, position, fence, index, detail=_TIMED_OUT)
                return
            await asyncio.sleep(poll_for)

    def attach(
        self,
        key: str,
        *,
        since: StreamCursor | str | None = None,
        idle: float | None = None,
        poll: float | None = None,
        authorize: Callable[[str], bool] | None = None,
    ) -> Response | SSEResponse:
        """An SSE response replaying from `since` and then tailing `key`.

        Synchronous, like `wreath.progress.progress_stream`: it returns a
        response rather than awaiting one, so a refusal is a plain response and
        the stream only opens once the refusals are past.

        `authorize(key) -> bool` decides whether this caller may read that
        stream. It matters: a stream key is usually a conversation id, and
        without a guard whoever can guess one can read a generation. A refusal
        answers `404`, identical to an unknown key, because a distinct `403`
        would confirm which keys exist.

        A `since` that does not parse answers `400` naming why, rather than
        starting from zero -- a silent restart would hand the client a second
        copy of everything it already rendered, which is the failure mode this
        whole module exists to remove.
        """
        if authorize is not None and not authorize(key):
            return _unknown_stream(key)
        try:
            _check_key(key)
        except ValueError as error:
            # Not a cursor refusal: the key is a path parameter and this is the
            # handler's own argument being wrong, which is a different fault
            # from a client replaying somebody else's `Last-Event-ID`. Counting
            # them together would make the doctor finding below say the wrong
            # thing about which end has the problem.
            return JSONResponse({"error": str(error), "key": key[:64]}, status=400)
        try:
            resume = (
                since if isinstance(since, StreamCursor) else StreamCursor.decode(since, key=key)
            )
        except ValueError as error:
            self.cursor_refusals += 1
            return JSONResponse({"error": str(error), "key": key}, status=400)
        return SSEResponse(
            self._sse(key, resume, idle, poll),
            headers=[(b"x-stream-key", key.encode("utf-8"))],
        )

    async def _sse(
        self, key: str, since: StreamCursor, idle: float | None, poll: float | None
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in self.follow(key, since=since, idle=idle, poll=poll):
            yield event.as_sse()

    def counters(self) -> Any:
        """This registry's counters, for `wreath.metrics.collect`.

        `started` against `attached` is the pair worth watching, and a
        divergence between them is only visible if both are scraped.
        """
        from .metrics import Counters

        return Counters(
            subsystem="streams",
            # Labelled by the log it writes to: one registry per
            # application is the common case, and two over different logs
            # must not collapse into one series.
            instance=str(getattr(self._log, "table", "") or "streams"),
            values=self.stats(),
        )

    def stats(self) -> dict[str, int]:
        """Every counter this registry keeps, by name.

        `started` against `attached` is the one worth an alert. The producer
        runs whether or not anybody is reading, which is the point of the
        design; an application where the two numbers diverge is paying a model
        for output nobody asked for twice. Both are this worker's, the way
        `RoomRegistry.members` is.
        """
        return {
            "started": self.started,
            "attached": self.attached,
            "resumed": self.resumed,
            "superseded_rows": self.superseded_rows,
            "cursor_refusals": self.cursor_refusals,
            "dropped": self._log.dropped,
        }

    async def _max_fence(self, key: str) -> int:
        """The highest attempt that has settled rows for `key`, or 0.

        Gated on the same horizon `wreath.log.read` is, so the seed and the
        rows it filters agree: a fence read from a transaction still in flight
        would skip rows on the strength of content nobody can yet see.
        """
        declaration = self._log.declaration
        value = await self._fetchval(
            f"SELECT coalesce(max(fence), 0) FROM {self._log.table} "
            f"WHERE {declaration.stream} = $1 "
            "AND xid < pg_snapshot_xmin(pg_current_snapshot())",
            key,
        )
        return int(value or 0)

    async def _head(self, key: str) -> tuple[int, int]:
        """`(fence, idx)` of `key`'s newest row, or `(0, -1)` for an empty stream.

        Ungated, unlike `_max_fence`: this is what `cancel` writes *after*, and
        a terminal row that landed behind an in-flight chunk would let a reader
        past it.
        """
        declaration = self._log.declaration
        row = await self._fetchrow(
            f"SELECT fence, idx FROM {self._log.table} "
            f"WHERE {declaration.stream} = $1 ORDER BY fence DESC, idx DESC LIMIT 1",
            key,
        )
        if row is None:
            return 0, -1
        return int(row[0]), int(row[1])

    async def _fetchval(self, sql: str, *args: Any) -> Any:
        database = self._log.database
        connection = await database.acquire("write")
        try:
            return await connection.fetchval(sql, *args)
        finally:
            await database.release("write", connection)

    async def _fetchrow(self, sql: str, *args: Any) -> Any:
        database = self._log.database
        connection = await database.acquire("write")
        try:
            return await connection.fetchrow(sql, *args)
        finally:
            await database.release("write", connection)

    async def _run(self, sql: str, *args: Any) -> Any:
        database = self._log.database
        connection = await database.acquire("write")
        try:
            return await connection.execute(sql, *args)
        finally:
            await database.release("write", connection)


#: The text a `superseded` event carries. One string, so a client can match on
#: it and a test can assert the distinct message rather than the field name.
_SUPERSEDED: Final = (
    "a retried attempt replaced everything delivered so far; discard it and render what follows"
)

#: The text a `timeout` event carries.
_TIMED_OUT: Final = "nothing arrived within the idle window; reconnect with this id to resume"


def _dedup(key: str) -> str:
    """The queue dedup key for a stream key. One key is one stream."""
    return f"stream:{key}"


def _check_key(key: str) -> None:
    """Guard the one value a caller supplies that reaches an index and a header.

    Three refusals, and the third is the one that matters. A stream key is
    usually a path parameter, so it is request text; `attach` echoes it into an
    `x-stream-key` response header, and a CR or LF in a header value ends the
    field -- which is response splitting, out of a route argument. Every control
    character goes, rather than the two that are exploitable today, because the
    set of characters an intermediary treats as a terminator is not this
    module's to enumerate.
    """
    if not isinstance(key, str) or not key:
        raise ValueError("a stream key must be a non-empty string")
    size = len(key.encode("utf-8"))
    if size > MAX_KEY_BYTES:
        raise ValueError(
            f"a stream key must be at most {MAX_KEY_BYTES} bytes and this one is "
            f"{size}; it indexes a table and is echoed into every event id"
        )
    for character in key:
        if character < " " or character == "\x7f":
            raise ValueError(
                "a stream key must hold no control character: it is echoed into a "
                f"response header, where {character!r} would end the field and let "
                "a path parameter append headers of its own"
            )


def _unknown_stream(key: str) -> Response:
    return JSONResponse({"error": "unknown or expired stream", "key": key}, status=404)


async def push_stream(
    websocket: Any,
    streams: Streams,
    key: str,
    *,
    since: StreamCursor | str | None = None,
    idle: float | None = None,
    poll: float | None = None,
) -> None:
    """Push `key`'s events as JSON text frames over an accepted WebSocket.

    The same reader as `attach`, framed for a socket that already exists --
    which is the case `wreath.rooms` leaves you in, where a client is already
    connected for something else and a second HTTP request would be a second
    connection for no reason. Each frame carries `id`, so a client that
    reconnects resumes exactly as an `EventSource` would.
    """
    from ._json import dumps as _dumps

    async for event in streams.follow(key, since=since, idle=idle, poll=poll):
        encoded = _dumps(event.as_dict())
        await websocket.send_text(
            encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded
        )


def check_stream_attachment(streams: Streams, *, ratio: float = 0.5) -> list[str]:
    """Findings about streams that were produced and never read.

    In `wreath.doctor`'s shape -- a list of human-readable findings, empty when
    there is nothing to say -- because this is a doctor question and not a
    metric: "the producer runs whether or not anyone reads it" is the design,
    and "most of them are never read" is a bill.

    Args:
        ratio: the fraction of started streams that must have been attached to
            before this says nothing. Below it, the finding names both numbers.
    """
    findings: list[str] = []
    stats = streams.stats()
    started = stats["started"]
    attached = stats["attached"]
    # No `started and` in front of this. `attached` only moves for a key this
    # worker started, so `started == 0` implies `attached == 0` and the
    # comparison is already false; the extra clause was a second spelling of one
    # condition, which a mutation run found by dropping it and changing nothing.
    if attached < started * ratio:
        findings.append(
            f"{attached} of {started} streams started on this worker were ever "
            "attached to. A producer runs detached from the connection by design, "
            "so an unread stream still spends whatever the producer spends; check "
            "that clients attach after start(), or stop starting the ones nobody "
            "reads."
        )
    if stats["dropped"]:
        findings.append(
            f"{stats['dropped']} buffered chunk(s) were lost to a full buffer or a "
            "failed flush, which is a hole in a stream. Raise Flush(capacity=...) "
            "or find out why the flush is failing."
        )
    if stats["cursor_refusals"]:
        findings.append(
            f"{stats['cursor_refusals']} Last-Event-ID value(s) were refused. A "
            "client is sending a cursor for another stream, or something between "
            "it and here is rewriting the header."
        )
    return findings
