"""Wreath logging — structured records that ride the flight recorder's ring.

Wreath does not ship a logging *subsystem*. A log record is one 64-byte cell on
the ring the Native Flight Recorder already owns, published by the same writer
that publishes a request completion, and joined to its trace by request id. That
is what makes a record correlated for free: the trace and span ids are already
in the native request context, so nothing on the request path reads a
`ContextVar` or constructs an OpenTelemetry object to attach them.

Two tiers, both landing in the same ring:

```python
from wreath import logging as log

# Fast tier: interned once, at import. The record carries site_id + arguments.
DENIED = log.event(
    "auth.denied",
    "user {user} denied {resource}",
    level=log.WARN,
    fields=(log.field("user", int), log.field("resource", str, log.RAW)),
)

async def handler(request):
    DENIED(user_id, "orders")          # no dict, no format, no frame walk

    log.info("cache miss for {key}", key=key)   # ergonomic tier, kwargs dict
```

**Redaction is deny-by-default**, matching `wreath.recording`. A scalar is
written verbatim; anything string-shaped is replaced by a keyed, process-local
fingerprint unless its field declares `RAW`. The ergonomic tier declares
nothing, so every string it carries is fingerprinted — which is exactly why it
can stay a one-liner.

**Levels are checked in one place.** A disabled site returns immediately, and
`bool(SITE)` answers the same question the call would, so a hot loop can skip
the call and its argument tuple:

```python
if DENIED:              # only where a benchmark says it pays
    DENIED(user_id, resource)
```

Formatting is deferred: the record holds arguments, the registry holds the
template, and `render` puts them together off the request path.

**A published record is packed in C** -- `wreath_nfr_log` writes it straight
into a ring cell, with no intermediate object -- and the Python packer beside it
is the twin that C is checked against byte for byte, not a fallback. It is also
what runs when there is no ring to pack into, when a record is buffered for a
possible promotion, and when the caller is not the loop. Which is which, and
why, is written once: the head of the log-record section in
`wreath._flight_schema`, immediately above `Severity`.
`docs/plans/first-class-logging.md` is the longer form, with the measurements.

Measured on CPython 3.14, 2026-07-28 (`benchmarks/bench_logging.py`; medians,
interleaved arms, A/A floor): a two-argument `SITE(a, b)` costs **0.42us**
against structlog's 2.59us and stdlib's 2.85-3.88us, a *disabled* `DEBUG(...)`
costs **0.07us**, and a record buffered for promotion costs **3.0us** -- which
is the one number here that is not good, and the plan says so plainly.

See `docs/guides/observability.md` and `docs/reference/logging.md`.
"""

from __future__ import annotations

import logging as _stdlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from threading import get_ident as _thread_id
from typing import Any, Final
from typing import Protocol as _Protocol

from ._flight_schema import (
    LOG_FLAG_EVENT_FIELDS,
    LOG_FLAG_OFF_LOOP,
    LOG_FLAG_REDACTED,
    LOG_MAX_ARGS,
    CaptureDisposition,
    LogArg,
    LogCell,
    Severity,
    severity_from_stdlib,
    severity_text,
)
from ._logscratch import (
    DEFAULT_LIMITER_CAPACITY,
    DEFAULT_OFF_LOOP_CAPACITY,
    DEFAULT_SCRATCH_BUDGET,
    LogSamplingPolicy,
    OffLoopStage,
    RequestLogBuffer,
    SiteLimiter,
)
from ._logsite import (
    DEFAULT_SITE_CAPACITY,
    LogField,
    LogSite,
    LogSiteError,
    SiteCounters,
    SiteRegistry,
    infer_field,
    pack_value,
    specs_for,
)
from ._logsite import attributes as _attributes
from ._logsite import declare as _declare
from ._logsite import render as _render

__all__ = [
    "DEBUG",
    "ERROR",
    "FATAL",
    "HASHED",
    "INFO",
    "LENGTH",
    "LogEvent",
    "LogRuntime",
    "RequestScope",
    "LogSiteError",
    "StdlibBridge",
    "MASKED",
    "RAW",
    "TRACE",
    "WARN",
    "LogSamplingPolicy",
    "arity_mismatch_count",
    "active",
    "attributes",
    "begin_request",
    "begin_request_for",
    "begin_request_seeded",
    "current_request_id",
    "current_scope",
    "recorder_emitter",
    "recorder_sink",
    "set_field",
    "bridged_loggers",
    "stdlib_bridge",
    "debug",
    "error",
    "event",
    "fatal",
    "field",
    "finish_request",
    "finish_request_for",
    "finish_session",
    "info",
    "installed",
    "off_loop_counts",
    "render",
    "request_scope",
    "severity_text",
    "site_overflow_count",
    "testing_runtime",
    "trace",
    "type_mismatch_count",
    "warn",
]

#: Severities, at the base of each OpenTelemetry band.
TRACE: Final = Severity.TRACE
DEBUG: Final = Severity.DEBUG
INFO: Final = Severity.INFO
WARN: Final = Severity.WARN
ERROR: Final = Severity.ERROR
FATAL: Final = Severity.FATAL

