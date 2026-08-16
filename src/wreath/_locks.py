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
import logging
import random
import warnings
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
# PostgreSQL can never disagree on the derived key (the migration runner hashes
# the same way). ``$1`` is the namespace, ``$2`` the caller's key.
#
# One 64-bit key, not two 32-bit ones. The two-argument form fills each operand
# with `hashtext`, so distinct (namespace, key) pairs collided at roughly 77 000
# of them -- and a collision is silent, appearing only as one caller waiting on
# a lock that has nothing to do with it. `hashtextextended` is the 64-bit
# variant, already what `wreath.migrations` uses; seeding the namespace hash
# into the key hash keeps the pair unambiguous without needing a separator that
# text cannot contain.
_KEYED = "hashtextextended($2::text, hashtextextended($1::text, 0))"


def _validate_mode(mode: str) -> None:
    if mode not in _ACQUIRE:
        raise ValueError(
            f"advisory lock mode must be 'exclusive' or 'shared', not {mode!r}"
        )


def _reject_read_only(database: Database, workload: Workload) -> None:
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


def _init_session_lock(
    lock: Any,
    database: Database,
    key: str,
    namespace: str | None,
    mode: str,
    workload: Workload,
) -> None:
    """Validate and initialise the state common to session lock handles."""
    _validate_mode(mode)
    _reject_read_only(database, workload)
    lock._database = database
    lock._key = key
    lock._namespace = namespace if namespace is not None else database.name
    lock._mode = mode
    lock._workload = workload
    lock._connection = None


# Each session-scoped advisory lock pins a pooled connection for its whole
# lifetime, so holding too many at once starves the pool of connections for
# ordinary queries. Track the live count per (database, workload) so an acquire
# that would exhaust the pool's headroom warns loudly instead of deadlocking
# silently at request time.
_held_locks: dict[tuple[int, str], int] = {}


def _pool_max_size(database: Database, workload: Workload) -> int | None:
    try:
        return database._configs[workload].max_size
    except (KeyError, AttributeError):
        return None


def _enter_held(database: Database, workload: Workload) -> None:
    key = (id(database), workload)
    held = _held_locks.get(key, 0) + 1
    _held_locks[key] = held
    max_size = _pool_max_size(database, workload)
    if max_size is not None and held >= max_size:
        warnings.warn(
            f"{held} session-scoped advisory lock(s) held on the {workload!r} pool "
            f"(max_size={max_size}); each pins a connection for its whole lifetime, "
            "leaving no headroom for ordinary queries -- raise the pool max_size, or "
            "prefer xact-scoped Session.lock for request-path exclusion.",
            ResourceWarning,
            stacklevel=3,
        )


def _exit_held(database: Database, workload: Workload) -> None:
    key = (id(database), workload)
    remaining = _held_locks.get(key, 0) - 1
    if remaining > 0:
        _held_locks[key] = remaining
    else:
        _held_locks.pop(key, None)


class _SessionLockExit:
    """The common release protocol for session-scoped lock handles."""

    __slots__ = ()

    _connection: Any
    _database: Database
    _key: str
    _mode: str
    _namespace: str
    _workload: Workload

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        connection = self._connection
        self._connection = None
        if connection is None:
            return False
        _exit_held(self._database, self._workload)
        await _release(
            self._database,
            self._workload,
            connection,
            self._mode,
            self._namespace,
            self._key,
            unlock=True,
        )
        return False


class AdvisoryLock(_SessionLockExit):
    """A blocking, session-scoped advisory lock held for an `async with` block.

    The lock pins a single connection from *workload* (default `"write"`, i.e.
    the primary) for the lifetime of the block and releases it -- and the lock --
    on exit. Because a session-scoped lock is bound to its backend connection,
    the connection is *withheld from the pool* while held; hold enough of these
    concurrently and ordinary queries can starve on `acquire`. Prefer the
    xact-scoped `Session.lock` for request-path exclusion and reserve this
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
        _init_session_lock(self, database, key, namespace, mode, workload)

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
        _enter_held(self._database, self._workload)
        return self

class AdvisoryTryLock(_SessionLockExit):
    """A non-blocking (or timeout-bounded) session-scoped advisory lock.

    Used as `async with db.try_lock(key) as held:` -- *held* is the lock handle
    when acquired, or `None` when it could not be taken. With `timeout` set,
    acquisition blocks up to that many seconds using PostgreSQL's `lock_timeout`
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
        _init_session_lock(self, database, key, namespace, mode, workload)
        self._timeout = timeout

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
        _enter_held(self._database, self._workload)
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

