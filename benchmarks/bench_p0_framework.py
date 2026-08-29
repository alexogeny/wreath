"""Price the P0 framework foundations without hiding results below noise.

The ``static`` suite uses only Wreath's pre-existing API so it can run unchanged
against an archived pre-change tree. The remaining suites report absolute cost
for the new contracts: constrained request/response validation, host and greedy
routing, ASGI mounts, incremental ingress, rich schema validation, reverse
routing, and typed settings construction.

Every suite interleaves its arms and places an A/A control last. A delta below
twice that measured floor is unresolved, not zero. These are framework
microbenchmarks, not server throughput results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from wreath import Response, Wreath
from wreath._devtools import measure


class _FixedApp:
    """Run an app with a fixed scope and fresh body channel per request."""

    def __init__(
        self,
        app: Any,
        template: dict[str, Any],
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.app = app
        self.template = template
        self.chunks = chunks

    async def __call__(self, _scope: Any, _receive: Any, send: Any) -> None:
        index = 0

        async def receive() -> dict[str, Any]:
            nonlocal index
            if index >= len(self.chunks):
                return {"type": "http.request", "body": b"", "more_body": False}
            body = self.chunks[index]
            index += 1
            return {
                "type": "http.request",
                "body": body,
                "more_body": index < len(self.chunks),
            }

        await self.app(dict(self.template), receive, send)


def _static_app() -> Wreath:
    app = Wreath()
    response = Response(b"ok")

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        return response

    return app


def _static_arms() -> list[measure.Arm]:
    return [
        measure.Arm("static route", _static_app()),
        measure.Arm("control static route", _static_app()),
    ]


def _dispatch_arms() -> list[measure.Arm]:
    from typing import Annotated

    from wreath.binding import Query

    static_scope = measure.scope()

    @dataclass
    class PublicItem:
        item_id: int
        name: str

    contracted = Wreath()

    @contracted.get("/")
    async def contract(request: Any, limit: int = 5) -> Any:
        return cast(
            Any,
            {"item_id": limit, "name": "wreath", "secret": "filtered"},
        )

    contract.__annotations__["limit"] = Annotated[int, Query(minimum=1, maximum=10)]
    contract.__annotations__["return"] = PublicItem

    host = Wreath()

    @host.get("/")
    async def host_default(request: Any) -> Response:
        return Response(b"ok")

    @host.get("/", host="{tenant}.example.test")
    async def host_scoped(request: Any, tenant: str) -> Response:
        return Response(b"ok")

    greedy = Wreath()

    @greedy.get("/assets/{asset_path:path}")
    async def greedy_route(request: Any, asset_path: str) -> Response:
        return Response(b"ok")

    async def child(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mounted = Wreath()
    mounted.mount("/service", child)

    return [
        measure.Arm("static route", _FixedApp(_static_app(), static_scope)),
        measure.Arm(
            "query + response contract",
            _FixedApp(contracted, measure.scope(path="/?limit=5")),
        ),
        measure.Arm(
            "host route",
            _FixedApp(
                host,
                measure.scope(headers={"host": "acme.example.test"}),
            ),
        ),
        measure.Arm(
            "trailing path route",
            _FixedApp(greedy, measure.scope(path="/assets/css/site.css")),
        ),
        measure.Arm(
            "mounted ASGI child",
            _FixedApp(mounted, measure.scope(path="/service/health")),
        ),
        measure.Arm("control static route", _FixedApp(_static_app(), static_scope)),
    ]


def _ingress_arms() -> list[measure.Arm]:
    payload = b"x" * 4096
    chunks = tuple(payload[index : index + 256] for index in range(0, len(payload), 256))

    buffered = Wreath()

    @buffered.post("/")
    async def buffer_body(request: Any) -> Response:
        body = await request.body()
        return Response(str(len(body)).encode())

    streamed = Wreath()

    @streamed.post("/")
    async def stream_body(request: Any) -> Response:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
        return Response(str(size).encode())

    boundary = b"WREATH-P0"
    multipart_body = (
        b"--"
        + boundary
        + b'\r\nContent-Disposition: form-data; name="name"\r\n\r\n'
        + payload
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    multipart_chunks = tuple(
        multipart_body[index : index + 256] for index in range(0, len(multipart_body), 256)
    )
    multipart = Wreath()

    @multipart.post("/")
    async def parse_form(request: Any) -> Response:
        form = await request.form()
        size = len(form["name"])
        form.close()
        return Response(str(size).encode())

    body_scope = measure.scope(method="POST")
    form_scope = measure.scope(
        method="POST",
        headers={
            "host": "example.test",
            "content-type": f"multipart/form-data; boundary={boundary.decode()}",
        },
    )
    return [
        measure.Arm("buffer 4 KiB / 16 chunks", _FixedApp(buffered, body_scope, chunks)),
        measure.Arm("stream 4 KiB / 16 chunks", _FixedApp(streamed, body_scope, chunks)),
        measure.Arm(
            "multipart 4 KiB / 17 chunks",
            _FixedApp(multipart, form_scope, multipart_chunks),
        ),
        measure.Arm("control buffered body", _FixedApp(buffered, body_scope, chunks)),
    ]


def _validation_arms() -> list[measure.Arm]:
    import datetime as dt
    import enum
    from decimal import Decimal
    from typing import Annotated, Literal
    from uuid import UUID

    from wreath.binding import Field, validate

    @dataclass
    class Simple:
        name: str
        count: int

    class State(enum.StrEnum):
        ACTIVE = "active"

    @dataclass
    class Rich:
        item_id: UUID
        amount: Decimal
        state: State
        kind: Literal["sale"]
        due: dt.date
        display_name: Annotated[
            str,
            Field(alias="displayName", min_length=3, max_length=12, pattern=r"^[A-Z]"),
        ]
        rating: Annotated[int, Field(ge=1, le=5)]
        tags: Annotated[set[str], Field(min_length=1, max_length=3)]

    Rich.__annotations__ = {
        "item_id": UUID,
        "amount": Decimal,
        "state": State,
        "kind": Literal["sale"],
        "due": dt.date,
        "display_name": Annotated[
            str,
            Field(alias="displayName", min_length=3, max_length=12, pattern=r"^[A-Z]"),
        ],
        "rating": Annotated[int, Field(ge=1, le=5)],
        "tags": Annotated[set[str], Field(min_length=1, max_length=3)],
    }

    simple_value = {"name": "wreath", "count": 3}
    rich_value = {
        "item_id": "cbfb7892-bbe8-4d26-9c5d-e12d17f404e2",
        "amount": "12.340",
        "state": "active",
        "kind": "sale",
        "due": "2026-08-01",
        "displayName": "Wreath",
        "rating": 5,
        "tags": ["python", "asgi"],
    }
    sink: Any = None

    def simple(iterations: int) -> None:
        nonlocal sink
        for _ in range(iterations):
            sink = validate(Simple, simple_value)

    def rich(iterations: int) -> None:
        nonlocal sink
        for _ in range(iterations):
            sink = validate(Rich, rich_value)

    return [
        measure.Arm("simple dataclass validation", payload=simple),
        measure.Arm("rich constrained validation", payload=rich),
        measure.Arm("control simple validation", payload=simple),
    ]


def _settings_arms() -> list[measure.Arm]:
    from wreath.config import Environment, Secret

    @dataclass(frozen=True)
    class DatabaseSettings:
        host: str
        port: int
        password: Secret[str]

    @dataclass(frozen=True)
    class Settings:
        debug: bool
        workers: int
        database: DatabaseSettings

    DatabaseSettings.__annotations__ = {
        "host": str,
        "port": int,
        "password": Secret[str],
    }
    Settings.__annotations__ = {
        "debug": bool,
        "workers": int,
        "database": DatabaseSettings,
    }

    environment = Environment(
        {
            "APP_DEBUG": "true",
            "APP_WORKERS": "4",
            "APP_DATABASE__HOST": "db.internal",
            "APP_DATABASE__PORT": "5432",
            "APP_DATABASE__PASSWORD": "secret",
        }
    )
    sink: Any = None

    def direct(iterations: int) -> None:
        nonlocal sink
        for _ in range(iterations):
            sink = Settings(True, 4, DatabaseSettings("db.internal", 5432, Secret("secret")))

    def bind(iterations: int) -> None:
        nonlocal sink
        for _ in range(iterations):
            sink = environment.bind(Settings, prefix="APP")

    return [
        measure.Arm("direct dataclass construction", payload=direct),
        measure.Arm("typed Environment.bind", payload=bind),
        measure.Arm("control direct construction", payload=direct),
    ]


def _reverse_arms() -> list[measure.Arm]:
    app = Wreath()

    @app.get("/assets/{asset_path:path}", name="asset")
    async def asset(request: Any, asset_path: str) -> Response:
        return Response(b"ok")

    sink = ""

    def literal(iterations: int) -> None:
        nonlocal sink
        for _ in range(iterations):
            sink = "/assets/" + "css/site.css"

    def reverse(iterations: int) -> None:
        nonlocal sink
        for _ in range(iterations):
            sink = app.url_path_for("asset", asset_path="css/site.css")

    return [
        measure.Arm("literal path construction", payload=literal),
        measure.Arm("named reverse lookup", payload=reverse),
        measure.Arm("control literal construction", payload=literal),
    ]


def _run_apps(
    arms: list[measure.Arm], args: argparse.Namespace, baseline: str, control: str
) -> dict[str, Any]:
    asyncio.run(
        measure.measure_apps(
            arms,
            measure.scope(),
            rounds=args.rounds,
            iterations=args.iterations,
            warmup=args.warmup,
        )
    )
    return measure.report(arms, baseline, control)


def _run_calls(
    arms: list[measure.Arm], args: argparse.Namespace, baseline: str, control: str
) -> dict[str, Any]:
    measure.measure_callables(
        arms,
        rounds=args.rounds,
        iterations=max(args.iterations, 20_000),
        warmup=args.warmup,
    )
    return measure.report(arms, baseline, control)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("static", "dispatch", "ingress", "validation", "settings", "reverse", "all"),
        default="all",
    )
    parser.add_argument("--rounds", type=int, default=measure.DEFAULT_ROUNDS)
    parser.add_argument("--iterations", type=int, default=measure.DEFAULT_ITERATIONS)
    parser.add_argument("--warmup", type=int, default=measure.DEFAULT_WARMUP)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results: dict[str, Any] = {}
    measured: dict[str, list[measure.Arm]] = {}

    if args.suite in ("static", "all"):
        arms = _static_arms()
        results["static"] = _run_apps(arms, args, "static route", "control static route")
        measured["static"] = arms
    if args.suite in ("dispatch", "all"):
        arms = _dispatch_arms()
        results["dispatch"] = _run_apps(arms, args, "static route", "control static route")
        measured["dispatch"] = arms
    if args.suite in ("ingress", "all"):
        arms = _ingress_arms()
        results["ingress"] = _run_apps(
            arms, args, "buffer 4 KiB / 16 chunks", "control buffered body"
        )
        measured["ingress"] = arms
    if args.suite in ("validation", "all"):
        arms = _validation_arms()
        results["validation"] = _run_calls(
            arms, args, "simple dataclass validation", "control simple validation"
        )
        measured["validation"] = arms
    if args.suite in ("settings", "all"):
        arms = _settings_arms()
        results["settings"] = _run_calls(
            arms, args, "direct dataclass construction", "control direct construction"
        )
        measured["settings"] = arms
    if args.suite in ("reverse", "all"):
        arms = _reverse_arms()
        results["reverse"] = _run_calls(
            arms, args, "literal path construction", "control literal construction"
        )
        measured["reverse"] = arms

    document = {
        "metadata": {
            "command": " ".join(sys.argv),
            "python": sys.version,
            "platform": platform.platform(),
            "rounds": args.rounds,
            "iterations": args.iterations,
            "warmup": args.warmup,
        },
        "results": results,
        "samples_us": {
            suite: {arm.label: [round(sample, 6) for sample in arm.samples] for arm in arms}
            for suite, arms in measured.items()
        },
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