#: Redaction dispositions, re-exported from `wreath.recording` so a call site
#: names them without importing the recording policy machinery.
RAW: Final = CaptureDisposition.RAW
HASHED: Final = CaptureDisposition.HASHED
MASKED: Final = CaptureDisposition.MASKED
LENGTH: Final = CaptureDisposition.LENGTH

#: A sink receives finished cells. The real one hands them to the recorder's
#: ring; the default one drops them, because logging must survive being called
#: before the server boots.
Sink = Callable[[LogCell], None]

#: The native emitter's signature: `(site_id, severity, request_id, flags,
#: dropped_siblings, specs, values, k0, k1) -> int`, where the returned int is
#: `(type_mismatches << 1) | published`. Two answers in one int because the
#: alternative is a tuple allocation on the request path.
NativeEmitter = Callable[
    [int, int, int, int, int, bytes, tuple[object, ...], int, int], int
]


class LogRuntime:
    """One process's logging state: a site registry, a level, and a sink.

    Ownership is explicit rather than ambient. `wreath.logging` installs exactly
    one runtime; the server replaces it when a recorder exists, and
    `testing_runtime` swaps a fresh one in so a test never inherits another
    test's site ids.
    """

    __slots__ = (
        "capture_level",
        "counters",
        "level",
        "limiter",
        "native",
        "off_loop",
        "off_loop_capacity",
        "registry",
        "scratch_budget",
        "sink",
        "writer_thread",
    )

    def __init__(
        self,
        sink: Sink | None = None,
        *,
        level: Severity = INFO,
        capture_level: Severity | None = None,
        site_capacity: int = DEFAULT_SITE_CAPACITY,
        sampling: LogSamplingPolicy | None = None,
        limiter_capacity: int = DEFAULT_LIMITER_CAPACITY,
        scratch_budget: int = DEFAULT_SCRATCH_BUDGET,
        native: NativeEmitter | None = None,
        off_loop_capacity: int = DEFAULT_OFF_LOOP_CAPACITY,
    ) -> None:
        self.registry = SiteRegistry(site_capacity)
        #: The native emitter, when a recorder provides one. Packing a record in
        #: Python and encoding it costs ~2.5us before the ring ever sees it; the
        #: same work in C is ~0.2us, which is why this exists and why the pure
        #: path stays as its checked twin rather than being deleted. None means
        #: the pure path -- which is what a test capture, `testing_runtime` and
        #: any non-recorder sink get, because there is nothing else to pack for.
        self.native = native
        #: At and above this, a record is published.
        self.level = level
        #: Below this, a call does nothing. Between the two a record is buffered
        #: for promotion. Defaults to `level`, which collapses the two into the
        #: single threshold a caller who did not ask for buffering expects.
        self.capture_level = level if capture_level is None else capture_level
        self.sink = sink
        self.counters = SiteCounters()
        self.limiter = SiteLimiter(sampling, capacity=limiter_capacity)
        self.scratch_budget = scratch_budget
        #: The thread allowed to write to the ring, and the queue for everything
        #: else. Both stay unset until `bind_writer`: a sink that is not a ring
        #: has no single-writer rule to keep, and paying for one would tax every
        #: test capture and every plain-callable sink for nothing.
        self.off_loop_capacity = off_loop_capacity
        self.off_loop: OffLoopStage | None = None
        self.writer_thread: int | None = None

    def bind_writer(self, thread_id: int | None = None) -> None:
        """Declare which thread may write to the ring, and open the slow path.

        Called by the server on the event loop, once, after the recorder exists.
        Until it is called `off_loop` is None and every record goes straight to
        the sink -- which is right for a process with no ring behind the sink
        (a test capture, a list, a file) and is why the check is not free-standing
        module state.
        """
        self.writer_thread = _thread_id() if thread_id is None else thread_id
        self.off_loop = OffLoopStage(self.off_loop_capacity)

    def _stage_off_loop(self, cell: LogCell) -> bool:
        """Stage a record made off the loop. False when there is no slow path."""
        stage = self.off_loop
        if stage is None or _thread_id() == self.writer_thread:
            return False
        stage.stage(replace(cell, flags=cell.flags | LOG_FLAG_OFF_LOOP))
        return True

    def drain_off_loop(self) -> int:
        """Publish everything staged from off the loop. Returns the count.

        Must run on the writer thread; the server drives it from a loop task on
        the same interval the writer uses. Records arrive one interval late and
        carry `LOG_FLAG_OFF_LOOP` so a reader can tell a late record from a
        reordered one.
        """
        stage = self.off_loop
        if stage is None:
            return 0
        records = stage.drain()
        sink = self.sink
        if sink is None:
            self.counters.dropped_no_runtime += len(records)
            return 0
        for cell in records:
            sink(cell)
        return len(records)

    def emit(self, cell: LogCell) -> None:
        """Hand a finished record to the sink, or count it as dropped."""
        if self.sink is None:
            self.counters.dropped_no_runtime += 1
            return
        if self._stage_off_loop(cell):
            return
        self.sink(cell)

    def publish(
        self,
        site: LogSite,
        request_id: int,
        severity: Severity,
        values: tuple[object, ...],
        flags: int = 0,
        *,
        limited: bool = True,
        specs: bytes | None = None,
    ) -> bool:
        """Pack and publish one record in C. False when there is no emitter.

        The caller has already decided this record is being published now -- the
        level check, the scratch decision and the limiter all ran above -- so
        this is packing and nothing else. It returns a bool rather than raising
        so the pure path stays one `if` away, which is what makes the two
        interchangeable and therefore comparable.

        `limited` says whether this record went through the per-call-site
        limiter, and so whether it should carry that site's outstanding drop
        count. The canonical line's field records did not, and taking the
        counter for them would report a suppression on a record that was never
        subject to one.

        `specs` overrides the site's own blob, for the kwargs tiers, where an
        interned template's declared types are whichever call arrived first --
        see `_logsite.specs_for`. A registered site never needs it: its fields
        are its declaration.
        """
        native = self.native
        if native is None:
            return False
        if self.off_loop is not None and _thread_id() != self.writer_thread:
            # Off the loop, and the ring has exactly one writer. Refuse, so the
            # caller packs a `LogCell` on the pure path and `emit` stages it.
            return False
        key = self.registry.key
        outcome = native(
            site.site_id,
            severity,
            request_id,
            flags,
            self.limiter.take_dropped(site.site_id) if limited else 0,
            site.specs if specs is None else specs,
            values,
            key[0],
            key[1],
        )
        mismatches = outcome >> 1
        if mismatches:
            self.counters.type_mismatch += mismatches
        return True


