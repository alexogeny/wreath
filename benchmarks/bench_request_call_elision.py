"""Ablate avoidable Python work in the metal request path.

The request arm measures only the isolated upper bound of collapsing Wreath's native
``_RequestContext`` and Python ``Request`` wrapper: the baseline allocates and
touches the wrapper exactly as dispatch does, while the ablation reads the same
six fields directly from a real context produced by the HTTP/1 parser. It does
not establish an end-to-end win: any production layout still needs a whole
native-server A/B measurement.

The egress arm runs whole ASGI requests through ten no-op global after hooks.
Both applications have the same handler and response; only coroutine/await
versus ``after_sync`` dispatch differs.

The response arm prices the ``response_only=True`` route contract. Both arms
run one route middleware hook and return the same response; the specialized arm
omits the otherwise necessary coercion wrapper around the handler.

The frozen arm prices a stronger startup contract: a route whose complete
``PreparedResponse`` is immutable.  The control still performs ordinary route
activation, while the frozen route goes from matching directly to emission.

Arms are interleaved and carry an A/A control through Wreath's shared
measurement harness. A delta below twice that measured floor is unresolved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Any

from wreath import Response, Wreath
from wreath._devtools import measure
from wreath.middleware import MiddlewareHooks
from wreath.request import Request
from wreath.response import PreparedResponse
from wreath.server import ServerConfig


class _Transport(asyncio.Transport):
    def write(self, data: Any) -> None:
        return None

    def writelines(self, data: Any) -> None:
        return None

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "sockname":
            return ("127.0.0.1", 8000)
        if name == "peername":
            return ("127.0.0.1", 50000)
        return default


class _CaptureContext:
    context: Any = None

    async def _wreath_http(self, context: Any, receive: Any, send: Any) -> None:
        self.context = context
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    __call__ = _wreath_http


async def _native_context() -> Any:
    from wreath._native._server import HttpProtocol

    app = _CaptureContext()
    protocol = HttpProtocol(app, ServerConfig(), asyncio.get_running_loop(), set())
    protocol.connection_made(_Transport())
    protocol.data_received(
        b"GET /items/42?q=1 HTTP/1.1\r\n"
        b"host: example.test\r\n"
        b"x-example: yes\r\n\r\n"
    )
    if app.context is None:
        raise RuntimeError("native protocol did not activate the capture app")
    context = app.context
    protocol.connection_lost(None)
    return context


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request_arms() -> list[measure.Arm]:
    context = asyncio.run(_native_context())
    sink = 0

    def wrapped(iterations: int) -> None:
        nonlocal sink
        value = 0
        for _ in range(iterations):
            request = Request(context, _receive)
            value += (
                len(request.method)
                + len(request.path)
                + len(request.scheme)
                + len(request.query_string)
                + len(request.headers)
            )
            if request.client is not None:
                value += len(request.client[0])
        sink = value

    def collapsed(iterations: int) -> None:
        nonlocal sink
        value = 0
        for _ in range(iterations):
            value += (
                len(context.method)
                + len(context.path)
                + len(context.scheme)
                + len(context.query_string)
                + len(context.headers)
            )
            if context.client is not None:
                value += len(context.client[0])
        sink = value

    return [
        measure.Arm("wrapped Request", payload=wrapped),
        measure.Arm("direct context upper bound", payload=collapsed),
        measure.Arm("control wrapped", payload=wrapped),
    ]


class _AsyncAfter:
    global_scope = True

    async def after(self, request: Any, response: Any) -> Any:
        return response


class _SyncAfter:
    global_scope = True

    def after_sync(self, request: Any, response: Any) -> Any:
        return response


class _InPlaceAfter:
    global_scope = True

    def after_inplace(self, request: Any, response: Any) -> None:
        return None


def _after_app(*, synchronous: bool, hooks: int) -> Wreath:
    app = Wreath()
    middleware = _SyncAfter if synchronous else _AsyncAfter
    for _ in range(hooks):
        app.add_middleware(middleware())
    response = Response(b"ok")

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        return response

    return app


def _inplace_app(*, inplace: bool, hooks: int) -> Wreath:
    app = Wreath()
    middleware = _InPlaceAfter if inplace else _SyncAfter
    for _ in range(hooks):
        app.add_middleware(middleware())
    response = Response(b"ok")

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        return response

    return app


def _after_arms(hooks: int) -> list[measure.Arm]:
    return [
        measure.Arm(f"{hooks} async after hooks", _after_app(synchronous=False, hooks=hooks)),
        measure.Arm(f"{hooks} sync after hooks", _after_app(synchronous=True, hooks=hooks)),
        measure.Arm("control async", _after_app(synchronous=False, hooks=hooks)),
    ]


def _inplace_arms(hooks: int) -> list[measure.Arm]:
    return [
        measure.Arm(
            f"{hooks} transforming sync after hooks",
            _inplace_app(inplace=False, hooks=hooks),
        ),
        measure.Arm(
            f"{hooks} in-place after hooks",
            _inplace_app(inplace=True, hooks=hooks),
        ),
        measure.Arm(
            "control transforming sync",
            _inplace_app(inplace=False, hooks=hooks),
        ),
    ]


def _response_app(*, response_only: bool) -> Wreath:
    app = Wreath()
    response = Response(b"ok")

    @app.get(
        "/",
        middleware=(MiddlewareHooks(before_sync=lambda request: None),),
        response_only=response_only,
    )
    async def endpoint(request: Any) -> Response:
        return response

    return app


def _response_arms() -> list[measure.Arm]:
    return [
        measure.Arm("ordinary response route", _response_app(response_only=False)),
        measure.Arm("response-only route", _response_app(response_only=True)),
        measure.Arm("control ordinary response", _response_app(response_only=False)),
    ]


def _frozen_app(*, frozen: bool) -> Wreath:
    app = Wreath()
    response = PreparedResponse.text("hello, world")
    if frozen:
        app.frozen("/", response)
    else:

        @app.get("/", response_only=True)
        async def endpoint(request: Any) -> PreparedResponse:
            return response

    return app


def _frozen_arms() -> list[measure.Arm]:
    return [
        measure.Arm("ordinary prepared route", _frozen_app(frozen=False)),
        measure.Arm("frozen response route", _frozen_app(frozen=True)),
        measure.Arm("control ordinary prepared", _frozen_app(frozen=False)),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("request", "after", "inplace", "response", "frozen", "all"),
        default="all",
    )
    parser.add_argument("--hooks", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=measure.DEFAULT_ROUNDS)
    parser.add_argument("--iterations", type=int, default=measure.DEFAULT_ITERATIONS)
    parser.add_argument("--warmup", type=int, default=measure.DEFAULT_WARMUP)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results: dict[str, Any] = {}
    measured_arms: dict[str, list[measure.Arm]] = {}

    if args.suite in ("request", "all"):
        arms = _request_arms()
        measure.measure_callables(
            arms,
            rounds=args.rounds,
            iterations=max(args.iterations, 20_000),
            warmup=args.warmup,
        )
        results["request"] = measure.report(
            arms, "wrapped Request", "control wrapped"
        )
        measured_arms["request"] = arms

    if args.suite in ("after", "all"):
        arms = _after_arms(args.hooks)
        asyncio.run(
            measure.measure_apps(
                arms,
                measure.scope(),
                rounds=args.rounds,
                iterations=args.iterations,
                warmup=args.warmup,
            )
        )
        results["after"] = measure.report(
            arms, f"{args.hooks} async after hooks", "control async"
        )
        measured_arms["after"] = arms

    if args.suite in ("inplace", "all"):
        arms = _inplace_arms(args.hooks)
        asyncio.run(
            measure.measure_apps(
                arms,
                measure.scope(),
                rounds=args.rounds,
                iterations=args.iterations,
                warmup=args.warmup,
            )
        )
        results["inplace"] = measure.report(
            arms,
            f"{args.hooks} transforming sync after hooks",
            "control transforming sync",
        )
        measured_arms["inplace"] = arms

    if args.suite in ("response", "all"):
        arms = _response_arms()
        asyncio.run(
            measure.measure_apps(
                arms,
                measure.scope(),
                rounds=args.rounds,
                iterations=args.iterations,
                warmup=args.warmup,
            )
        )
        results["response"] = measure.report(
            arms, "ordinary response route", "control ordinary response"
        )
        measured_arms["response"] = arms

    if args.suite in ("frozen", "all"):
        arms = _frozen_arms()
        asyncio.run(
            measure.measure_apps(
                arms,
                measure.scope(),
                rounds=args.rounds,
                iterations=args.iterations,
                warmup=args.warmup,
            )
        )
        results["frozen"] = measure.report(
            arms, "ordinary prepared route", "control ordinary prepared"
        )
        measured_arms["frozen"] = arms

    document = {
        "metadata": {
            "command": " ".join(sys.argv),
            "python": sys.version,
            "platform": platform.platform(),
            "rounds": args.rounds,
            "iterations": args.iterations,
            "warmup": args.warmup,
            "hooks": args.hooks,
        },
        "results": results,
        "samples_us": {
            # Preserve the interleaved raw readings. Summary medians alone are
            # not enough to reproduce or audit a claimed sub-microsecond delta.
            suite: {
                arm.label: [round(sample, 6) for sample in arm.samples]
                for arm in suite_arms
            }
            for suite, suite_arms in measured_arms.items()
        },
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
