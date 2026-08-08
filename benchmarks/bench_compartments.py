"""Does per-route compartmentalization capture its own ceiling?

This benchmark prices custom-hook compartmentalization by *truncating* the stack:
an application built with two hooks instead of seven. That is what the saving
would be if deciding which two were free, and it says nothing about whether a
mechanism can collect it -- the earlier attempt at one measured -0.05us and was
reverted, against a CORS body now known to cost 4.1-4.7us.

So three arms, and the gap between the middle two is the whole question:

    full            seven custom hooks, every route running all of them
    compartments    seven registered, two applying to the measured route
    ceiling         two middlewares registered at all

`compartments` reaches the same answer as `ceiling` by a different route, so it
also has to *be* the same answer: the arm set asserts the bodies match before
reporting a time, because a compartment that skips a middleware by failing to
run something it should have run is not an optimization.

Interleaved with an A/A control at the far end of the round, and the round's
direction alternates -- without that this file's own numbers are position, not
cost. See `_devtools/measure.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

from wreath import Response, Wreath
from wreath._devtools.measure import Arm, _ordered, report, run, scope

#: The measured route runs these two; the other five decline it.
KEEP = (0, 1)
HOT = "/i/{x}"
COLD = "/everything/{x}"

_BODY = Response(b'{"ok":1}', media_type=b"application/json")


class _CustomHook:
    """A minimal application-defined global hook with observable work."""

    global_scope = True

    def __init__(self) -> None:
        self.calls = 0

    def before_sync(self, request: Any) -> None:
        self.calls += 1


CUSTOM_HOOK_FACTORIES = tuple(_CustomHook for _ in range(7))


class _Scoped:
    """A custom hook wrapper that declines every route but `COLD`.

    Delegation by attribute rather than by subclass: the hooks a middleware
    exposes decide how `_hook_program` compiles it, and copying that decision
    here would let this benchmark drift from the dispatcher it is measuring.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        for name in (
            "before_sync",
            "before",
            "after_inplace",
            "after_sync",
            "after",
            "global_scope",
        ):
            attribute = getattr(inner, name, None)
            if attribute is not None:
                setattr(self, name, attribute)

    def applies_to(self, method: str, path: str) -> bool:
        return path == COLD


def _routes(app: Wreath) -> Wreath:
    @app.get(HOT)
    async def hot(request: Any) -> Response:
        return _BODY

    @app.get(COLD)
    async def cold(request: Any) -> Response:
        return _BODY

    app._compile_routes()
    return app


def _full() -> Wreath:
    app = Wreath()
    for factory in CUSTOM_HOOK_FACTORIES:
        app.add_middleware(factory())
    return _routes(app)


def _compartments() -> Wreath:
    app = Wreath()
    for index, factory in enumerate(CUSTOM_HOOK_FACTORIES):
        instance = factory()
        app.add_middleware(instance if index in KEEP else _Scoped(instance))
    return _routes(app)


def _ceiling() -> Wreath:
    app = Wreath()
    for index in KEEP:
        app.add_middleware(CUSTOM_HOOK_FACTORIES[index]())
    return _routes(app)


def _text(value: Any) -> str:
    """Header names and values reach here as `str` or `bytes` by path."""
    return value.decode("latin-1") if isinstance(value, bytes | bytearray) else str(value)


def _raw(value: Any) -> bytes:
    return bytes(value) if isinstance(value, bytes | bytearray) else str(value).encode()


async def _answer(app: Wreath, template: dict[str, Any]) -> tuple[int, Any, bytes]:
    """The status, sorted headers and body one arm produces, for comparison."""
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(dict(template), receive, send)
    status, headers, body = 0, [], b""
    for message in sent:
        if message.get("type") in ("http.response.start", "wreath.response"):
            status = message.get("status", 0)
            headers = sorted(
                (name, _text(v))
                for k, v in (message.get("headers") or [])
                # Minted per request by design, so equal-by-shape is the most
                # that can be asserted about them.
                if (name := _text(k).lower())
                not in ("date", "x-request-id", "server-timing")
            )
        body += _raw(message.get("body") or b"")
    return status, headers, body


async def _assert_identical(arms: list[Arm], template: dict[str, Any]) -> None:
    """Compartments must answer what the ceiling answers, header for header."""
    answers = {arm.label: await _answer(arm.app, template) for arm in arms}
    # Every arm must be *serving*, not merely agreeing. Comparing the two arms
    # against each other missed a run where the baseline answered 500 on every
    # request: the arms with fewer middlewares still answered 200, so the
    # "saving" being measured was partly the gap between an error path and a
    # success path. Checked first, and per arm, for that reason.
    for label, (status, _headers, _body) in answers.items():
        if status != 200:
            raise SystemExit(
                f"bench-compartments: arm {label!r} answered {status}, not 200.\n"
                "Its timings would be the cost of that response, not of a "
                "served request."
            )
    ceiling = answers["ceiling: 2 registered"]
    got = answers["compartments: 2 of 7"]
    if got != ceiling:
        raise SystemExit(
            "bench-compartments: the compartment arm does not answer what the "
            f"ceiling answers.\n  ceiling:      {ceiling}\n  compartments: {got}\n"
            "A saving that changes the response is not a saving."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=14)
    parser.add_argument("--iterations", type=int, default=2500)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    arms = [
        Arm("full: 7 of 7 (baseline)", app=_full()),
        Arm("compartments: 2 of 7", app=_compartments()),
        Arm("ceiling: 2 registered", app=_ceiling()),
        Arm("A/A control", app=_full()),
    ]
    template = scope("GET", "/i/42", {"host": "example.com"})

    async def drive() -> None:
        await _assert_identical(arms, template)
        for arm in arms:
            await run(arm.app, template, args.warmup)
        for index in range(args.rounds):
            for arm in _ordered(arms, index):
                start = time.perf_counter()
                await run(arm.app, template, args.iterations)
                arm.samples.append((time.perf_counter() - start) / args.iterations * 1e6)
        await _assert_identical(arms, template)

    asyncio.run(drive())
    print(f"python {platform.python_version()} on {platform.platform()}")
    print(f"rounds={args.rounds} iterations={args.iterations}\n")
    payload = report(arms, "full: 7 of 7 (baseline)", "A/A control")

    medians = {arm.label: arm.median for arm in arms}
    captured = medians["full: 7 of 7 (baseline)"] - medians["compartments: 2 of 7"]
    available = medians["full: 7 of 7 (baseline)"] - medians["ceiling: 2 registered"]
    share = captured / available * 100 if available else 0.0
    print(f"\n  ceiling available        {available:+7.2f}us")
    print(f"  compartments captured    {captured:+7.2f}us  ({share:.0f}% of it)")
    print(f"  mechanism overhead       {available - captured:+7.2f}us")

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