#: The installed runtime. A module-level singleton by necessity -- site ids must
#: be stable for the life of the process -- but never written except through
#: `install` / `testing_runtime`, so the ownership stays traceable.
_RUNTIME: LogRuntime = LogRuntime()

#: Whether the installed runtime has a sink, cached as a plain module global.
#: The request path reads this *before* touching a `ContextVar` or the native
#: context, because both of those are calls into C and `wreath-request-trace`
#: counts them: a server with a recorder and no logging runtime must add no
#: crossings at all, and a bare `LOAD_GLOBAL` is how that stays true. Written
#: only by `install`, so it cannot drift from `_RUNTIME.sink`.
_ACTIVE: bool = False


def installed() -> LogRuntime:
    """The runtime records are currently emitted into."""
    return _RUNTIME


def install(runtime: LogRuntime) -> LogRuntime:
    """Replace the installed runtime, returning the previous one."""
    global _RUNTIME, _ACTIVE
    previous, _RUNTIME = _RUNTIME, runtime
    _ACTIVE = runtime.sink is not None
    return previous


@contextmanager
def testing_runtime(
    sink: Sink | None = None,
    *,
    level: Severity = DEBUG,
    capture_level: Severity | None = None,
    site_capacity: int = DEFAULT_SITE_CAPACITY,
    sampling: LogSamplingPolicy | None = None,
    limiter_capacity: int = DEFAULT_LIMITER_CAPACITY,
    scratch_budget: int = DEFAULT_SCRATCH_BUDGET,
) -> Iterator[list[LogCell]]:
    """Install a fresh runtime for the duration of a block, capturing records.

    Yields the list of cells emitted inside the block. Any `sink` given is also
    called, so a test can assert on both the capture and its own collector.
    Nests: the previous runtime is restored on exit, whatever happens.
    """
    captured: list[LogCell] = []

    def capture(cell: LogCell) -> None:
        captured.append(cell)
        if sink is not None:
            sink(cell)

    previous = install(
        LogRuntime(
            capture,
            level=level,
            capture_level=capture_level,
            site_capacity=site_capacity,
            sampling=sampling,
            limiter_capacity=limiter_capacity,
            scratch_budget=scratch_budget,
        )
    )
    try:
        yield captured
    finally:
        install(previous)



#: The current request's log buffer, bound only inside `request_scope`. Read
#: with `.get(None)`: a record outside a request pays one ContextVar lookup and
#: a predicted branch, which is the same shape `_flight_markers` uses for phase
#: propagation.
_SCRATCH: ContextVar[RequestLogBuffer] = ContextVar("wreath_log_scratch")

#: Names of the stdlib loggers a bridge is attached to, so `wreath.doctor` can
#: tell a genuinely split stream from one that is already joined.
_BRIDGED: set[str] = set()

#: The current request's scope, for `set_field`. Bound and reset alongside
#: `_SCRATCH` so the two can never disagree about which request is current.
_SCOPE: ContextVar[RequestScope] = ContextVar("wreath_log_scope")

#: Application fields attached per request before overflow. Wide events are the
#: point, so this is generous; it is a ceiling against a loop that calls `set`,
#: not a style guideline.
DEFAULT_FIELD_BUDGET: Final = 64


