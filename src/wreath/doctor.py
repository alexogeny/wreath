"""Diagnose the bugs a green test suite cannot see.

Today that means the N+1 query: fifty fast statements where one belonged. It is
the most common performance defect in an ORM-backed API and the least visible,
because every individual part of it is correct. Finding it needs the route and
the queries in the same field of view, and in most stacks nothing holds both --
the ORM does not know what it is serving, and the server does not know what the
ORM did.

Wreath owns both layers, so it can say the useful sentence:

```python
GET /llamas issued 51 statements; 50 of them hydrated Trek
```
Two ways to hear it. In development, install the guard and the request fails at
the query that crossed the line, with a traceback pointing at the loop:

```python
app.add_middleware(NPlusOneGuard(limit=10))
```
In production, the Flight Recorder already records what each request did, so
`wreath doctor n-plus-one <socket>` reads it back out without reproducing
anything -- and each finding carries the `request_id` that `wreath replay`
needs to turn it into a regression test.
"""

from __future__ import annotations

import logging as _stdlib_logging
from collections.abc import Callable
from typing import Any

from ._nplusone import (
    Finding,
    NPlusOneDetected,
    QueryLedger,
    Repetition,
    find_n_plus_one,
    query_ledger,
    watch,
)

__all__ = [
    "Finding",
    "check_logging_streams",
    "NPlusOneDetected",
    "NPlusOneGuard",
    "Repetition",
    "diagnose_n_plus_one",
    "find_n_plus_one",
]

_STATE_TOKEN = "_wreath_nplusone_token"


async def diagnose_n_plus_one(
    client: Any, *, threshold: int = 10, limit: int = 256
) -> list[Finding]:
    """Scan a running server's recorded traces through its Inspector.

    `client` is a connected `InspectorClient`. Reads
    the recent timeline plus the route and model name tables, and returns
    findings worst first -- so a production N+1 is diagnosed from outside the
    process, without reproducing the request that caused it.

    Requires the server to be recording in Detailed mode or better: phases are
    what carry the model, and an unsampled request has none. A server whose
    metadata predates its ORM simply reports numeric model IDs.
    """
    timeline = await client.timeline(limit=limit)
    routes = (await client.metadata("routes")).get("rows", ())
    models = (await client.metadata("models")).get("rows", ())
    return find_n_plus_one(
        timeline.get("traces", ()), threshold=threshold, routes=routes, models=models
    )


class NPlusOneGuard:
    """Fail (or report) a request that queries one model over and over.

    `limit` is how many times a single model may be hydrated within one
    request before that is treated as a defect. Ten is a deliberate default:
    a handful of related lookups is ordinary, ten of the same model is a loop.

    By default the `limit`-th query raises `NPlusOneDetected` from
    inside the ORM call, which is the whole point -- the traceback names the
    loop. Pass `on_detect` to log the `Finding` instead and let the
    request finish, which is what you want in staging:

    ```python
    app.add_middleware(NPlusOneGuard(limit=25, on_detect=log.warning))
    ```
    Each model trips once per request, so a runaway loop yields one diagnosis
    rather than a thousand. Intended for development and staging: it costs one
    `ContextVar` read per ORM query, which is nothing against a round trip,
    but a guard that fails production requests is a worse outage than the N+1.
    """

    global_scope = True
    __slots__ = ("_limit", "_on_detect")

    def __init__(
        self, *, limit: int = 10, on_detect: Callable[[Finding], None] | None = None
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._on_detect = on_detect
        # Arms the ORM seam. Until a guard exists the seam does not so much as
        # read the ContextVar, so an app that never installs one pays nothing.
        watch()

    async def before(self, request: Any) -> None:
        ledger = QueryLedger(
            limit=self._limit,
            route=f"{request.method} {request.path}",
            on_exceeded=self._on_detect or _raise,
        )
        request.state.__setattr__(_STATE_TOKEN, query_ledger.set(ledger))
        return None

    async def after(self, request: Any, response: Any) -> Any:
        token = request.state.get(_STATE_TOKEN)
        if token is not None:
            # Reset rather than set(None): a nested guard (or a test) must get
            # its own binding back, not a cleared one. A binding that escapes
            # anyway dies with the request's task.
            query_ledger.reset(token)
        return response


def _raise(finding: Finding) -> None:
    raise NPlusOneDetected(finding)


# --- split logging streams ---------------------------------------------------


def check_logging_streams(*, active: bool | None = None) -> list[str]:
    """Report stdlib loggers that will bypass wreath's log stream.

    `wreath.logging` deliberately does not install itself on the root logger:
    a framework that seizes global logging state fights `dictConfig`, surprises
    anyone with handlers of their own, and either double-emits or silently
    discards their configuration. The cost of that restraint is that a library
    logging to `logging.getLogger(...)` produces a second, disjoint stream --
    which an operator discovers at 3am while correlating by hand.

    So the restraint is paired with a check. This is the check: it names the
    loggers holding their own handlers while wreath logging is active, so the
    split is something tooling reports rather than something a human trips over.

    Args:
        active: Whether wreath logging is running. Defaults to asking the
            installed runtime; pass it explicitly to diagnose a configuration
            that is not the current process's.

    Returns:
        One human-readable line per logger that will not reach wreath's stream.
        Empty when there is nothing to say -- including when wreath logging is
        inactive, because then there is no second stream to be split from.
    """
    from . import logging as wreath_logging

    if active is None:
        active = wreath_logging.installed().sink is not None
    if not active:
        return []
    bridged = wreath_logging.bridged_loggers()
    if "root" in bridged or "" in bridged:
        return []  # the root bridge catches everything that propagates
    findings: list[str] = []
    manager = _stdlib_logging.Logger.manager
    for name, logger in sorted(manager.loggerDict.items()):
        if not isinstance(logger, _stdlib_logging.Logger) or not logger.handlers:
            continue
        if name in bridged:
            continue
        if all(
            isinstance(h, _stdlib_logging.NullHandler) for h in logger.handlers
        ) and not logger.propagate:
            # A NullHandler on a non-propagating logger is a library silencing
            # itself, not a stream competing with wreath's.
            continue
        findings.append(
            f"logger {name!r} has {len(logger.handlers)} handler(s) of its own, so "
            f"its records will not reach wreath's log stream; bridge it with "
            f"wreath.logging.stdlib_bridge(logging.getLogger({name!r})) or accept "
            f"two streams deliberately"
        )
    return findings
