"""Price four proposed optimization pathways before any of them is built.

Each arm is the *ceiling* for one proposal -- what it would buy if it worked
perfectly and cost nothing -- measured by removing the work rather than by
writing the optimization. A ceiling that does not clear the noise floor is a
proposal not worth building; a ceiling that clears it by a wide margin is worth
the design cost, and the gap between them is the thing this file exists to
settle.

    myelination      fuse a hot route's wrapper chain into one closure
    reflex           answer a miss without the observing middleware
    compartments     run only the middleware a route actually needs
    ontogeny         (control) the response-validator compilation already shipped

`immune memory` -- persisting compiled artifacts so a cold worker starts warm --
is not here, because it is a startup cost rather than a per-request one and is
measured separately by `--startup`.

Arms are interleaved with an A/A control at the far end of each round, through
the shared harness, because on a powersave governor the first arm of a pair
runs at a lower clock than the second and an unmatched comparison is fiction.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from wreath import Response, Wreath
from wreath._devtools.measure import Arm, _ordered, report, run, scope
from wreath._devtools.sample_app import MIDDLEWARE_FACTORIES, build_realistic_app
from wreath._json import dumps as _dumps
from wreath.binding import _compile_jsonable, _compile_response_check

_BODY = Response(b'{"ok":1}', media_type=b"application/json")


def _tape_app(count: int) -> Wreath:
    """The realistic global stack, truncated to its first `count` middlewares.

    Truncation stands in for per-route compartmentalization: a route that needs
    two of seven pays for two. Which two is a policy question; what it saves is
    not, and that is what this measures.
    """
    app = Wreath()
    for factory in MIDDLEWARE_FACTORIES[:count]:
        app.add_middleware(factory())

    @app.get("/i/{x}")
    async def item(request: Any) -> Response:
        return _BODY

    app._compile_routes()
    return app


def _observer_split_app() -> Wreath:
    """Deciders only: the stack with every purely-observing hook removed.

    A reflex answers before the signal reaches the brain, and the signal still
    arrives afterwards. The middleware that only decorates a response or counts
    it -- request id, timing, security headers -- can run after the bytes are
    out; the middleware that can *refuse* the request cannot. This arm is the
    former deleted rather than deferred, which is the ceiling of deferring it.
    """
    app = Wreath()
    for factory in MIDDLEWARE_FACTORIES:
        middleware = factory()
        # A decider is anything that can answer instead of the handler. On this
        # protocol that is exactly a middleware exposing a `before` hook.
        if getattr(middleware, "before_sync", None) or getattr(middleware, "before", None):
            app.add_middleware(middleware)

    @app.get("/i/{x}")
    async def item(request: Any) -> Response:
        return _BODY

    app._compile_routes()
    return app


def _layered_app() -> Wreath:
    """A route wearing the wrapper chain an annotated, parametrized route gets."""
    app = Wreath()

    @app.get("/i/{x}")
    async def item(request: Any, x: int) -> dict[str, Any]:
        return {"id": x, "ok": True}

    app._compile_routes()
    return app


def _fused_app() -> Wreath:
    """The same work with the chain collapsed, as a hot-route recompile would.

    It binds the same parameter and returns the same body; what it does not do
    is cross a binder frame and a response-validator frame to get there. That
    difference is the whole of what myelination buys.
    """
    app = Wreath()
    # The same validation the layered arm performs, called directly. An earlier
    # version of this arm returned a hand-built body and skipped validation
    # entirely, which measured "do less work" and reported it as fusion -- 3.60us
    # against the 1.05us fusion actually buys.
    check = _compile_response_check(dict[str, Any])
    to_json = _compile_jsonable(dict[str, Any])

    @app.get("/i/{x}")
    async def item(request: Any) -> Response:
        value = int(request.path_params["x"])
        body = to_json(check({"id": value, "ok": True}))
        return Response(_dumps(body), media_type=b"application/json")

    app._compile_routes()
    return app


def _annotated_app() -> Wreath:
    app = Wreath()

    @app.get("/i/{x}")
    async def item(request: Any) -> dict[str, Any]:
        return {"id": request.path_params["x"], "ok": True}

    app._compile_routes()
    return app


def startup_cost() -> None:
    """What a cold worker pays before serving anything -- the immune-memory prize."""
    print("cold-start costs (what persisting compiled artifacts would save)\n")
    samples: list[float] = []
    for _ in range(7):
        app, _headers, _method, _path = build_realistic_app()
        app._dirty = True
        start = time.perf_counter()
        app._compile_routes()
        samples.append((time.perf_counter() - start) * 1e3)
    print(f"  route compilation, realistic app   {statistics.median(samples):7.2f} ms")

    code = "import time;s=time.perf_counter();import wreath;print((time.perf_counter()-s)*1e3)"
    import subprocess

    imports: list[float] = []
    for _ in range(5):
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        if done.returncode == 0:
            imports.append(float(done.stdout.strip()))
    if imports:
        print(f"  `import wreath`, fresh process     {statistics.median(imports):7.2f} ms")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=3000)
    parser.add_argument("--warmup", type=int, default=3000)
    parser.add_argument("--startup", action="store_true", help="cold-start costs only")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.startup:
        startup_cost()
        return 0

    full = len(MIDDLEWARE_FACTORIES)
    arms = [
        Arm("tape: all 7 (baseline)", app=_tape_app(full)),
        Arm("compartments: 2 of 7", app=_tape_app(2)),
        Arm("observers deferred", app=_observer_split_app()),
        Arm("tape: none at all", app=_tape_app(0)),
        # Far end of the round from its twin, deliberately.
        Arm("A/A control", app=_tape_app(full)),
    ]
    template = scope("GET", "/i/42")

    import asyncio

    async def drive() -> None:
        for arm in arms:
            await run(arm.app, template, args.warmup)
        for index in range(args.rounds):
            for arm in _ordered(arms, index):
                start = time.perf_counter()
                await run(arm.app, template, args.iterations)
                arm.samples.append(
                    (time.perf_counter() - start) / args.iterations * 1e6
                )

    asyncio.run(drive())
    print(f"python {platform.python_version()} on {platform.platform()}\n")
    print("── middleware pathways (reflex, compartments) ──\n")
    payload = report(arms, "tape: all 7 (baseline)", "A/A control")

    # The wrapper-chain arms are a separate baseline: no middleware at all, so
    # the chain is the only thing between them.
    chain = [
        Arm("layered: binder + validator", app=_layered_app()),
        Arm("ontogeny: annotation only", app=_annotated_app()),
        Arm("myelination: fused closure", app=_fused_app()),
        Arm("A/A control", app=_layered_app()),
    ]

    async def drive_chain() -> None:
        for arm in chain:
            await run(arm.app, template, args.warmup)
        for index in range(args.rounds):
            for arm in _ordered(chain, index):
                start = time.perf_counter()
                await run(arm.app, template, args.iterations)
                arm.samples.append(
                    (time.perf_counter() - start) / args.iterations * 1e6
                )

    asyncio.run(drive_chain())
    print("\n── wrapper-chain pathways (myelination) ──\n")
    second = report(chain, "layered: binder + validator", "A/A control")

    if args.json is not None:
        args.json.write_text(
            json.dumps({"middleware": payload, "chain": second}, indent=2) + "\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