class RequestScope:
    """Handle on one request's buffered records.

    Obtained from `request_scope`. The server finishes it with the recorder's
    verdict; application code can force publication with `promote` when it sees
    an anomaly the framework cannot.
    """

    __slots__ = ("_buffer", "_field_budget", "_fields", "_fields_dropped", "_finished")

    def __init__(self, buffer: RequestLogBuffer, field_budget: int) -> None:
        self._buffer = buffer
        self._finished = False
        self._fields: dict[str, tuple[object, bool]] = {}
        self._field_budget = field_budget
        self._fields_dropped = 0

    @property
    def request_id(self) -> int:
        return self._buffer.request_id

    @property
    def held(self) -> int:
        """Records currently buffered and not yet published or discarded."""
        return self._buffer.held

    @property
    def dropped(self) -> int:
        """Records the per-request budget refused."""
        return self._buffer.dropped

    @property
    def fields(self) -> int:
        """Application fields attached to the canonical log line so far."""
        return len(self._fields)

    @property
    def fields_dropped(self) -> int:
        """Fields the per-request budget refused."""
        return self._fields_dropped

    def set(self, key: str, value: object, *, raw: bool = False) -> None:
        """Attach one field to this request's canonical log line.

        Fields follow the same deny-by-default rule as log arguments: a scalar
        is written, a string is fingerprinted unless `raw=True`. A wide event is
        exactly where a tenant name and an access token sit side by side.

        Setting a key twice keeps the last value, so a later, better-informed
        write wins -- which is how a request that refines its own description as
        it runs expects this to behave.
        """
        if key not in self._fields and len(self._fields) >= self._field_budget:
            self._fields_dropped += 1
            return
        self._fields[key] = (value, raw)

    def promote(self) -> None:
        """Publish this request's buffered records when the scope finishes.

        For the case the framework cannot detect: a 200 response that was
        nonetheless wrong.
        """
        self._buffer.promote()

    def finish(self, *, promoted: bool) -> int:
        """Publish or discard the buffer. Returns the number published.

        `promoted` is the recorder's verdict -- the error or slow promotion the
        completion cell already carries. Calling twice is harmless; the second
        call finds an empty buffer.
        """
        self._finished = True
        published = self._buffer.finish(promoted=promoted, emit=_RUNTIME.emit)
        self._publish_fields()
        return published

    def _publish_fields(self) -> None:
        """Emit the attached fields as event-field records for this request.

        A cell holds LOG_MAX_ARGS arguments, so a wide event spans several. They
        are ordinary log cells flagged LOG_FLAG_EVENT_FIELDS: the projector
        already joins every record to its trace by request id, so the canonical
        line needs no separate channel, no new cell kind, and no second
        assembly path.
        """
        if not self._fields:
            return
        runtime = _RUNTIME
        items = list(self._fields.items())
        self._fields = {}
        for start in range(0, len(items), LOG_MAX_ARGS):
            chunk = items[start : start + LOG_MAX_ARGS]
            specs: list[LogField] = []
            flags = LOG_FLAG_EVENT_FIELDS
            for name, (value, raw) in chunk:
                spec = infer_field(name, value)
                if raw and spec.disposition is not RAW:
                    spec = LogField(name, spec.type, RAW)
                specs.append(spec)
            template = " ".join(f"{name}={{{name}}}" for name, _ in chunk)
            site = runtime.registry.intern_template(template, INFO, tuple(specs))
            values = tuple(value for _name, (value, _raw) in chunk)
            if runtime.publish(
                site,
                self._buffer.request_id,
                INFO,
                values,
                flags=flags,
                limited=False,
                specs=specs_for(site, tuple(specs)),
            ):
                continue
            packed: list[LogArg] = []
            for spec, value in zip(specs, values, strict=True):
                arg, mismatched = pack_value(runtime.registry, value, spec)
                if mismatched:
                    runtime.counters.type_mismatch += 1
                if arg.redacted:
                    flags |= LOG_FLAG_REDACTED
                packed.append(arg)
            runtime.emit(
                LogCell(
                    request_id=self._buffer.request_id,
                    site_id=site.site_id,
                    severity=INFO,
                    args=tuple(packed),
                    flags=flags,
                )
            )


class _InertScope:
    """Stands in for a scope when logging is not running.

    `request.event.set(...)` must never raise and must never require the caller
    to know whether telemetry is configured, so the accessor always returns
    something with the same shape. Every method is a no-op and every counter
    reads zero.
    """

    __slots__ = ()

    @property
    def request_id(self) -> int:
        return 0

    @property
    def held(self) -> int:
        return 0

    @property
    def dropped(self) -> int:
        return 0

    @property
    def fields(self) -> int:
        return 0

    @property
    def fields_dropped(self) -> int:
        return 0

    def set(self, key: str, value: object, *, raw: bool = False) -> None:
        return

    def promote(self) -> None:
        return

    def finish(self, *, promoted: bool) -> int:
        return 0


#: The single inert scope handed out when nothing is bound. Shared because it
#: holds no state.
INERT_SCOPE: Final = _InertScope()


