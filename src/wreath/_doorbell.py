"""One supervised ``LISTEN`` connection, shared by the bus and the job runner.

Both :class:`wreath.messaging.MessageBus` and :class:`wreath.jobs.JobRunner`
hold a single PostgreSQL connection for the life of the process, so a ``NOTIFY``
wakes them without waiting out a poll. Every reason a long-lived connection ends
-- a failover, an idle timeout, a ``pg_terminate_backend``, a network blip --
happens to that one connection eventually, and the driver's ``notifications()``
iterator *ends* rather than raising when it does.

Both subsystems once wrapped the whole thing in ``contextlib.suppress(Exception)``
and spawned the loop only after a successful connect. Two failures followed, and
they are why this module is supervised rather than hopeful:

* the stream ending is not an exception, so the loop returned having caught
  nothing -- fan-out stopped for the lifetime of the process, everything
  degraded to polling, and nothing said so;
* a database that was down *at boot* left the process with no doorbell task at
  all, so it could not recover even once the database came back.

The supervisor owns tasks; it does not resurrect them. So the retry lives here.

**What a pump owes, and where the two callers deliberately differ.**
:class:`Doorbell` treats any exception out of ``pump`` as a lost connection and
reconnects. A pump that runs *user* code must therefore catch and count that
itself -- otherwise a bug in a handler is indistinguishable from a flapping
database in the very counter that exists to tell them apart.

* ``MessageBus._pump`` dispatches to subscriber callbacks, so it catches per
  notification and counts ``handler_errors`` separately from ``reconnects``.
* ``JobRunner._pump`` only sets an event. There is no user code on its path, so
  it has nothing to catch and deliberately has no second counter -- one that
  could only ever read zero would suggest a distinction the runner cannot make.

That asymmetry is the point, not an oversight. It is the same rule
:mod:`wreath._busbridge` follows when it keeps ``rooms`` publishing inline and
unsuppressed while ``progress`` is fire-and-forget: extract the machinery, keep
the difference that carries meaning.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ._jobcore import compute_backoff

#: Reconnect backoff for the ``NOTIFY`` doorbell. 50ms first, because the
#: ordinary loss -- a failover, an idle timeout, a blip -- leaves a database that
#: hands back a working connection at once, and every millisecond without a
#: doorbell is fan-out on the floor and polling latency on every job.
BACKOFF_BASE = 0.05
#: Capped at the default poll interval, because that is the behaviour an outage
#: degrades to: retrying slower than the damage buys nothing. The cap also keeps
#: a database that is genuinely *down* at a handful of attempts a minute rather
#: than a reconnect storm against something already struggling.
BACKOFF_CAP = 5.0


def delay(attempt: int) -> float:
    """Seconds before reconnect ``attempt`` (1-based). Jittered so a fleet that
    lost the same database does not come back at it in lockstep."""
    return compute_backoff(
        attempt, kind="exp", base=BACKOFF_BASE, cap=BACKOFF_CAP, jitter=0.2,
    )


async def sleep_or_stop(stopping: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or until shutdown -- whichever comes first."""
    with contextlib.suppress(asyncio.TimeoutError):
        async with asyncio.timeout(seconds):
            await stopping.wait()


class Doorbell:
    """A held ``LISTEN`` connection that reopens itself until shutdown.

    ``pump`` receives the live connection and returns when its stream ends,
    which is the ordinary end of a dropped connection. See the module docstring
    for what a pump owes about *its own* exceptions.
    """

    __slots__ = ("_channels", "_conn", "_db", "_pump", "_workload", "reconnects")

    def __init__(
        self,
        *,
        database: Any,
        workload: str,
        pump: Callable[[Any], Awaitable[None]],
        channels: Sequence[str] = (),
    ) -> None:
        self._db = database
        self._workload = workload
        self._pump = pump
        #: Assignable after construction because a bus cannot know its channels
        #: until subscriptions are registered, which happens after ``__init__``.
        self._channels: tuple[str, ...] = tuple(channels)
        self._conn: Any = None
        #: Connections lost *and* failed opens, including the one attempted on
        #: the startup path -- a process that came up against a database that
        #: was down has no doorbell, which is the same outage.
        self.reconnects = 0

    @property
    def connection(self) -> Any:
        """The held connection, or ``None`` between attempts and after release."""
        return self._conn

    @property
    def channels(self) -> tuple[str, ...]:
        return self._channels

    @channels.setter
    def channels(self, value: Sequence[str]) -> None:
        self._channels = tuple(value)

    async def open(self) -> bool:
        """Take a connection and ``LISTEN`` on every wire channel.

        Reports failure rather than raising, because a database that will not
        give us a connection is precisely the condition :meth:`run` exists to
        ride out -- and because this also runs on the startup path, where it must
        not stop a caller from doing its own work by polling. Cancellation is not
        a failure and propagates.
        """
        connection: Any = None
        try:
            connection = await self._db.acquire(self._workload)
            for wire in self._channels:
                await connection.listen(wire)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the caller backs off and tries again
            self.reconnects += 1
            if connection is not None:
                with contextlib.suppress(Exception):
                    await self._db.release(self._workload, connection)
            return False
        self._conn = connection
        return True

    async def release(self) -> None:
        """Give the held connection back, exactly once.

        Both the owner's ``drain`` and :meth:`run`'s own teardown call this, and
        a clean shutdown runs them in that order, so the swap-to-``None`` is what
        keeps the second caller from releasing a connection twice.
        """
        connection, self._conn = self._conn, None
        if connection is None:
            return
        with contextlib.suppress(Exception):
            await self._db.release(self._workload, connection)

    async def run(self, stopping: asyncio.Event) -> None:
        """Hold the connection for the process's lifetime, and get it back."""
        loop = asyncio.get_running_loop()
        attempt = 0
        while not stopping.is_set():
            if self._conn is None and not await self.open():
                attempt += 1  # `open` counted it
                await sleep_or_stop(stopping, delay(attempt))
                continue
            opened = loop.time()
            try:
                await self._pump(self._conn)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken stream is a lost connection
                pass
            finally:
                await self.release()
            if stopping.is_set():
                # Shutdown closed it; that is not an outage and not a reason to
                # open another one on the way out.
                break
            self.reconnects += 1
            # A connection that is accepted and then dies immediately is a flap,
            # not a recovery, so only one that outlived the backoff cap resets
            # the count -- otherwise a database in that state gets retried at the
            # base delay forever, which is the storm the cap exists to stop.
            attempt = 1 if loop.time() - opened >= BACKOFF_CAP else attempt + 1
            await sleep_or_stop(stopping, delay(attempt))