class SingletonRunner:
    """Run one coroutine at a time across the whole fleet, via an advisory lock.

    `work` is a zero-argument callable returning a *fresh* awaitable each time
    it is invoked (a coroutine can only be awaited once, and leadership may be
    re-established after a failover). The winner holds a dedicated connection and
    the advisory lock for as long as `work()` runs; if the process dies, its
    backend connection drops, PostgreSQL releases the lock, and a follower is
    promoted within one `poll_interval`. The guarded work must be idempotent --
    failover can hand leadership over mid-flight.
    """

    __slots__ = (
        "_database", "_jitter", "_key", "_lead_errors", "_namespace", "_poll",
        "_release_errors", "_stopped", "_task", "_work", "_workload",
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
        self._lead_errors = 0
        self._release_errors = 0
        self._task: asyncio.Task[None] = asyncio.ensure_future(self._run())

    @property
    def lead_errors(self) -> int:
        """How many leadership attempts ended in a failure rather than a return.

        A `work()` that fails every time is otherwise invisible: leadership is
        acquired, dropped, and re-contended on a timer, so the fleet looks busy
        while nothing is being done. This is the only signal that distinguishes
        "nobody has needed to lead yet" from "leading has never once worked".
        """
        return self._lead_errors

    @property
    def release_errors(self) -> int:
        """How many rounds ended with the connection's release itself failing.

        Distinct from `lead_errors`, which counts leadership attempts that failed
        to do their work. This counts rounds whose work may well have succeeded
        and whose *cleanup* did not, so a runner that is quietly failing to hand
        connections back is visible before the pool runs out of them.
        """
        return self._release_errors

    async def _run(self) -> None:
        while not self._stopped:
            connection = await self._database.acquire(self._workload)
            held = False
            counted = False
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
                    _enter_held(self._database, self._workload)
                    counted = True
                    await self._work()
                    return  # work() returned -> voluntarily relinquish leadership
            except asyncio.CancelledError:
                # Closing the connection is the surest release of the advisory
                # lock during cancellation, when a clean unlock round-trip may
                # itself be interrupted. The reference is kept so `finally` still
                # hands the slot back -- a closed connection is discarded by the
                # pool on release, but only if release is actually called.
                await _drop(connection)
                held = False
                raise
            except Exception:  # noqa: BLE001 -- breadth is the decision; see below
                # Connection died or work() failed: drop the connection so the
                # lock is released server-side and a follower can be promoted.
                #
                # Broad on purpose. `work()` is caller-supplied and may raise
                # anything, and the query above can fail in any driver-specific
                # way -- but every one of those has the same answer, so naming a
                # set would be a longer way of writing `Exception`. Counted,
                # because a `work()` that fails every time would otherwise flap
                # leadership forever with no signal: acquire, fail, drop, sleep,
                # repeat, while the fleet looks healthy.
                #
                # Deliberately *not* `BaseException`. `CancelledError` is handled
                # above and re-raised; `KeyboardInterrupt` and `SystemExit` must
                # end the process rather than be retried on a timer.
                self._lead_errors += 1
                await _drop(connection)
                held = False
                # Keep the reference so `finally` still calls `_release`. Setting
                # it to `None` here leaked the pool slot: `Pool.release` is what
                # removes a connection from `_borrowed`, so a dropped-but-never-
                # released connection pinned the slot forever, and the *next*
                # round blocked in `acquire()` until the pool was stopped. One
                # `work()` failure therefore ended leadership for the process --
                # invisibly, because a runner parked in `acquire()` looks exactly
                # like one that is simply not the leader.
            finally:
                if counted:
                    _exit_held(self._database, self._workload)
                if connection is not None:
                    from .postgres import PostgresError

                    try:
                        await _release(self._database, self._workload, connection,
                                       "exclusive", self._namespace, self._key,
                                       unlock=held)
                    except (PostgresError, OSError):
                        # A failed release degrades this round; it must not end
                        # the runner. Escaping a `finally` is exactly how it did:
                        # the task died for the process lifetime, and because
                        # nothing awaits `_task` until `stop()`, it surfaced only
                        # as "Task exception was never retrieved" at GC. A runner
                        # that has silently stopped contending is indistinguishable
                        # from one that is simply not the leader.
                        #
                        # Named types, not `Exception`: `Pool.release` raises
                        # `InterfaceError` on a double release, and the trailing
                        # `connection.close()` fails with `OSError` on a dead
                        # socket. Neither leaks the slot this catch lets through --
                        # the `InterfaceError` is raised *before* `_borrowed` is
                        # touched, and the close runs *after* the slot is already
                        # back. `CancelledError` still propagates, because `stop()`
                        # is waiting for it.
                        self._release_errors += 1
                        logging.getLogger("wreath").exception(
                            "advisory lock release failed for %r; still contending",
                            self._key,
                        )
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
    connection on release. That is why the catch below is broad: the round trip
    can fail in any driver-specific way, and every one of them means the same
    thing here, which is that confirmation did not happen.
    """
    broken = False
    try:
        if unlock:
            await connection.fetchval(
                f"SELECT {_UNLOCK[mode]}({_KEYED})", namespace, key
            )
    except Exception:  # noqa: BLE001 -- one answer for every failure; see docstring
        broken = True
    except BaseException:
        # Cancellation or interpreter exit leaves the lock state just as unknown,
        # so the connection still must not be leased out -- but the exception has
        # to reach the caller rather than being spent on cleanup.
        broken = True
        raise
    finally:
        # In a `finally` so the release happens on the raising path too. Leaking a
        # pool slot during cancellation is how a shutdown becomes a hang, and the
        # unlock is exactly where cancellation is most likely to land.
        if broken:
            await _drop(connection)
        await database.release(workload, connection)


async def _drop(connection: Any) -> None:
    """Best-effort close, so PostgreSQL releases the advisory lock server-side.

    Only ever called on a connection already being discarded, so a close that
    fails changes nothing a caller could act on -- the pool drops a closed *or* a
    broken connection on release either way. That is what makes the breadth
    harmless here rather than a swallowed signal.

    `CancelledError`, `KeyboardInterrupt` and `SystemExit` still propagate: the
    lock is released by the backend dying regardless, so there is nothing to be
    gained by spending a cancellation on a courtesy close.
    """
    if connection is not None and not getattr(connection, "closed", False):
        try:
            await connection.close()
        except Exception:  # noqa: BLE001 -- best-effort cleanup; see docstring
            pass


__all__ = ["AdvisoryLock", "AdvisoryTryLock", "SingletonRunner"]