def current_scope() -> RequestScope | _InertScope:
    """The current request's scope, or an inert stand-in.

    Backs `Request.event`. Never returns None, so `request.event.set(...)` is
    safe to write unconditionally -- application code should not have to branch
    on whether the operator configured telemetry.
    """
    if not _ACTIVE:
        return INERT_SCOPE
    scope = _SCOPE.get(None)
    return INERT_SCOPE if scope is None else scope


@contextmanager
def request_scope(
    request_id: int, *, budget: int | None = None, field_budget: int = DEFAULT_FIELD_BUDGET
) -> Iterator[RequestScope]:
    """Bind a per-request log buffer for the duration of a block.

    TRACE and DEBUG records made inside are held rather than published; INFO and
    above pass straight through, carrying the request id. Leaving the block
    without calling `finish` discards whatever was held: an escaped scope is
    inert, never a leak and never a late publication.
    """
    runtime = _RUNTIME
    buffer = RequestLogBuffer(
        request_id, budget if budget is not None else runtime.scratch_budget
    )
    scope = RequestScope(buffer, field_budget)
    token = _SCRATCH.set(buffer)
    scope_token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(scope_token)
        _SCRATCH.reset(token)
        # Whatever happened, the buffer stops being a reference to live records.
        buffer.finish(promoted=False, emit=runtime.emit)


def set_field(key: str, value: object, *, raw: bool = False) -> None:
    """Attach a field to the current request's canonical log line, if any.

    The form helper code deep in a call stack can use without threading a
    request object down to it. Outside a request it is a no-op, so a helper does
    not have to know whether it is serving one.
    """
    scope = _SCOPE.get(None)
    if scope is not None:
        scope.set(key, value, raw=raw)


def current_request_id() -> int:
    """The request a record emitted right now would be attributed to, or 0."""
    buffer = _SCRATCH.get(None)
    return 0 if buffer is None else buffer.request_id


class _HasRequestId(_Protocol):
    """The one thing a native request context is needed for here."""

    def _flight_request_id(self) -> int: ...


class _HasStatus(_Protocol):
    """The one thing a finished response is read for here."""

    @property
    def status(self) -> int: ...


class _PublishesCells(_Protocol):
    """The one method a sink needs from a recorder.

    Structural rather than an import of the native `Recorder`, so the pure
    oracle and a test double satisfy it without inheriting anything.
    """

    def publish_log(self, cell: bytes, /) -> bool: ...


def recorder_emitter(recorder: object) -> NativeEmitter | None:
    """The recorder's native emitter, or None when it has no C to offer.

    The pure oracle and every test double satisfy `_PublishesCells` without
    having a `log`; they get the Python packer, which is the twin the native one
    is checked against, so the two are never both required to exist.
    """
    native = getattr(recorder, "log", None)
    return native if callable(native) else None


def recorder_sink(recorder: _PublishesCells) -> Sink:
    """A sink that publishes records into a recorder's ring.

    This is the wiring that makes a log record an ordinary ring cell: it goes
    through the same single-writer publish, the same one capacity check, and the
    same `RING_FULL` accounting as a request completion, and the projector then
    joins it to its trace by request id with no extra machinery.

    A full ring returns False, which is a counted drop rather than an error --
    the same posture the recorder already takes for a completion it cannot fit.
    """
    publish = recorder.publish_log

    def sink(cell: LogCell) -> None:
        publish(cell.encode())

    return sink


def active() -> bool:
    """Whether a runtime with a sink is installed, i.e. records go somewhere."""
    return _RUNTIME.sink is not None


def begin_request_for(context: _HasRequestId) -> RequestScope | None:
    """Open a log scope for a native request context, if logging is running.

    Takes the context rather than an id so the runtime check comes *first*: the
    request id lives in C, and reading it is a boundary crossing this must not
    make on behalf of a feature nobody enabled. An app with a recorder and no
    logging runtime therefore pays one attribute load and a branch, and no
    crossing at all -- `wreath-request-trace` is the gate that noticed.
    """
    if not _ACTIVE:
        return None
    return begin_request(context._flight_request_id())


def begin_request_seeded(scope: dict[str, object]) -> RequestScope | None:
    """Open a log scope from a dict scope's seeded `_wreath_flight` value.

    HTTP/2, HTTP/3 and a WebSocket session dispatch without a request-context
    object, so their protocols seed the recorder's request id into the scope
    dict. The value is an int only while it is still that seeded id; once Python
    has attributed the route it is a `(route_id, plan_id)` tuple, so the class
    check is also what makes calling this twice harmless.

    The *scope* goes in rather than the value, and `_ACTIVE` is read before
    anything else, because subscripting a dict is a call into C:
    `wreath-request-trace` counts those, and it counted this one in
    `pre_activation` -- the phase the framework guards hardest -- until the
    lookup moved behind this check.
    """
    if not _ACTIVE:
        return None
    seeded = scope["_wreath_flight"]
    if seeded.__class__ is not int:
        return None
    return begin_request(seeded)  # type: ignore[arg-type]


