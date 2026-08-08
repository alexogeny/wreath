"""One daemon thread that ticks on an interval and flushes on the way out.

`wreath._projector` drains the flight recorder's ring; `wreath._export` ships
the traces and metrics that come out of it. Two different jobs that wanted the
same small thing: a background thread that does a unit of work, waits, and does
it again, and that on shutdown *drains once more after the writers are quiet*.

Written twice, the last part was re-derived twice -- and it is the part that
fails silently. A `stop` that joins the thread and returns loses whatever
arrived between the final tick and the join: no error, no counter, just a gap at
the end of every run, on the one path nobody watches because the process is
already going away. The projector settles twice on the way out (a completion
seen on the last cycle still needs its tail); the exporter flushes traces and
metrics but deliberately not logs. Those differences are the callers' business,
which is why `flush` is a parameter rather than something inferred from `tick`.

`stop` runs `flush` whether or not a thread was ever started -- `_abort_startup`
stops a projector that never ran, and the cells it already holds still have to
go somewhere.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class DrainThread:
    """A named daemon thread running `tick` every `interval` seconds."""

    __slots__ = ("_flush", "_interval", "_name", "_stop", "_thread", "_tick")

    def __init__(
        self,
        name: str,
        interval: float,
        tick: Callable[[], object],
        flush: Callable[[], object],
    ) -> None:
        self._name = name
        self._interval = interval
        self._tick = tick
        self._flush = flush
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def thread(self) -> threading.Thread | None:
        """The running thread, or None before `start` and after `stop`."""
        return self._thread

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Spawn the thread. Idempotent; a stopped instance can restart."""
        if self.running:
            return
        self._stop.clear()
        thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the thread to finish, join it, then flush what it left."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            self._thread = None
        self._flush()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self._interval)
