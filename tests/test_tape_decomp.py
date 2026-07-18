"""`wreath-tape-decomp`: middleware cost decomposition and its silent-failure guards."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath._devtools import tape_decomp
from wreath._devtools.sample_app import MIDDLEWARE_FACTORIES, build_realistic_app
from wreath.middleware.ratelimit import RateLimitMiddleware


def _template() -> dict[str, Any]:
    _, headers, method, path = build_realistic_app()
    return tape_decomp._scope(method, path, headers)


def _arms(mode: str = "both") -> list[tape_decomp.Arm]:
    names = [type(factory()).__name__ for factory in MIDDLEWARE_FACTORIES]
    return tape_decomp._build_arms(
        lambda: build_realistic_app()[0],
        names,
        lambda index: MIDDLEWARE_FACTORIES[index](),
        mode,
    )


def test_the_stack_is_built_from_factories_not_shared_instances() -> None:
    # Sharing one middleware object across arms let a token bucket drain, and
    # the drained arm then "measured" 429s -- faster than bare, which is how the
    # decomposition came to report a negative cost.
    first = [factory() for factory in MIDDLEWARE_FACTORIES]
    second = [factory() for factory in MIDDLEWARE_FACTORIES]
    for one, other in zip(first, second, strict=True):
        assert one is not other


def test_no_two_arms_share_a_middleware_object() -> None:
    seen: set[int] = set()
    for arm in _arms():
        for middleware in arm.middleware:
            assert id(middleware) not in seen, f"{arm.label} reuses a middleware instance"
            seen.add(id(middleware))


def test_the_sample_rate_limiter_cannot_drain_during_a_benchmark() -> None:
    limiter = next(
        factory() for factory in MIDDLEWARE_FACTORIES
        if isinstance(factory(), RateLimitMiddleware)
    )
    # Behavioral, not a private-attribute assertion: a full decomposition drives
    # well over a million requests through one bucket, and the limit must be out
    # of reach for all of them.
    from time import monotonic

    now = monotonic()
    for _ in range(200_000):
        assert limiter._try_acquire("k", 1.0, now) <= 0.0, "the sample limiter drains"


def test_every_arm_serves_a_200() -> None:
    arms = _arms()
    asyncio.run(tape_decomp._verify(arms, _template(), "before"))


def test_verify_refuses_an_arm_that_stopped_serving() -> None:
    arms = _arms("alone")
    # A limiter with nothing left is exactly the failure that made the tool lie.
    drained = tape_decomp._build_arms(
        lambda: build_realistic_app()[0],
        ["RateLimitMiddleware"],
        lambda _index: RateLimitMiddleware(limit=1, window=3600.0),
        "alone",
    )
    template = _template()
    asyncio.run(tape_decomp._run(drained[1].app, template, 5))
    with pytest.raises(SystemExit, match="not 200"):
        asyncio.run(tape_decomp._verify(drained, template, "after"))
    del arms


def test_a_full_stack_arm_exists_for_each_mode() -> None:
    for mode in ("alone", "cumulative", "both"):
        labels = [arm.label for arm in _arms(mode)]
        assert "full stack" in labels
        assert labels[0] == "bare"
        # The control must sit at the far end of the round, or it measures
        # adjacency rather than drift and reports a flatteringly small floor.
        assert labels[-1] == "A/A control"