def begin_request(
    request_id: int, *, budget: int | None = None, field_budget: int | None = None
) -> RequestScope | None:
    """Open a log scope for the request the caller is about to serve.

    The non-context-manager form of `request_scope`, for the server, which
    begins a request in one method and completes it in another. Returns None
    when no runtime is installed, so the caller's gate is one `is not None`.

    The bindings are set without a reset token, exactly as `_flight_markers`
    binds the phase marker and for the same reason: the request runs in its own
    task, whose context dies with it. A scope that escapes anyway is inert --
    `finish` empties the buffer whatever happens, so nothing is published late
    and nothing is held alive.
    """
    runtime = _RUNTIME
    if runtime.sink is None:
        return None
    buffer = RequestLogBuffer(
        request_id, budget if budget is not None else runtime.scratch_budget
    )
    scope = RequestScope(
        buffer, field_budget if field_budget is not None else DEFAULT_FIELD_BUDGET
    )
    _SCRATCH.set(buffer)
    _SCOPE.set(scope)
    return scope


def finish_session(*, promoted: bool) -> int:
    """Close the current scope with an explicit verdict.

    For a WebSocket session, which has no response to read a status from: the
    caller already knows whether the session ended badly. Costs one global read
    when no logging runtime is installed.
    """
    if not _ACTIVE:
        return 0
    scope = _SCOPE.get(None)
    if scope is None:
        return 0
    return scope.finish(promoted=promoted)


def finish_request_for(response: _HasStatus) -> int:
    """Close the current request's scope, taking the verdict from a response.

    Takes the response rather than a boolean for the same reason
    `begin_request_for` takes the context: reading a native response's status is
    a boundary crossing, and it must not happen for a feature nobody enabled.
    The bound-scope check comes first, so a request with no logging runtime pays
    one `ContextVar.get(None)` and nothing else.
    """
    if not _ACTIVE:
        return 0
    scope = _SCOPE.get(None)
    if scope is None:
        return 0
    # Mirrors the recorder's own rule: a 5xx is what ERROR_PROMOTED marks. The
    # completion flags are not readable here -- C sets them after this returns.
    return scope.finish(promoted=response.status >= 500)


def finish_request(*, promoted: bool) -> int:
    """Close the current request's log scope, if one is open.

    The other half of `begin_request`, for a server that opens a request in one
    method and completes it in another. Returns the number of buffered records
    published; zero when no scope is bound, which is the common case for a
    process with no logging runtime.

    Calling it twice is harmless -- the second call finds an empty buffer -- and
    never calling it costs nothing beyond the discarded records, because the
    binding dies with the request's task.
    """
    scope = _SCOPE.get(None)
    if scope is None:
        return 0
    return scope.finish(promoted=promoted)


def field(
    name: str, type_: type, disposition: CaptureDisposition | None = None
) -> LogField:
    """Declare one argument of a call site.

    The declaration carries the name, the type, and the redaction disposition
    together, so a reviewer reads one line to know what reaches disk. Omitting
    the disposition means deny-by-default: scalars raw, strings fingerprinted.
    """
    return _declare(name, type_, disposition)


class LogEvent:
    """A registered call site, callable with the statement's dynamic arguments.

    Instances are built by `event` at import and called on the request path.
    Calling one that is below the installed level returns after a level compare;
    `bool(site)` answers the same question without the call.
    """

    __slots__ = ("_fields", "_severity", "site")

    def __init__(self, site: LogSite) -> None:
        self.site = site
        self._severity = site.severity
        self._fields = site.fields

    @property
    def site_id(self) -> int:
        """The interned id, or 0 when the site table was full at registration."""
        return self.site.site_id

    def __bool__(self) -> bool:
        # The floor, not the publish threshold: a DEBUG site under a runtime
        # that buffers DEBUG *is* doing something, and a guard that said
        # otherwise would silently disable failure-triggered logging.
        return self._severity >= _RUNTIME.capture_level

    def __repr__(self) -> str:
        return f"<LogEvent {self.site.event_name!r} id={self.site.site_id}>"

    def __call__(self, *args: object) -> None:
        runtime = _RUNTIME
        severity = self._severity
        if severity < runtime.capture_level:
            return
        buffer = _SCRATCH.get(None)
        site_id = self.site.site_id
        # Below the publish threshold a record is buffered for promotion, or
        # dropped when there is no request to promote it. A buffered record is
        # not rate-limited: it costs an append and may never be published at
        # all, so suppressing it would save nothing and would thin out exactly
        # the history a failure is about to need.
        buffered = severity < runtime.level
        if buffered and buffer is None:
            return
        if not buffered and not runtime.limiter.allow(site_id, severity):
            return
        specs = self._fields
        flags = 0
        if len(args) != len(specs):
            runtime.counters.arity_mismatch += 1
        if not buffered and runtime.publish(
            self.site, 0 if buffer is None else buffer.request_id, severity, args
        ):
            # Packed straight into a ring cell in C. A buffered record cannot
            # take this path: it has to survive as an object until the request
            # decides whether to promote it.
            return
        packed: list[LogArg] = []
        for index, spec in enumerate(specs):
            if index >= len(args):
                packed.append(LogArg.none())
                continue
            arg, mismatched = pack_value(runtime.registry, args[index], spec)
            if mismatched:
                runtime.counters.type_mismatch += 1
            if arg.redacted:
                flags |= LOG_FLAG_REDACTED
            packed.append(arg)
        cell = LogCell(
            request_id=0 if buffer is None else buffer.request_id,
            site_id=site_id,
            severity=severity,
            args=tuple(packed),
            flags=flags,
            dropped_siblings=0 if buffered else runtime.limiter.take_dropped(site_id),
        )
        if buffered:
            if buffer is not None:  # guaranteed above; narrows for the checker
                buffer.add(cell)
            return
        runtime.emit(cell)


