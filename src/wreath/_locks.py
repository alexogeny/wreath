"""Distributed advisory locks over the application-owned PostgreSQL driver.

PostgreSQL advisory locks are cluster-global mutexes keyed by two ``int32``
values. Their correctness depends entirely on *which backend connection* holds
the lock, so these helpers own connection affinity:

* A **session-scoped** lock (:meth:`Database.lock`, :meth:`Database.try_lock`)
  pins one pooled connection for the lock's whole lifetime and unlocks on that
  same backend before returning the connection to the pool.
* An **xact-scoped** lock (:meth:`wreath.orm.session.Session.lock`) rides an
  open transaction on the session's already-pinned connection and is released
  automatically by ``COMMIT``/``ROLLBACK`` -- no explicit unlock, no affinity
  bookkeeping.

There is no new C here: these are plain SQL over the shared ``Connection``
surface (``pg_advisory_lock`` family), so they run identically on the native and
pure backends. Keys are hashed *server-side* (``hashtext``) for parity with the
migration runner's existing advisory-lock usage, and use the two-``int32`` form
``(namespace, object)`` -- a separate keyspace from the single-``bigint`` locks
the migration system takes, so the two never collide.

Locks route through a *primary* (writable) workload. Advisory locks cannot be
taken on a standby, and they are per-backend in-memory state that is never
replicated -- treat them as single-primary coordination, not a cross-failover
safety barrier, and keep guarded critical sections idempotent.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .postgres import Database, Workload

# A ``SET lock_timeout`` that fires surfaces as sqlstate 55P03 (lock_not_available).
_LOCK_NOT_AVAILABLE = "55P03"

_ACQUIRE = {"exclusive": "pg_advisory_lock", "shared": "pg_advisory_lock_shared"}
_TRY = {"exclusive": "pg_try_advisory_lock", "shared": "pg_try_advisory_lock_shared"}
_UNLOCK = {"exclusive": "pg_advisory_unlock", "shared": "pg_advisory_unlock_shared"}

# Both advisory-lock key operands are hashed in the server so Python and
# PostgreSQL can never disagree on the derived int32 (the migration runner hashes
# the same way). ``$1`` is the namespace, ``$2`` the caller's key.
_KEYED = "hashtext($1::text), hashtext($2::text)"


def _validate_mode(mode: str) -> None:
    if mode not in _ACQUIRE:
        raise ValueError(
            f"advisory lock mode must be 'exclusive' or 'shared', not {mode!r}"
        )


def _reject_read_only(database: Database, workload: str) -> None:
    # Imported lazily to avoid an import cycle (postgres imports this module).
    from .postgres import _READ_ONLY

    if workload in _READ_ONLY:
        raise ValueError(
            "advisory locks must run on a primary (writable) workload, not "
            f"{workload!r}; a read/replica pool runs read-only and cannot take "
            "advisory locks -- route locks through the 'write' workload"
        )


def _default_jitter(base: float) -> float:
    # Bounded non-negative jitter so followers do not thundering-herd the lock.
    return random.random() * base * 0.25


class AdvisoryLock:
    """A blocking, session-scoped advisory lock held for an ``async with`` block.

    The lock pins a single connection from *workload* (default ``"write"``, i.e.
    the primary) for the lifetime of the block and releases it -- and the lock --
    on exit. Because a session-scoped lock is bound to its backend connection,
    the connection is *withheld from the pool* while held; hold enough of these
    concurrently and ordinary queries can starve on ``acquire``. Prefer the
    xact-scoped :meth:`Session.lock` for request-path exclusion and reserve this
    for a handful of long-lived fleet locks.
    """

    __slots__ = ("_connection", "_database", "_key", "_mode", "_namespace", "_workload")

    def __init__(
        self,
        database: Database,
        key: str,
        *,
        namespace: str | None = None,
        mode: str = "exclusive",
        workload: Workload = "write",
    ) -> None:
        _validate_mode(mode)
        _reject_read_only(database, workload)
        self._database = database
        self._key = key
        self._namespace = namespace if namespace is not None else database.name
        self._mode = mode
        self._workload = workload
        self._connection: Any = None

    async def __aenter__(self) -> AdvisoryLock:
        connection = await self._database.acquire(self._workload)
        self._connection = connection
        try:
            await connection.fetchval(
                f"SELECT {_ACQUIRE[self._mode]}({_KEYED})",
                self._namespace,
                self._key,
            )
        except BaseException:
            # Never return a connection to the pool while it might hold the lock:
            # the acquire failed, so nothing is held -- hand the connection back.
            self._connection = None
            await self._database.release(self._workload, connection)
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        connection = self._connection
        self._connection = None
        if connection is None:
            return False
        await _release(self._database, self._workload, connection, self._mode,
                       self._namespace, self._key, unlock=True)
        return False


class AdvisoryTryLock:
    """A non-blocking (or timeout-bounded) session-scoped advisory lock.

    Used as ``async with db.try_lock(key) as held:`` -- *held* is the lock handle
    when acquired, or ``None`` when it could not be taken. With ``timeout`` set,
    acquisition blocks up to that many seconds using PostgreSQL's ``lock_timeout``
    (which inherits the server's fair-ish lock queue), not a client-side spin.
    """

    __slots__ = (
        "_connection", "_database", "_key", "_mode", "_namespace", "_timeout",
        "_workload",
    )

    def __init__(
        self,
        database: Database,
        key: str,
        *,
        timeout: float | None = None,
        namespace: str | None = None,
        mode: str = "exclusive",
        workload: Workload = "write",
    ) -> None:
        _validate_mode(mode)
        _reject_read_only(database, workload)
        self._database = database
        self._key = key
        self._namespace = namespace if namespace is not None else database.name
        self._mode = mode
        self._timeout = timeout
        self._workload = workload
        self._connection: Any = None

    async def __aenter__(self) -> AdvisoryTryLock | None:
        connection = await self._database.acquire(self._workload)
        acquired = False
        try:
            acquired = await self._attempt(connection)
        except BaseException:
            await _release(self._database, self._workload, connection, self._mode,
                           self._namespace, self._key, unlock=False)
            raise
        if not acquired:
            await _release(self._database, self._workload, connection, self._mode,
                           self._namespace, self._key, unlock=False)
            return None
        self._connection = connection
        # The handle is this object; presence (not None) signals the lock is held.
        return self

    async def _attempt(self, connection: Any) -> bool:
        if self._timeout is None or self._timeout <= 0:
            return bool(
                await connection.fetchval(
                    f"SELECT {_TRY[self._mode]}({_KEYED})",
                    self._namespace,
                    self._key,
                )
            )
        # Blocking acquire bounded by lock_timeout for fair queueing. The value is
        # an int we compute, never user text, so the inline SET is injection-safe.
        from .postgres import PostgresError

        milliseconds = max(1, int(self._timeout * 1000))
        await connection.execute(f"SET lock_timeout = {milliseconds}")
        try:
            await connection.fetchval(
                f"SELECT {_ACQUIRE[self._mode]}({_KEYED})",
                self._namespace,
                self._key,
            )
            return True
        except PostgresError as error:
            if getattr(error, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            return False
        finally:
            # Never leak a non-default timeout onto the pooled connection.
            await connection.execute("SET lock_timeout = DEFAULT")

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        connection = self._connection
        self._connection = None
        if connection is None:
            return False
        await _release(self._database, self._workload, connection, self._mode,
                       self._namespace, self._key, unlock=True)
        return False


class SingletonRunner:
    """Run one coroutine at a time across the whole fleet, via an advisory lock.

    ``work`` is a zero-argument callable returning a *fresh* awaitable each time
    it is invoked (a coroutine can only be awaited once, and leadership may be
    re-established after a failover). The winner holds a dedicated connection and
    the advisory lock for as long as ``work()`` runs; if the process dies, its
    backend connection drops, PostgreSQL releases the lock, and a follower is
    promoted within one ``poll_interval``. The guarded work must be idempotent --
    failover can hand leadership over mid-flight.
    """

    __slots__ = (
        "_database", "_jitter", "_key", "_namespace", "_poll", "_stopped",
        "_task", "_work", "_workload",
    )

    def __init__(
        self,
        database: Database,
        key: str,
        work: Callable[[], Awaitable[Any]],
        *,
        namespace: str | None = None,
        workload: Workload = "write",
        poll_interval: float = 5.0,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        _reject_read_only(database, workload)
        if poll_interval <= 0:
            raise ValueError("run_singleton poll_interval must be positive")
        self._database = database
        self._key = key
        self._namespace = namespace if namespace is not None else database.name
        self._work = work
        self._workload = workload
        self._poll = poll_interval
        self._jitter = jitter if jitter is not None else _default_jitter
        self._stopped = False
        self._task: asyncio.Task[None] = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while not self._stopped:
            connection = await self._database.acquire(self._workload)
            held = False
            try:
                held = bool(
                    await connection.fetchval(
                        f"SELECT {_TRY['exclusive']}({_KEYED})",
                        self._namespace,
                        self._key,
                    )
                )
                if held:
                    # We are the leader for as long as we hold this connection.
                    await self._work()
                    return  # work() returned -> voluntarily relinquish leadership
            except asyncio.CancelledError:
                # Closing the connection is the surest release of the advisory
                # lock during cancellation, when a clean unlock round-trip may
                # itself be interrupted.
                await _drop(connection)
                connection = None
                held = False
                raise
            except BaseException:
                # Connection died or work() failed: drop the connection so the
                # lock is released server-side and a follower can be promoted.
                await _drop(connection)
                connection = None
                held = False
            finally:
                if connection is not None:
                    await _release(self._database, self._workload, connection,
                                   "exclusive", self._namespace, self._key,
                                   unlock=held)
            await asyncio.sleep(self._poll + self._jitter(self._poll))

    async def stop(self) -> None:
        """Relinquish leadership (if held) and stop contending for it."""
        if self._stopped:
            return
        self._stopped = True
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


async def _release(
    database: Database,
    workload: Workload,
    connection: Any,
    mode: str,
    namespace: str,
    key: str,
    *,
    unlock: bool,
) -> None:
    """Unlock (if held) on the *same* backend, then return it to the pool.

    If the unlock cannot be confirmed the connection's lock state is unknown, so
    it is closed rather than leased out still-locked -- the pool discards a closed
    connection on release.
    """
    broken = False
    if unlock:
        try:
            await connection.fetchval(
                f"SELECT {_UNLOCK[mode]}({_KEYED})", namespace, key
            )
        except BaseException:
            broken = True
    if broken and not getattr(connection, "closed", False):
        await connection.close()
    await database.release(workload, connection)


async def _drop(connection: Any) -> None:
    if connection is not None and not getattr(connection, "closed", False):
        try:
            await connection.close()
        except BaseException:
            pass


__all__ = ["AdvisoryLock", "AdvisoryTryLock", "SingletonRunner"]
