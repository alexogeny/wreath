"""The native reactor — an asyncio-compatible event loop with a fused fast path.

This is the loop behind Wreath's experimental **metal** tier (`--loop metal`):
alongside `pure` (Python) and `native` (C on the stock asyncio loop), metal
runs the native server on this reactor — an `asyncio`-compatible loop that
inline-drives non-suspending request coroutines and, when `timers="wheel"`,
backs every `call_later` deadline with the native hashed timing wheel from
`wreath._native._reactor` instead of asyncio's timer heap ("bare metal").

The loop stays a strict `asyncio` loop, so `await` of anything works. The
wheel path trades sub-millisecond timer precision for O(1) deadline churn, which
is the right trade for keep-alive/request timeouts; it is opt-in and off in the
default loop so general asyncio-timer semantics are preserved. The full story and
the measurement behind the wheel choice are in
docs/explorations/the-timer-that-wouldnt-settle.md.


Stage 0 lands the loop core and the headline novel technique: **inline-drive**.
A request coroutine that completes without ever suspending is driven straight to
completion at `create_task` time — no `Task` scheduled onto the ready queue,
no `call_soon` round trip, no done-callback hop. Only a coroutine that
actually suspends is promoted to the normal (scheduled) driver.

This is the async analogue of a leaf-call optimization: neither stock asyncio
nor uvloop does it, because you cannot hand an already-stepped coroutine to
`asyncio.Task` (it would `send(None)` twice). We sidestep that by driving the
coroutine ourselves from the first step, using the C task-state helpers with an
explicit leave/enter around the inline step so it composes even when a running
task creates another task.

The loop is a `SelectorEventLoop` subclass so that every third-party `await`
(sleep, gather, locks, executors, DNS, TLS, transports) works unchanged from day
one; the C poller, timing wheel, and protocol→coroutine fusion replace internals
in later stages behind the same observable contract (see tests/reactor/).
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import gc
import selectors
import socket
import ssl
import sys
from asyncio import events as _events
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # `serve()` imports wreath.server lazily; borrow only the protocol-name
    # literal so its signature names the values ServerConfig actually accepts.
    from .server import HttpProtocolName

    # The C task-state helpers, restated with the shape they actually accept.
    # typeshed types them for `asyncio.Task`, but `WreathTask` below is a
    # duck-typed task: it subclasses `Future` on purpose, because `Task.__init__`
    # schedules the first step and skipping that scheduling is the entire point
    # of the class. The registry only needs the future/task protocol, which it
    # satisfies -- so widen the parameter rather than claim a Task it is not.
    def _register_task(task: asyncio.Future[Any]) -> None: ...
    def _enter_task(loop: asyncio.AbstractEventLoop, task: asyncio.Future[Any]) -> None: ...
    def _leave_task(loop: asyncio.AbstractEventLoop, task: asyncio.Future[Any]) -> None: ...
else:
    from asyncio.tasks import (  # C-accelerated task-state helpers
        _enter_task,
        _leave_task,
        _register_task,
    )

#: The timing-wheel/poller extension, or None without `wreath[linux]`. Resolved
#: through the `_native` loader so it is Any-typed like `_core`; a direct import
#: of the compiled submodule is invisible to static analysis.
from ._native import _reactor as _wheel_ext

__all__ = [
    "new_event_loop",
    "available_backends",
    "metal_tls_client_context",
    "metal_tls_context",
    "serve",
    "EventLoop",
    "WreathTask",
]


class _MetalTLSContext(ssl.SSLContext):
    """An `ssl.SSLContext` that also carries a native one.

    It has to be a real `SSLContext` because `loop.create_server` type-checks
    the `ssl=` argument and refuses anything else, and it has to carry the
    native handle because there is no supported way to borrow an `SSL_CTX *`
    out of a Python context. Loading the same material into both halves means
    any path that does *not* recognise this subclass still terminates TLS
    correctly, just through asyncio.
    """

    __slots__ = ("metal",)

    #: The native `_reactor.TLSContext`. Declared as well as slotted because a
    #: slot is a descriptor rather than an annotation, and the type checker
    #: reads the latter.
    metal: Any


def metal_tls_client_context(
    *,
    cafile: str | None = None,
    capath: str | None = None,
    verify: bool = True,
    alpn: tuple[str, ...] = ("http/1.1",),
) -> ssl.SSLContext:
    """An outbound TLS context whose crypto runs in C, for `metal_event_loop()`.

    The other half of `metal_tls_context`. Pass it as `ssl=` to
    `create_connection` with a `server_hostname`; on the metal loop the
    connection keeps the native transport and runs `SSL_connect` in C.

    **Verification is on by default and the default is the point.** A TLS client
    that skips the trust check is faster than one that does not and looks
    identical until it matters, so `verify=False` has to be typed out. Both the
    chain and the host name are checked inside OpenSSL, because setting SNI
    without setting the name to check against is the classic half-done client:
    it reaches the right virtual host and then accepts a certificate for any
    name at all.

    Args:
        cafile: PEM bundle of trusted roots. Defaults to the system store.
        capath: Directory of hashed trusted roots.
        verify: Check the peer's chain and host name.
        alpn: Protocols to offer, in preference order.

    Returns:
        A context usable anywhere an `ssl.SSLContext` is.
    """
    if _wheel_ext is None or not hasattr(_wheel_ext, "TLSClientContext"):
        raise RuntimeError(
            "native TLS needs 'wreath[linux]'; the metal tier is Linux-only"
        )
    context = _MetalTLSContext(ssl.PROTOCOL_TLS_CLIENT)
    if verify:
        if cafile is not None or capath is not None:
            context.load_verify_locations(cafile=cafile, capath=capath)
        else:
            context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(list(alpn))
    context.metal = _wheel_ext.TLSClientContext(
        cafile=cafile, capath=capath, verify=verify, alpn=list(alpn))
    return context


def metal_tls_context(
    *,
    certfile: str,
    keyfile: str,
    password: str | None = None,
    alpn: tuple[str, ...] = ("http/1.1",),
) -> ssl.SSLContext:
    """A server TLS context whose crypto runs in C, for `metal_event_loop()`.

    Pass it as `ssl=` to `create_server` (or as `wreath.server`'s `ssl`). On the
    metal loop the listener keeps the native transport and terminates TLS in
    C; on any other loop it behaves as an ordinary `ssl.SSLContext`, because it
    is one.

    **Why this is not `ssl.SSLContext` alone.** `EventLoop._start_serving` takes
    the native path only when it can build an `SSL_CTX` itself, and that needs
    the certificate and key *paths* -- a built `SSLContext` will not give up its
    private key, by design. `TLSConfig` already carries the paths for the same
    reason on the HTTP/3 side.

    Measured on one machine, one physical core, handshakes amortised: the
    asyncio fallback served 21,300 req/s against nginx's 47,400, because a TLS
    connection left the metal tier entirely -- asyncio's accept loop, asyncio's
    transport, and `asyncio.sslproto.SSLProtocol` running Python per read and
    per write.

    Args:
        certfile: PEM certificate chain, leaf first.
        keyfile: PEM private key for `certfile`.
        password: Passphrase for an encrypted key.
        alpn: Protocols to advertise, in server preference order.

    Returns:
        A context usable anywhere an `ssl.SSLContext` is.

    Raises:
        OSError: The certificate or key could not be read or do not match.
            Raised here, while the proxy is being configured, rather than on the
            first handshake -- a listener that binds and then fails every
            connection is the failure this tree refuses everywhere else.
    """
    if _wheel_ext is None or not hasattr(_wheel_ext, "TLSContext"):
        raise RuntimeError(
            "native TLS needs 'wreath[linux]'; the metal tier is Linux-only"
        )
    context = _MetalTLSContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile, password)
    context.set_alpn_protocols(list(alpn))
    context.metal = _wheel_ext.TLSContext(
        certfile=str(certfile), keyfile=str(keyfile),
        password=password, alpn=list(alpn),
    )
    return context


#: Accept-flag bits that ride in a socket's `type` on Linux but never appear in
#: what `getsockopt(SO_TYPE)` reports. Read through `getattr` because they are
#: platform-conditional in the `socket` module and metal's listener spec must
#: not depend on their presence.
_SOCK_NONBLOCK = getattr(socket, "SOCK_NONBLOCK", 0)
_SOCK_CLOEXEC = getattr(socket, "SOCK_CLOEXEC", 0)

#: Defaults for ``gc_mode="idle"``. `threshold` is CPython's gen-0 allocation
#: trigger (stock is 2000); `idle`/`full_idle` are arrival-gap seconds -- the
#: poller's own estimate of how long it waits between completions -- above which
#: a wait is idle enough to spend on a young or a full collection.
_GC_THRESHOLD = 20_000
_GC_IDLE_SECONDS = 250e-6
_GC_FULL_IDLE_SECONDS = 20e-3
#: Floor between two loop-driven collections. One request wakes the loop several
#: times, so without it a lightly loaded server re-collects a young generation it
#: just emptied, several times per request.
_GC_MIN_INTERVAL_SECONDS = 10e-3


class _LoopCollector:
    """Moves cycle collection out of request batches and into the loop's idle gaps.

    Three separable pieces. They are separable on purpose: each can be measured
    on its own, and only the first of them removes work rather than moving it.

    **Freeze.** Everything alive when the server finishes starting -- imported
    modules, the route table, the ORM registry, the app's own state -- is
    long-lived by construction. ``gc.freeze()`` moves it into the permanent
    generation, which no collection ever traverses again. This is the piece that
    makes collections *cheaper* rather than *rarer*.

    **Threshold.** CPython triggers on an allocation counter, so a
    request-serving loop collects every few hundred requests -- roughly 1% of
    them, which is exactly where a p99 lives. Raising the trigger does not
    reduce total collector time: the cost of a young collection scales with the
    young generation it scans, so rarer collections are proportionally bigger.
    It moves that time off p99 and onto p999, which is the whole point when the
    goal is consistency. It is raised modestly rather than removed because it
    remains the backstop for a loop that never goes idle.

    **Idle collection.** The poller calls the collector when its arrival
    estimator says the loop is about to wait (see ``rp_collect_idle``). Under
    sustained saturation that never fires, by design -- there is no idle gap to
    spend -- and the raised threshold carries it.

    Two consequences worth stating rather than discovering. ``gc.freeze()``
    makes any reference cycle created during startup permanently
    unreachable-but-uncollected; that is the intended trade, and ``release()``
    undoes it. And ``gc.unfreeze()`` is process-global, so ``release()`` calls it
    only when this collector is the one that froze.
    """

    __slots__ = ("_poller", "_previous_threshold", "_frozen", "_threshold",
                 "_idle_seconds", "_full_idle_seconds", "_freeze_enabled",
                 "_min_interval_seconds")

    def __init__(self, *, threshold: int = _GC_THRESHOLD,
                 idle_seconds: float = _GC_IDLE_SECONDS,
                 full_idle_seconds: float = _GC_FULL_IDLE_SECONDS,
                 min_interval_seconds: float = _GC_MIN_INTERVAL_SECONDS,
                 freeze_enabled: bool = True) -> None:
        if threshold < 1:
            raise ValueError("gc threshold must be positive")
        if not idle_seconds > 0.0 or full_idle_seconds < idle_seconds:
            raise ValueError(
                "idle_seconds must be positive and no greater than "
                "full_idle_seconds")
        if min_interval_seconds < 0.0:
            raise ValueError("min_interval_seconds must not be negative")
        self._threshold = threshold
        self._idle_seconds = idle_seconds
        self._full_idle_seconds = full_idle_seconds
        self._min_interval_seconds = min_interval_seconds
        self._freeze_enabled = freeze_enabled
        self._poller: Any = None
        self._previous_threshold: tuple[int, ...] | None = None
        self._frozen = 0

    def attach(self, poller: Any) -> None:
        """Raise the automatic trigger and hand the poller the collector."""
        if self._previous_threshold is None:
            self._previous_threshold = gc.get_threshold()
            gc.set_threshold(self._threshold, *self._previous_threshold[1:])
        self._poller = poller
        poller._set_gc_collector(
            gc.collect, self._idle_seconds, self._full_idle_seconds,
            self._min_interval_seconds)

    def freeze(self) -> int:
        """Collect once, then move everything still alive out of the collector's reach.

        Collecting first matters: freezing garbage would keep it for the life of
        the process. Returns the number of objects now in the permanent
        generation, or 0 when freezing is ablated off.
        """
        if not self._freeze_enabled:
            return 0
        gc.collect()
        gc.freeze()
        self._frozen = gc.get_freeze_count()
        return self._frozen

    def release(self) -> None:
        """Give the heap back to CPython: restore the trigger and unfreeze."""
        if self._poller is not None:
            # A poller closed before its loop has already dropped the collector;
            # clearing it twice is harmless and keeps teardown order free.
            self._poller._set_gc_collector(None, 1.0, 1.0, 0.0)
            self._poller = None
        if self._previous_threshold is not None:
            gc.set_threshold(*self._previous_threshold)
            self._previous_threshold = None
        if self._frozen:
            gc.unfreeze()
            self._frozen = 0

    def stats(self) -> dict[str, int]:
        poller = self._poller
        return {
            "threshold": gc.get_threshold()[0],
            "frozen_objects": self._frozen,
            "idle_young_collections": getattr(
                poller, "gc_young_collections", 0),
            "idle_full_collections": getattr(poller, "gc_full_collections", 0),
            "idle_collect_nanoseconds": getattr(
                poller, "gc_collect_nanoseconds", 0),
        }


def _stats_template() -> dict[str, int]:
    return {
        "inline_completions": 0,
        "fused_resumes": 0,
        "call_soon_scheduled": 0,
        "coro_steps": 0,
        "poll_calls": 0,
        "timers_fired": 0,
        "tasks_promoted": 0,
    }


class WreathTask(asyncio.Future):
    """A Task that drives its coroutine itself, inlining non-suspending work.

    Behaves like `asyncio.Task` for every scheduled (suspending) path; the only
    departure is that the *first* step runs synchronously inside `create_task`
    when the loop is already running, so a coroutine that finishes without
    awaiting anything never touches the ready queue.
    """

    _log_destroy_pending = True

    def __init__(self, coro, *, loop, name=None, context=None):
        super().__init__(loop=loop)
        if not asyncio.iscoroutine(coro):
            raise TypeError(f"a coroutine was required, got {coro!r}")
        self._coro = coro
        self._context = context if context is not None else contextvars.copy_context()
        self._fut_waiter: asyncio.Future | None = None
        self._must_cancel = False
        self._cancel_message = None
        self._num_cancels_requested = 0
        self._name = str(name) if name is not None else f"WreathTask-{id(self):x}"
        _register_task(self)
        # Inline-drive only while the loop is actually running (so get_running_loop
        # and current-task bookkeeping are valid). run_until_complete's own top
        # coroutine is created before the loop runs and takes the scheduled path.
        if _events._get_running_loop() is loop:
            self._drive_first_step_inline()
        else:
            loop.call_soon(self.__step, context=self._context)
            loop._reactor_stats["tasks_promoted"] += 1

    # -- introspection parity with asyncio.Task -----------------------------
    def get_coro(self):
        return self._coro

    def get_name(self):
        return self._name

    def set_name(self, value):
        self._name = str(value)

    def get_context(self):
        return self._context

    def __repr__(self):
        return f"<WreathTask name={self._name!r} state={self._state}>"

    # -- cancellation -------------------------------------------------------
    def cancel(self, msg=None):
        self._num_cancels_requested += 1
        if self.done():
            return False
        if self._fut_waiter is not None:
            if self._fut_waiter.cancel(msg=msg):
                return True
        self._must_cancel = True
        self._cancel_message = msg
        return True

    def cancelling(self):
        return self._num_cancels_requested

    def uncancel(self):
        if self._num_cancels_requested > 0:
            self._num_cancels_requested -= 1
        return self._num_cancels_requested

    # -- the driver ---------------------------------------------------------
    def _drive_first_step_inline(self):
        loop = self._loop
        outer = asyncio.current_task(loop)
        # Leave the outer task so the C task-state machine sees no nesting, then
        # run our step in our own context, then restore the outer task.
        if outer is not None:
            _leave_task(loop, outer)
        try:
            self._context.run(self.__step)
        finally:
            if outer is not None:
                _enter_task(loop, outer)
        # `Future._loop` is declared `AbstractEventLoop`; this task is only ever
        # built by `EventLoop.create_task`, so the stats dict is always there.
        # Redeclaring `_loop` as `EventLoop` would fix these three reads and cost
        # five more on `loop.call_soon` below, which the per-instance shadowing in
        # `EventLoop.__init__` turns into a method/attribute union.
        if self.done():
            loop._reactor_stats["inline_completions"] += 1  # ty: ignore[unresolved-attribute]
        else:
            loop._reactor_stats["tasks_promoted"] += 1  # ty: ignore[unresolved-attribute]

    def __step(self, exc=None):
        loop = self._loop
        coro = self._coro
        self._fut_waiter = None
        _enter_task(loop, self)
        loop._reactor_stats["coro_steps"] += 1  # ty: ignore[unresolved-attribute]
        try:
            if exc is None:
                result = coro.send(None)
            else:
                result = coro.throw(exc)
        except StopIteration as si:
            if self._must_cancel:
                self._must_cancel = False
                asyncio.Future.cancel(self, msg=self._cancel_message)
            else:
                asyncio.Future.set_result(self, si.value)
        except asyncio.CancelledError:
            asyncio.Future.cancel(self, msg=self._cancel_message)
        except (KeyboardInterrupt, SystemExit) as e:
            asyncio.Future.set_exception(self, e)
            raise
        except BaseException as e:  # noqa: BLE001 -- stored on the future, not lost
            # `Task.__step`, reimplemented. The exception is *recorded* on the
            # future for whoever awaits this task -- it is not discarded, and the
            # three cases that need different handling are caught above:
            # `CancelledError` cancels the future, `KeyboardInterrupt` and
            # `SystemExit` are stored *and* re-raised so they still reach the
            # loop. Narrowing this to `Exception` would drop any other
            # `BaseException` on the floor instead of delivering it to the
            # awaiter. This mirrors CPython's own tasks.py; keep it that way.
            asyncio.Future.set_exception(self, e)
        else:
            blocking = getattr(result, "_asyncio_future_blocking", None)
            if blocking is not None:
                if result._loop is not loop:
                    exc2 = RuntimeError(
                        f"Task {self!r} got Future {result!r} attached to a different loop"
                    )
                    loop.call_soon(self.__step, exc2, context=self._context)
                elif blocking:
                    if result is self:
                        exc2 = RuntimeError(f"Task cannot await on itself: {self!r}")
                        loop.call_soon(self.__step, exc2, context=self._context)
                    else:
                        result._asyncio_future_blocking = False
                        result.add_done_callback(self.__wakeup)
                        self._fut_waiter = result
                        if self._must_cancel:
                            if self._fut_waiter.cancel(msg=self._cancel_message):
                                self._must_cancel = False
                else:
                    exc2 = RuntimeError(
                        f"yield was used instead of yield from in task {self!r} with {result!r}"
                    )
                    loop.call_soon(self.__step, exc2, context=self._context)
            elif result is None:
                loop.call_soon(self.__step, context=self._context)
            else:
                exc2 = RuntimeError(f"Task got bad yield: {result!r}")
                loop.call_soon(self.__step, exc2, context=self._context)
        finally:
            _leave_task(loop, self)

    def __wakeup(self, future):
        try:
            future.result()
        except BaseException as exc:  # noqa: BLE001 -- forwarded into __step
            # Also from CPython's tasks.py. The exception is handed straight to
            # `__step`, which decides what it means for the task -- this frame
            # only chooses which of the two `__step` calls to make. Nothing is
            # absorbed, and `CancelledError` in particular must arrive here.
            self.__step(exc)
        else:
            self.__step()


if TYPE_CHECKING:

    class _LoopBase(asyncio.SelectorEventLoop):
        """CPython's private ``BaseEventLoop``/``BaseSelectorEventLoop`` surface.

        typeshed declares no private members, but this loop deliberately
        overrides and calls several of them -- that is what "replace the loop
        internals behind the same observable contract" means here. Stating them
        once, in a base that exists only for type checking, keeps
        ``super()._run_once()`` and friends honest instead of waiving the same
        fact at eight separate call sites. At runtime the base is exactly
        ``asyncio.SelectorEventLoop``: no extra class, no MRO change, no cost.

        These signatures track CPython's; if a release changes one, the mismatch
        is supposed to show up here rather than at each use.
        """

        _selector: selectors.BaseSelector
        _ssock: socket.socket | None
        _write_to_self: Callable[[], None]
        # `create_task` consults the factory itself and checks the closed flag
        # itself, because it does not go through `BaseEventLoop.create_task`.
        _task_factory: Callable[..., Any] | None

        def _check_closed(self) -> None: ...
        def _read_from_self(self) -> None: ...
        def _run_once(self) -> None: ...
        def _process_events(self, event_list: Any) -> None: ...
        def _start_serving(
            self,
            protocol_factory: Any,
            sock: socket.socket,
            sslcontext: Any = None,
            server: Any = None,
            backlog: int = 100,
            ssl_handshake_timeout: float | None = None,
            ssl_shutdown_timeout: float | None = None,
        ) -> None: ...
        def _stop_serving(self, sock: socket.socket) -> None: ...
        def _make_socket_transport(
            self,
            sock: socket.socket,
            protocol: Any,
            waiter: Any = None,
            *,
            extra: Any = None,
            server: Any = None,
        ) -> asyncio.Transport: ...
        # The trailing keywords are deliberately `**kwargs` rather than named:
        # CPython has changed them across releases (`call_connection_made` is
        # absent in 3.14, the two timeouts default to constants), and naming one
        # that a release does not accept fails invisibly -- the accept path
        # swallows the TypeError and drops the connection.
        def _make_ssl_transport(
            self,
            rawsock: socket.socket,
            protocol: Any,
            sslcontext: Any,
            waiter: Any = None,
            *,
            server_side: bool = False,
            server_hostname: str | None = None,
            extra: Any = None,
            server: Any = None,
            **kwargs: Any,
        ) -> asyncio.Transport: ...

    class _CPythonTask(asyncio.Task[Any]):
        """CPython's `Task`, with the one private member `create_task` reads.

        Same bargain as `_LoopBase` above: typeshed declares no private
        members, and `_source_traceback` is one this module uses on purpose.
        It is present on every Task -- `None` unless the loop is in debug mode
        -- because `Future` carries it as a class attribute. At runtime this
        name *is* `asyncio.Task`.
        """

        _source_traceback: list[Any] | None

else:
    _LoopBase = asyncio.SelectorEventLoop
    _CPythonTask = asyncio.Task


class EventLoop(_LoopBase):
    """SelectorEventLoop augmented with the reactor's fast path and telemetry.

    `timers="wheel"` routes every `call_later`/`call_at` deadline through
    the native hashed timing wheel instead of asyncio's heap. A single bridge
    tick (one heap timer, re-armed only while the wheel is non-empty) drives
    expiry, so no `_run_once` reimplementation is needed. Timer precision is
    the wheel resolution (1 ms) -- ample for server deadlines, which is why it is
    opt-in rather than the default.
    """

    def __init__(self, selector=None, *, backend: str = "epoll",
                 timers: str = "heap", tasks: str = "inline", stats: bool = True,
                 native_transport: bool = False, native_loop: bool = False,
                 direct_task_steps: bool = True, worker_id: int = 0,
                 reuse_port: bool = False, adaptive_polling: bool = True,
                 diagnostics: bool = True, wheel_slots: int = 4096,
                 wheel_resolution: float = 0.001, gc_mode: str = "stock",
                 gc_freeze: bool = True):
        super().__init__(selector)
        # Everything `close()` touches, before anything below can raise.
        # `super().__init__` has already made this object collectable, so a
        # rejected argument leaves a half-built loop for `BaseEventLoop.__del__`
        # to close -- and a `close()` that raises AttributeError there turns a
        # clear ValueError into an unraisable one from a destructor.
        self._poller = None
        self._collector: _LoopCollector | None = None
        self._backend = backend
        self._tasks = tasks
        self._stats_on = stats
        self._native_transport = native_transport and _wheel_ext is not None
        self._native_loop = native_loop and _wheel_ext is not None
        self._direct_task_steps = direct_task_steps
        if worker_id < 0:
            raise ValueError("worker_id must be non-negative")
        self._worker_id = worker_id
        self._adaptive_polling = adaptive_polling
        self._diagnostics = diagnostics
        self._wreath_reuse_port = reuse_port
        self._reactor_stats = _stats_template()
        # Native objects, so Any like `_core` everywhere else in the package;
        # None until timers="wheel" builds them. The wheel-only methods below are
        # reached solely from the wheel branch, so they never see the None.
        self._wheel: Any = None
        self._wheel_schedule = None  # cached bound schedule_call: no per-timer attr lookup
        self._wheel_res = wheel_resolution
        self._wheel_tick_handle = None
        if gc_mode not in ("stock", "idle"):
            raise ValueError("gc_mode must be 'stock' or 'idle'")
        if gc_mode == "idle" and not self._native_loop:
            # The idle gap the policy spends is a native-run-loop concept: only
            # the C poller knows when it is about to wait, and only it holds the
            # arrival estimate that distinguishes "idle" from "saturated". Fail
            # here rather than install a policy with no hook to run in.
            raise ValueError(
                "gc_mode='idle' requires native_loop=True: the idle gap it "
                "collects in belongs to the native run loop")
        # `gc_freeze` is separable from `gc_mode` because the two are separately
        # measurable: freezing makes collections cheaper, the idle policy makes
        # them land somewhere harmless, and which one earned a result is a
        # question an ablation has to be able to ask.
        self._collector = (
            _LoopCollector(freeze_enabled=gc_freeze) if gc_mode == "idle"
            else None
        )
        if self._native_loop and timers != "wheel":
            # The C _run_once pops loop._scheduled by expiry only; asyncio's
            # cancelled-TimerHandle compaction lives in the Python _run_once it
            # replaces, so heap timers under the native loop leak every
            # schedule-then-cancel (wait_for churn) until its deadline lapses.
            raise ValueError(
                "native_loop=True requires timers='wheel': the native run "
                "loop does not compact cancelled heap timers")
        if timers == "wheel":
            if _wheel_ext is None:
                raise RuntimeError("timers='wheel' needs 'wreath[linux]'")
            self._wheel = _wheel_ext.TimingWheel(
                resolution=wheel_resolution, slots=wheel_slots, base=self.time())
            self._wheel_schedule = self._wheel.schedule_call
        if not stats:
            # No introspection: shadow the counting call_soon/_run_once with the
            # base C implementations so the hot path pays no Python override
            # frame. Rebinding a method as a per-instance attribute is the point
            # -- and is exactly what a type checker cannot model, since the name
            # is a method on the class and a bound method on the instance. Both
            # assignments are waived rather than the technique disguised.
            self.call_soon = super().call_soon  # ty: ignore[invalid-assignment]
            self._run_once = super()._run_once  # ty: ignore[invalid-assignment]
        if self._native_loop:
            self._install_native_loop()
        if self._collector is not None:
            self._collector.attach(self._poller)

    def _install_native_loop(self) -> None:
        """Replace the selector I/O core with the C ReactorPoller.

        The poller owns a tagged io_uring completion domain and fd-to-callback
        registry. It dispatches readiness CQEs directly -- no selector.select()
        wrapper, no _process_events, no
        per-event Handle. We rebind the loop's own low-level I/O methods to the
        poller's C methods (so transports and sock_* reach it), then move the
        self-pipe -- registered on the base selector by super().__init__() -- onto
        the poller, or call_soon_threadsafe and signals could never wake us.
        """
        inherited_selector = self._selector
        poller = _wheel_ext.ReactorPoller(
            self,
            self._wheel if self._wheel is not None else None,
            self._direct_task_steps,
            self._worker_id,
            1,
            1 if self._adaptive_polling else 0,
            1 if self._diagnostics else 0,
        )
        self._poller = poller
        self._add_reader = poller._add_reader
        self._add_writer = poller._add_writer
        self._remove_reader = poller._remove_reader
        self._remove_writer = poller._remove_writer
        self._run_once = poller._run_once
        # Handle-free call_soon: every Future callback and Task wakeup skips
        # asyncio's two Python frames + Handle construction in favour of a
        # freelisted C handle the native run loop executes directly.
        # call_soon_threadsafe keeps the base implementation (thread safety);
        # its Handles share the same FIFO deque, so ordering is preserved.
        # Debug-mode thread/coroutine checks do not apply on this fast path.
        self.call_soon = poller._call_soon
        ssock = getattr(self, "_ssock", None)
        if ssock is not None:
            # CPython writes signal wake bytes to this socket; the unified ring
            # invokes the drain callback from its control CQ path.
            poller._set_signal_reader(ssock.fileno(), self._read_from_self)
        self._write_to_self = poller._wake
        # SelectorEventLoop construction creates an epoll fd before the native
        # poller can be installed. Metal never dispatches through it: close that
        # inherited kernel object now instead of retaining duplicate ownership
        # and RSS until loop teardown. Base close is idempotent.
        inherited_selector.close()

    def close(self) -> None:
        if self._collector is not None:
            # Before the poller goes: the collector detaches through it, and a
            # process that outlives this loop must get its heap policy back.
            self._collector.release()
        if self._poller is not None:
            # Break the loop<->poller cycle and free the ring/registry promptly.
            self._poller.close()
        super().close()

    def freeze_heap(self) -> int:
        """Move everything currently alive beyond the collector's reach.

        Called once by the server when startup is complete, which is the point
        at which "everything reachable" and "everything long-lived" coincide.
        Returns the number of objects in the permanent generation, or 0 when
        this loop does not own its collector (`gc_mode="stock"`).
        """
        if self._collector is None:
            return 0
        return self._collector.freeze()

    def gc_stats(self) -> dict[str, int]:
        """Collector counters: what the policy did, and what it cost."""
        if self._collector is None:
            return {}
        return self._collector.stats()

    # Returns a duck-typed task, not an `asyncio.Task`, whenever the inline tier
    # is on -- which is the reason this module exists (see `WreathTask`). That is
    # a real Liskov departure and is waived here rather than hidden by widening
    # the base class's return type for every caller.
    def create_task(  # ty: ignore[invalid-method-override]
        self,
        coro: Any,
        *,
        name: str | None = None,
        context: contextvars.Context | None = None,
    ) -> Any:
        # The factory first, and in *every* mode. It used to be reachable only
        # on the `auto` path, because `inline` returned a `WreathTask` before
        # `BaseEventLoop.create_task` could consult it -- so `set_task_factory`
        # on an inline loop installed a hook that was never called, which reads
        # as the factory being wrong rather than skipped. Keywords are forwarded
        # only when given, matching what the base class would have passed on.
        if self._task_factory is not None:
            keywords: dict[str, Any] = {}
            if name is not None:
                keywords["name"] = name
            if context is not None:
                keywords["context"] = context
            return self._task_factory(self, coro, **keywords)
        if self._tasks == "inline":
            return WreathTask(coro, loop=self, name=name, context=context)
        # `auto` is CPython's C Task, constructed here rather than through
        # `BaseEventLoop.create_task`. That wrapper is two Python frames, a
        # `**kwargs` dict built and unpacked, and a debug-only traceback trim,
        # and it measured 5,005 instructions a task on top of the constructor's
        # own 5,970 -- 2.8% of a Fortunes request, which takes one task every
        # time it waits on the database. Everything the wrapper does that is
        # observable is done here: the factory above, the closed check, and the
        # traceback trim below.
        # Before the Task exists, not from inside it. The constructor's own
        # `call_soon` raises the same `RuntimeError` on a closed loop, but only
        # after building a Task that then reports "Task was destroyed but it is
        # pending!" when it is collected -- a second, confusing report of one
        # mistake.
        self._check_closed()
        task = _CPythonTask(coro, loop=self, name=name, context=context)
        if task._source_traceback:  # debug mode only; drop this frame from it
            del task._source_traceback[-1]
        return task

    # BaseEventLoop.call_soon binds its argument types with a TypeVarTuple
    # (`callback: Callable[[*Ts], object], *args: *Ts`). Reproducing that shape
    # here does not currently type-check either -- ty renders the starred
    # parameter as unresolved -- so this takes the plain form and waives the
    # resulting Liskov complaint. The forwarding below is unchanged.
    def call_soon(  # ty: ignore[invalid-method-override]
        self,
        callback: Callable[..., object],
        *args: Any,
        context: contextvars.Context | None = None,
    ) -> asyncio.Handle:
        if self._stats_on:
            self._reactor_stats["call_soon_scheduled"] += 1
        return super().call_soon(callback, *args, context=context)

    def call_later(self, delay, callback, *args, context=None):
        # Straight to the C wheel via a cached bound method: no call_at hop, no
        # per-timer TimerHandle, no attribute lookup, and context=None dispatches
        # the callback directly (no copy_context / context.run) -- server deadline
        # callbacks do not read contextvars.
        schedule = self._wheel_schedule
        if schedule is None:
            return super().call_later(delay, callback, *args, context=context)
        handle = schedule(delay if delay > 0.0 else 0.0, callback, args, context)
        if self._poller is None and self._wheel_tick_handle is None:
            self._ensure_wheel_tick()
        return handle

    def call_at(self, when, callback, *args, context=None):
        schedule = self._wheel_schedule
        if schedule is None:
            return super().call_at(when, callback, *args, context=context)
        delay = when - self.time()
        handle = schedule(delay if delay > 0.0 else 0.0, callback, args, context)
        if self._poller is None and self._wheel_tick_handle is None:
            self._ensure_wheel_tick()
        return handle

    def _ensure_wheel_tick(self):
        if self._wheel_tick_handle is None and self._wheel.count > 0:
            # Bypass our call_at override: the tick itself must ride the heap.
            self._wheel_tick_handle = super().call_at(
                self.time() + self._wheel_res, self._wheel_tick)

    def _wheel_tick(self):
        self._wheel_tick_handle = None
        # advance_run expires and dispatches due timers entirely in C.
        self._wheel.advance_run(self.time())
        self._ensure_wheel_tick()

    # Waived for the same reason as the assignment in `__init__`: shadowing this
    # per instance makes the name a method-or-bound-method union, which no
    # class-level definition can then be substitutable for.
    def _run_once(self) -> None:  # ty: ignore[invalid-method-override]
        if self._stats_on:
            self._reactor_stats["poll_calls"] += 1
        return super()._run_once()

    def _start_serving(
        self, protocol_factory, sock, sslcontext=None, server=None, backlog=100,
        ssl_handshake_timeout=None, ssl_shutdown_timeout=None,
    ):
        native_tls = getattr(sslcontext, "metal", None)
        if self._poller is not None and (sslcontext is None or native_tls is not None):
            # The listener's family/type/proto ride along because every
            # connection accepted from it has the same three, and a
            # `socket(fileno=...)` that is not told them asks the kernel --
            # two getsockopt calls per accepted connection for a fact this
            # socket has known since it was bound.
            #
            # SOCK_NONBLOCK/SOCK_CLOEXEC are masked out of the type because a
            # listener handed in as `sock=` may carry them and the kernel's
            # SO_TYPE never reports them. Masking keeps the accepted socket's
            # `.type` byte-identical to what the getsockopt path produced,
            # rather than a value only this path can return.
            sock_type = int(sock.type) & ~(_SOCK_NONBLOCK | _SOCK_CLOEXEC)
            # The seventh element is the native TLS context, or None. A
            # listener carrying one hands every accepted connection an `SSL`
            # and defers `connection_made` until the handshake completes; the
            # transport drives that itself, in C.
            native_server = (
                protocol_factory, server, socket.socket,
                int(sock.family), sock_type, int(sock.proto), native_tls,
            )
            self._poller._add_uring_listener(sock.fileno(), native_server)
            return
        return super()._start_serving(
            protocol_factory,
            sock,
            sslcontext,
            server,
            backlog,
            ssl_handshake_timeout,
            ssl_shutdown_timeout,
        )

    def _make_ssl_transport(
        self, rawsock, protocol, sslcontext, waiter=None, *,
        server_side=False, server_hostname=None, extra=None, server=None,
        **kwargs,
    ):
        """Native TLS when the context carries one, asyncio's otherwise.

        The outbound hook: `create_connection(ssl=...)` lands here, and without
        it every `https://` call keeps the `asyncio.sslproto` path even on the
        metal loop.

        **The remaining keywords are forwarded, not restated.** This signature
        has changed across CPython releases -- `call_connection_made` exists in
        some and not in 3.14, and the two timeouts default to constants rather
        than None -- so naming them here would mean passing an argument the base
        does not accept. That failure is invisible: `_accept_connection2`
        swallows it and drops the connection, which surfaces as a client-side
        `UNEXPECTED_EOF_WHILE_READING` with nothing in the log to connect it to.

        Only the inbound-accept case is excluded (`server is not None` or
        `server_side`): a listener with a native context never reaches here at
        all, because `_start_serving` already took the native path.
        """
        native_tls = getattr(sslcontext, "metal", None)
        if (
            self._poller is not None
            and native_tls is not None
            and server is None
            and not server_side
            and kwargs.get("call_connection_made", True)
        ):
            return _wheel_ext.SocketTransport(
                self, rawsock, protocol, waiter, extra, None, False, -1,
                native_tls, server_hostname,
            )
        return super()._make_ssl_transport(
            rawsock, protocol, sslcontext, waiter,
            server_side=server_side, server_hostname=server_hostname,
            extra=extra, server=server, **kwargs,
        )

    def _stop_serving(self, sock):
        if (
            self._poller is not None
            and self._poller._remove_uring_listener(sock.fileno())
        ):
            sock.close()
            return
        return super()._stop_serving(sock)

    async def sendfile(self, transport, file, offset=0, count=None, *, fallback=True):
        """Send a file over `transport`, including the native C transport.

        asyncio decides how to send a file by reading `_sendfile_compatible`
        off the transport and defaulting it to *unsupported*, which raises
        rather than falling back. The native transport is not an
        `asyncio` `_SelectorSocketTransport`, so it carried no such attribute
        and `wreath.file` -- every `FileResponse`, and all of
        `wreath.staticfiles` -- raised `RuntimeError` on the metal tier while
        working everywhere else.

        Neither stock path can simply be switched on for it. The native path
        reaches into `loop._transports[transp._sock_fd]`, `transp._sock`, and
        `transp._make_empty_waiter()`; the fallback path refuses any transport
        that is not an `asyncio` `_FlowControlMixin` subclass. So the sequence
        is done here, over the surface the native transport does have.

        Reading is paused for the duration -- the descriptor is about to be
        written by `os.sendfile` rather than by the transport -- and the head
        of the response is drained first, because sendfile writes to the socket
        directly and would otherwise overtake it.
        """
        if type(transport) is not _wheel_ext.SocketTransport:
            return await super().sendfile(
                transport, file, offset, count, fallback=fallback
            )
        if transport.is_closing():
            raise RuntimeError("Transport is closing")
        sock = transport.get_extra_info("socket")
        if sock is None:
            raise RuntimeError("transport has no socket to send on")
        # `sock_sendfile` validates mode, offset and count itself, so there is
        # nothing to add here beyond having a socket to send on.
        resume_reading = transport.is_reading()
        transport.pause_reading()
        try:
            await transport._empty_waiter()
            return await self.sock_sendfile(
                sock, file, offset, count, fallback=False
            )
        finally:
            if resume_reading and not transport.is_closing():
                transport.resume_reading()

    def _make_socket_transport(self, sock, protocol, waiter=None, *,
                               extra=None, server=None):
        if self._native_transport:
            # Native C transport: direct recv/send, one contiguous write buffer,
            # bounded eager read-drain. App-facing behaviour matches asyncio.
            return _wheel_ext.SocketTransport(self, sock, protocol, waiter, extra, server)
        return super()._make_socket_transport(
            sock, protocol, waiter, extra=extra, server=server)

    # -- native-reactor surface --------------------------------------------
    def reactor_backend(self) -> str:
        return self._backend

    def reactor_timers(self) -> str:
        return "wheel" if self._wheel is not None else "heap"

    def reactor_stats(self) -> dict[str, int]:
        return dict(self._reactor_stats)


# `create_future` and `get_debug` in C, replacing the two inherited from
# `asyncio.base_events`.
#
# Between them they are two interpreter frames on every future this loop
# creates: `create_future` itself, and the `get_debug` that
# `asyncio.Future.__init__` calls on its loop before deciding whether to capture
# a source traceback. That is charged per future, and a PostgreSQL query creates
# two of them -- the operation's, and the protocol's read waiter.
#
# The C replacements return exactly what the originals did, `get_debug` included, so
# `loop.set_debug(True)` still works and a debug loop still captures tracebacks.
# Grafted rather than declared in the class body because they are C methods on a
# Python heap type; `_install_loop_fastpath` does the `PyDescr_NewMethod` and is
# a no-op for every other loop.
if _wheel_ext is not None and hasattr(_wheel_ext, "_install_loop_fastpath"):
    _wheel_ext._install_loop_fastpath(EventLoop)


def available_backends() -> tuple[str, ...]:
    backends = []
    if hasattr(selectors, "EpollSelector"):
        backends.append("epoll")
    # io_uring lands in a later stage; advertised only when the C poller exists.
    return tuple(backends)


def _default_backend() -> str:
    backends = available_backends()
    if not backends:
        raise RuntimeError("no supported reactor backend on this platform")
    return backends[0]


def new_event_loop(backend: str | None = None, *, timers: str = "heap") -> EventLoop:
    backend = backend or _default_backend()
    if backend not in available_backends():
        raise ValueError(f"unsupported reactor backend: {backend!r}")
    selector = selectors.EpollSelector()
    return EventLoop(selector, backend=backend, timers=timers)


def _metal_gc_mode(configured: str | None, *, gil_enabled: bool) -> str:
    """Resolve Metal's collector ownership once, before the loop is built.

    The idle collector is loop-local but ``gc.collect()`` is process-wide.  A
    free-threaded server has several independent loops in one process, so none
    of them can safely infer that a quiet instant belongs to the whole process.
    Keep the policy available as an explicit diagnostic, but default to
    CPython's coordinated collector when the GIL is actually disabled.
    """
    if configured is not None:
        if configured not in ("stock", "idle"):
            raise ValueError("WREATH_METAL_GC must be 'stock' or 'idle'")
        return configured
    return "idle" if gil_enabled else "stock"


def metal_event_loop(
    *, worker_id: int = 0, reuse_port: bool | None = None,
    diagnostics: bool = False, gc_mode: str | None = None,
    adaptive_polling: bool | None = None,
) -> EventLoop:
    """The event loop for the ``metal`` tier: native C poller + transport.

    Metal always owns socket I/O through io_uring, uses the native timing wheel,
    native transport, direct native poller dispatch, deferred task-run polling,
    and no callback-statistics bookkeeping. Backend selection belongs to other Wreath
    execution tiers, not to metal.

    ReactorPoller reads the wheel's exact next deadline, so there is no recurring
    bridge tick and an idle server sleeps until native work or a real deadline.

    With the GIL enabled, Metal also owns its cycle collector
    (``gc_mode="idle"``): collection runs in the loop's idle gaps rather than
    wherever CPython's allocation counter happens to trip. A GIL-disabled
    runtime defaults to ``stock`` because collection is process-wide while idle
    detection belongs to one loop. Both choices remain explicitly ablatable via
    ``WREATH_METAL_GC``. ``WREATH_METAL_GC_FREEZE=0`` independently keeps the
    startup heap traceable.
    """
    import os

    # Default to CPython's C Task: the Python inline-drive WreathTask measured as
    # a net loss on the runtime path (it needs a C implementation to pay off), so
    # metal's win is the native poller+transport fusion, not the Python task driver.
    tasks = "auto"
    timers = "wheel"
    transport = True
    native_loop = True
    direct_task_steps = True
    if reuse_port is None:
        reuse_port = os.environ.get("WREATH_METAL_REUSEPORT", "0") == "1"
    if gc_mode is None:
        gc_mode = _metal_gc_mode(
            os.environ.get("WREATH_METAL_GC"),
            gil_enabled=sys._is_gil_enabled(),
        )
    if adaptive_polling is None:
        # The spin is the one metal-only thing on the wait path that can cost a
        # request time without doing any work for it (a miss burns its whole
        # budget and then blocks anyway), so it needs an ablation switch like
        # the send path and the collector have.
        poll_setting = os.environ.get("WREATH_METAL_ADAPTIVE_POLL", "1")
        if poll_setting not in ("0", "1"):
            raise ValueError("WREATH_METAL_ADAPTIVE_POLL must be '0' or '1'")
        adaptive_polling = poll_setting == "1"
    freeze_setting = os.environ.get("WREATH_METAL_GC_FREEZE", "1")
    if freeze_setting not in ("0", "1"):
        raise ValueError("WREATH_METAL_GC_FREEZE must be '0' or '1'")
    backend = _default_backend()
    return EventLoop(selectors.EpollSelector(), backend=backend,
                     timers=timers, tasks=tasks, stats=False,
                     adaptive_polling=adaptive_polling, diagnostics=diagnostics,
                     native_transport=transport, native_loop=native_loop,
                     direct_task_steps=direct_task_steps, worker_id=worker_id,
                     reuse_port=reuse_port, gc_mode=gc_mode,
                     gc_freeze=freeze_setting == "1")


class _ServerHandle:
    """Returned by :func:`serve`; owns the running server's teardown."""

    def __init__(self, server: Any, host: str, port: int | None, udp_port: int | None):
        self._server = server
        self.host = host
        self.port = port
        self.udp_port = udp_port

    async def aclose(self) -> None:
        await self._server.close()


async def serve(
    app: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    protocols: tuple[HttpProtocolName, ...] = ("http/1.1",),
    config: Any = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> _ServerHandle:
    """Serve an ASGI app on the reactor. Stage 0: plaintext HTTP/1.1.

    Reuses the framework's own `wreath.server.Server`, so the reactor
    hosts the real protocol stack over its own sockets and timers. TLS (h2) and
    QUIC (h3) integration land in a later stage.
    """
    from . import server as _server

    loop = loop or asyncio.get_running_loop()
    protocols = tuple(protocols)
    # Stage 0 drives bare ASGI apps in tests; lifespan is disabled so an app that
    # only handles "http" scopes is not invoked with a "lifespan" scope.
    if config is None:
        config = _server.ServerConfig(
            protocols=protocols, host=host, port=port, lifespan="off"
        )
    else:
        config = dataclasses.replace(
            config, protocols=protocols, host=host, port=port, lifespan="off"
        )

    if "h2" in protocols or "h3" in protocols:
        raise NotImplementedError(
            "reactor.serve() binds plaintext HTTP/1.1 today; h2 (TLS/ALPN) and "
            "h3 (QUIC) integration is the next reactor stage"
        )

    server = _server.Server(app, config, loop)
    await server._start(ssl=None, tls=None)
    tcp_port = server.sockets[0].getsockname()[1] if server.sockets else port
    return _ServerHandle(server, host, tcp_port, None)