def event(
    event_name: str,
    template: str,
    *,
    level: Severity = INFO,
    fields: tuple[LogField, ...] = (),
) -> LogEvent:
    """Register a call site and return the callable that emits it.

    Call this at module scope. Registration validates the template against the
    declared fields and refuses a duplicate event name, so a malformed site
    fails at import rather than producing an unreadable record in production.

    Args:
        event_name: Stable identity for this class of event, carried to OTLP as
            `EventName`. Aggregating "how many of this event" is then a lookup,
            not message-text clustering.
        template: The message, with `{name}` placeholders matching `fields`.
            Rendered off the request path, never at the call site.
        level: The record's severity.
        fields: The declared arguments, in the order the call passes them.

    Raises:
        LogSiteError: If the template and fields disagree, a field type cannot
            be packed, more fields are declared than a record holds, or the
            event name is already registered.
    """
    site = _RUNTIME.registry.register(event_name, template, level, fields)
    return LogEvent(site)


def _emit_kwargs(severity: Severity, template: str, values: dict[str, object]) -> None:
    """The ergonomic tier: intern lazily, dispatch argument types at runtime."""
    runtime = _RUNTIME
    if severity < runtime.capture_level:
        return
    buffer = _SCRATCH.get(None)
    buffered = severity < runtime.level
    if buffered:
        if buffer is None:
            return
        held_buffer = buffer
    else:
        held_buffer = None
    specs = tuple(infer_field(name, value) for name, value in values.items())
    site = runtime.registry.intern_template(template, severity, specs)
    site_id = site.site_id
    if not buffered and not runtime.limiter.allow(site_id, severity):
        return
    if not buffered and runtime.publish(
        site,
        0 if buffer is None else buffer.request_id,
        severity,
        tuple(values.values()),
        specs=specs_for(site, specs),
    ):
        return
    flags = 0
    packed: list[LogArg] = []
    for spec, value in zip(specs, values.values(), strict=True):
        arg, mismatched = pack_value(runtime.registry, value, spec)
        if mismatched:
            runtime.counters.type_mismatch += 1
        if arg.redacted:
            flags |= LOG_FLAG_REDACTED
        packed.append(arg)
    cell = LogCell(
        request_id=0 if buffer is None else buffer.request_id,
        site_id=site_id,
        severity=severity,
        args=tuple(packed),
        flags=flags,
        dropped_siblings=0 if buffered else runtime.limiter.take_dropped(site_id),
    )
    if held_buffer is not None:
        held_buffer.add(cell)
        return
    runtime.emit(cell)


def _emit_prepared(
    severity: Severity,
    template: str,
    pairs: tuple[tuple[tuple[str, type, CaptureDisposition], object], ...],
) -> None:
    """Emit with explicit field specs, for callers that already have them."""
    runtime = _RUNTIME
    if severity < runtime.capture_level:
        return
    buffer = _SCRATCH.get(None)
    buffered = severity < runtime.level
    if buffered:
        if buffer is None:
            return
        held_buffer = buffer
    else:
        held_buffer = None
    specs = tuple(LogField(name, type_, disp) for (name, type_, disp), _v in pairs)
    site = runtime.registry.intern_template(template, severity, specs)
    site_id = site.site_id
    if not buffered and not runtime.limiter.allow(site_id, severity):
        return
    if not buffered and runtime.publish(
        site,
        0 if buffer is None else buffer.request_id,
        severity,
        tuple(value for _decl, value in pairs),
        specs=specs_for(site, specs),
    ):
        return
    flags = 0
    packed: list[LogArg] = []
    for spec, (_decl, value) in zip(specs, pairs, strict=True):
        arg, mismatched = pack_value(runtime.registry, value, spec)
        if mismatched:
            runtime.counters.type_mismatch += 1
        if arg.redacted:
            flags |= LOG_FLAG_REDACTED
        packed.append(arg)
    cell = LogCell(
        request_id=0 if buffer is None else buffer.request_id,
        site_id=site_id,
        severity=severity,
        args=tuple(packed),
        flags=flags,
        dropped_siblings=0 if buffered else runtime.limiter.take_dropped(site_id),
    )
    if held_buffer is not None:
        held_buffer.add(cell)
        return
    runtime.emit(cell)


