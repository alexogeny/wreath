"""The native reactor — an asyncio-compatible event loop with a fused fast path.

This is the loop behind Wreath's experimental **metal** tier (``--loop metal``):
alongside ``pure`` (Python) and ``native`` (C on the stock asyncio loop), metal
runs the native server on this reactor — an ``asyncio``-compatible loop that
inline-drives non-suspending request coroutines and, when ``timers="wheel"``,
backs every ``call_later`` deadline with the native hashed timing wheel from
``wreath._native._reactor`` instead of asyncio's timer heap ("bare metal").

The loop stays a strict ``asyncio`` loop, so ``await`` of anything works. The
wheel path trades sub-millisecond timer precision for O(1) deadline churn, which
is the right trade for keep-alive/request timeouts; it is opt-in and off in the
default loop so general asyncio-timer semantics are preserved. The full story and
the measurement behind the wheel choice are in
docs/explorations/the-timer-that-wouldnt-settle.md.


Stage 0 lands the loop core and the headline novel technique: **inline-drive**.
A request coroutine that completes without ever suspending is driven straight to
completion at ``create_task`` time — no ``Task`` scheduled onto the ready queue,
no ``call_soon`` round trip, no done-callback hop. Only a coroutine that
actually suspends is promoted to the normal (scheduled) driver.

This is the async analogue of a leaf-call optimization: neither stock asyncio
nor uvloop does it, because you cannot hand an already-stepped coroutine to
``asyncio.Task`` (it would ``send(None)`` twice). We sidestep that by driving the
coroutine ourselves from the first step, using the C task-state helpers with an
explicit leave/enter around the inline step so it composes even when a running
task creates another task.

The loop is a ``SelectorEventLoop`` subclass so that every third-party ``await``
(sleep, gather, locks, executors, DNS, TLS, transports) works unchanged from day
one; the C poller, timing wheel, and protocol→coroutine fusion replace internals
in later stages behind the same observable contract (see tests/reactor/).
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import selectors
import socket
from asyncio import events as _events
from asyncio.tasks import (  # C-accelerated task-state helpers
    _enter_task,
    _leave_task,
    _register_task,
)
from typing import Any

__all__ = [
    "new_event_loop",
    "available_backends",
    "serve",
    "EventLoop",
    "WreathTask",
]


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

    Behaves like ``asyncio.Task`` for every scheduled (suspending) path; the only
    departure is that the *first* step runs synchronously inside ``create_task``
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
        if self.done():
            loop._reactor_stats["inline_completions"] += 1
        else:
            loop._reactor_stats["tasks_promoted"] += 1

    def __step(self, exc=None):
        loop = self._loop
        coro = self._coro
        self._fut_waiter = None
        _enter_task(loop, self)
        loop._reactor_stats["coro_steps"] += 1
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
        except BaseException as e:
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
        except BaseException as exc:
            self.__step(exc)
        else:
            self.__step()


try:
    from wreath._native import _reactor as _wheel_ext
except ImportError:  # pragma: no cover - extension always built in-tree
    _wheel_ext = None


class EventLoop(asyncio.SelectorEventLoop):
    """SelectorEventLoop augmented with the reactor's fast path and telemetry.

    ``timers="wheel"`` routes every ``call_later``/``call_at`` deadline through
    the native hashed timing wheel instead of asyncio's heap. A single bridge
    tick (one heap timer, re-armed only while the wheel is non-empty) drives
    expiry, so no ``_run_once`` reimplementation is needed. Timer precision is
    the wheel resolution (1 ms) -- ample for server deadlines, which is why it is
    opt-in rather than the default.
    """

    def __init__(self, selector=None, *, backend: str = "epoll",
                 timers: str = "heap", tasks: str = "inline", stats: bool = True,
                 native_transport: bool = False, native_loop: bool = False,
                 direct_task_steps: bool = True, worker_id: int = 0,
                 reuse_port: bool = False, adaptive_polling: bool = True,
                 diagnostics: bool = True, wheel_slots: int = 4096,
                 wheel_resolution: float = 0.001):
        super().__init__(selector)
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
        self._poller = None
        self._reactor_stats = _stats_template()
        self._wheel = None
        self._wheel_schedule = None  # cached bound schedule_call: no per-timer attr lookup
        self._wheel_res = wheel_resolution
        self._wheel_tick_handle = None
        if timers == "wheel":
            if _wheel_ext is None:
                raise RuntimeError("timers='wheel' needs wreath._native._reactor")
            self._wheel = _wheel_ext.TimingWheel(
                resolution=wheel_resolution, slots=wheel_slots, base=self.time())
            self._wheel_schedule = self._wheel.schedule_call
        if not stats:
            # No introspection: shadow the counting call_soon/_run_once with the
            # base C implementations so the hot path pays no Python override frame.
            self.call_soon = super().call_soon
            self._run_once = super()._run_once
        if self._native_loop:
            self._install_native_loop()

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
        if self._poller is not None:
            # Break the loop<->poller cycle and free the ring/registry promptly.
            self._poller.close()
        super().close()

    def create_task(self, coro, *, name=None, context=None):
        if self._tasks == "inline":
            return WreathTask(coro, loop=self, name=name, context=context)
        return super().create_task(coro, name=name, context=context)

    def call_soon(self, callback, *args, context=None):
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

    def _run_once(self):
        if self._stats_on:
            self._reactor_stats["poll_calls"] += 1
        return super()._run_once()

    def _start_serving(
        self, protocol_factory, sock, sslcontext=None, server=None, backlog=100,
        ssl_handshake_timeout=None, ssl_shutdown_timeout=None,
    ):
        if self._poller is not None and sslcontext is None:
            native_server = (protocol_factory, server, socket.socket)
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

    def _stop_serving(self, sock):
        if (
            self._poller is not None
            and self._poller._remove_uring_listener(sock.fileno())
        ):
            sock.close()
            return
        return super()._stop_serving(sock)

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


def metal_event_loop(
    *, worker_id: int = 0, reuse_port: bool | None = None,
    diagnostics: bool = False,
) -> EventLoop:
    """The event loop for the ``metal`` tier: native C poller + transport.

    Metal always owns socket I/O through io_uring, uses the native timing wheel,
    native transport, direct native poller dispatch, deferred task-run polling,
    and no callback-statistics bookkeeping. Backend selection belongs to other Wreath
    execution tiers, not to metal.

    ReactorPoller reads the wheel's exact next deadline, so there is no recurring
    bridge tick and an idle server sleeps until native work or a real deadline.
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
    backend = _default_backend()
    return EventLoop(selectors.EpollSelector(), backend=backend,
                     timers=timers, tasks=tasks, stats=False,
                     adaptive_polling=True, diagnostics=diagnostics,
                     native_transport=transport, native_loop=native_loop,
                     direct_task_steps=direct_task_steps, worker_id=worker_id,
                     reuse_port=reuse_port)


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
    protocols: tuple[str, ...] = ("http/1.1",),
    config: Any = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> _ServerHandle:
    """Serve an ASGI app on the reactor. Stage 0: plaintext HTTP/1.1.

    Reuses the framework's own :class:`wreath.server.Server`, so the reactor
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