def trace(template: str, **values: object) -> None:
    """Emit a TRACE record. See the module docstring for the cost of this tier."""
    _emit_kwargs(TRACE, template, values)


def debug(template: str, **values: object) -> None:
    """Emit a DEBUG record."""
    _emit_kwargs(DEBUG, template, values)


def info(template: str, **values: object) -> None:
    """Emit an INFO record."""
    _emit_kwargs(INFO, template, values)


def warn(template: str, **values: object) -> None:
    """Emit a WARN record."""
    _emit_kwargs(WARN, template, values)


def error(template: str, **values: object) -> None:
    """Emit an ERROR record."""
    _emit_kwargs(ERROR, template, values)


def fatal(template: str, **values: object) -> None:
    """Emit a FATAL record."""
    _emit_kwargs(FATAL, template, values)


class StdlibBridge(_stdlib.Handler):
    """A `logging.Handler` that forwards stdlib records into wreath's ring.

    This is the *compatible* path, not the fast one, and the distinction is
    worth stating plainly: by the time a handler is called, CPython has already
    built a `LogRecord`, walked the stack for the file and line, and formatted
    the message. None of the cost the registration tier avoids has been avoided
    here. Its value is that `asyncpg`, `httpx` and everything a user imports
    land in the same correlated stream instead of a second, disjoint one.

    The formatted message is an undeclared string from a foreign library, so by
    default it is fingerprinted like any other. Pass `raw_messages=True` to read
    it in cleartext, which is the right choice for most application logs and the
    wrong one for anything that might interpolate a credential.
    """

    def __init__(self, *, raw_messages: bool = True) -> None:
        super().__init__()
        self._raw = raw_messages

    def emit(self, record: _stdlib.LogRecord) -> None:
        severity = severity_from_stdlib(record.levelno)
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # A caller's own %-args are malformed. Their bug, not a reason to
            # lose the record: keep the template and say what happened.
            message = f"{record.msg!r} (unformattable args)"
        template = f"{record.name}: {{message}}"
        disposition = RAW if self._raw else HASHED
        _emit_prepared(
            severity,
            template,
            ((("message", str, disposition), message),),
        )


@contextmanager
def stdlib_bridge(
    logger: _stdlib.Logger | None = None, *, raw_messages: bool = True
) -> Iterator[list[LogCell]]:
    """Attach a `StdlibBridge` to `logger` (the root by default) for a block.

    Opt-in by design. Installing a handler on the root logger at startup fights
    `dictConfig`, surprises anyone with handlers of their own, and either
    double-emits or silently discards their configuration -- so wreath does not
    do it unasked. `wreath.doctor.check_logging_streams` notices when the
    consequence (two disjoint streams) is actually present and says so.

    Yields the records captured while attached, for tests and for a boot-time
    smoke check.
    """
    captured: list[LogCell] = []
    target = logger if logger is not None else _stdlib.getLogger()
    handler = StdlibBridge(raw_messages=raw_messages)
    previous_sink = _RUNTIME.sink

    def tee(cell: LogCell) -> None:
        captured.append(cell)
        if previous_sink is not None:
            previous_sink(cell)

    _RUNTIME.sink = tee
    _BRIDGED.add(target.name)
    target.addHandler(handler)
    try:
        yield captured
    finally:
        target.removeHandler(handler)
        _BRIDGED.discard(target.name)
        _RUNTIME.sink = previous_sink


def bridged_loggers() -> frozenset[str]:
    """Names of the stdlib loggers a bridge is currently attached to."""
    return frozenset(_BRIDGED)


def render(cell: LogCell) -> str:
    """The human-readable message for a record, rendered from its site."""
    return _render(_RUNTIME.registry, cell)


def attributes(cell: LogCell) -> dict[str, Any]:
    """The record's arguments as named values, for structured output."""
    return _attributes(_RUNTIME.registry, cell)


def off_loop_counts() -> dict[str, int]:
    """What the off-loop slow path has carried, and what it refused.

    `staged` is records emitted from a thread that may not write to the ring --
    a `wreath.jobs` worker, a thread-pool task -- and `dropped` is those the
    bounded stage could not hold, which is `LossReason.LOG_OFF_LOOP`. A `staged`
    that keeps climbing is not an error; it is instrumentation telling you where
    your logging happens, and each of those records arrives one drain interval
    late and flagged `off-loop`.
    """
    stage = _RUNTIME.off_loop
    if stage is None:
        return {"staged": 0, "dropped": 0, "held": 0}
    return {"staged": stage.staged, "dropped": stage.dropped, "held": len(stage)}


def site_overflow_count() -> int:
    """Call sites refused because the bounded table was full."""
    return _RUNTIME.registry.overflow


def type_mismatch_count() -> int:
    """Arguments that failed their declared type and packed as `none`."""
    return _RUNTIME.counters.type_mismatch


def arity_mismatch_count() -> int:
    """Calls that passed more or fewer arguments than the site declares."""
    return _RUNTIME.counters.arity_mismatch
