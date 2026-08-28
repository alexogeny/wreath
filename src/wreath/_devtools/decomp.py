"""Decompose request stages, ORM reads, Python frames, and awaits."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import platform
import statistics
import sys
from pathlib import Path
from typing import Any

from .measure import (
    DEFAULT_ITERATIONS,
    DEFAULT_ROUNDS,
    DEFAULT_WARMUP,
    Arm,
    measure_callables,
    report,
    run_apps,
    scope,
)

REQUEST_HEADERS = {
    "host": "example.com",
    "origin": "https://example.com",
    "authorization": "Bearer tok",
    "user-agent": "wreath-decomp",
    "accept": "*/*",
    "accept-encoding": "gzip, br",
}


def _build_stage_app(*, auth: bool, policy: bool, orm: bool) -> Any:
    from wreath import Wreath
    from wreath._auth.backends import BearerTokenBackend
    from wreath._auth.decorators import authorize, roles
    from wreath._auth.models import Identity
    from wreath.orm.registry import Registry
    from wreath.orm.session import Session
    from wreath.request import Request

    from .sample_app import TracedPost, TracedUser, _Authorizer, _ScriptedDatabase

    database = _ScriptedDatabase()
    database.connection.script(
        "users", [[1, "a@b.c", "A", datetime.datetime(2024, 1, 1)]]
    )
    registry = Registry(database, [TracedUser, TracedPost], validate_schema="off")

    async def verify(token: str) -> Identity | None:
        if token != "tok":
            return None
        return Identity(
            id="u1",
            roles=frozenset({"admin", "staff"}),
            permissions=frozenset({"users:read"}),
        )

    app = Wreath()
    if auth:
        app.configure_auth(BearerTokenBackend(verify), _Authorizer())

    async def handler(request: Request) -> Any:
        if orm:
            session = Session(registry, "read")
            try:
                user = await session.fetch_one(
                    TracedUser.select().where(
                        TracedUser.id == int(request.path_params["user_id"])
                    )
                )
                return {"id": user.id, "email": user.email, "name": user.name}
            finally:
                await session.close()
        return {"id": request.path_params["user_id"], "email": "a@b.c", "name": "A"}

    if auth:
        handler = roles("admin")(handler)
        if policy:
            handler = authorize(action="read", resource=TracedUser)(handler)
    app.route("/users/{user_id}", methods=("GET",))(handler)

    async def sibling(request: Request) -> Any:
        return {"ok": True}

    comparison_paths = (
        "/health",
        "/users",
        "/posts/{post_id}",
        "/orgs/{o}/members/{u}",
    )
    for path in comparison_paths:
        app.route(path, methods=("GET",))(sibling)
    return app


def suite_request(rounds: int, iterations: int, warmup: int) -> dict[str, Any]:
    """What each stage of a request costs, with the middleware tape removed."""
    print("== request stages (no global middleware) ==\n")
    arms = [
        Arm("route only", _build_stage_app(auth=False, policy=False, orm=False)),
        Arm("+ auth (roles)", _build_stage_app(auth=True, policy=False, orm=False)),
        Arm("+ auth + policy", _build_stage_app(auth=True, policy=True, orm=False)),
        Arm("+ auth + policy + ORM read", _build_stage_app(auth=True, policy=True, orm=True)),
        Arm("route only (A/A)", _build_stage_app(auth=False, policy=False, orm=False)),
    ]
    template = scope("GET", "/users/1", REQUEST_HEADERS)
    run_apps(arms, template, rounds, iterations, warmup)
    return report(arms, "route only", "route only (A/A)", cumulative=True)


#: Measured on 10,000 real rows through `benchmarks/postgres/bench_orm_hydrate.py`
#: against PostgreSQL 17: 1,943,738 rows/s direct-native against 530,053 rows/s
#: through Records. Quoted rather than re-measured because this suite has no
#: database; re-derive it there if the hydrators change.
_RECORD_PATH_PENALTY = 3.7


def _hydration_path(database: Any) -> str:
    """Return whether rows are decoded directly or hydrated from Records."""
    connection = getattr(database, "connection", None)
    if getattr(connection, "_decode_dest", None) is not None:
        return "native"
    return "record"


def suite_orm(rounds: int, iterations: int) -> dict[str, Any]:
    """Measure query construction, compilation, and native Record hydration."""
    from wreath.orm.compiler import compile_select, shape_of
    from wreath.orm.registry import Registry
    from wreath.orm.session import Session
    from wreath.queries import Param, Queries, query

    from .sample_app import TracedPost, TracedUser, _ScriptedDatabase

    print("\n== inside one ORM read (scripted database, no I/O) ==\n")
    database = _ScriptedDatabase()
    database.connection.script(
        "users", [[1, "a@b.c", "A", datetime.datetime(2024, 1, 1)]]
    )
    registry = Registry(database, [TracedUser, TracedPost], validate_schema="off")
    prebuilt = TracedUser.select().where(TracedUser.id == 1)

    class PreparedUsers(Queries[TracedUser]):
        by_id = query(TracedUser.id == Param("id")).one()

    def build_query(n: int) -> None:
        for _ in range(n):
            TracedUser.select().where(TracedUser.id == 1)

    def shape_key_only(n: int) -> None:
        for _ in range(n):
            shape_of(registry, prebuilt)

    def compile_prebuilt(n: int) -> None:
        for _ in range(n):
            compile_select(registry, prebuilt)

    def build_and_compile(n: int) -> None:
        for _ in range(n):
            compile_select(registry, TracedUser.select().where(TracedUser.id == 1))

    def session_lifecycle(n: int) -> None:
        async def body() -> None:
            for _ in range(n):
                session = Session(registry, "read")
                await session.close()

        asyncio.run(body())

    def full_read(n: int) -> None:
        async def body() -> None:
            for _ in range(n):
                session = Session(registry, "read")
                try:
                    await session.fetch_one(
                        TracedUser.select().where(TracedUser.id == 1)
                    )
                finally:
                    await session.close()

        asyncio.run(body())

    def prepared_read(n: int) -> None:
        async def body() -> None:
            for _ in range(n):
                session = Session(registry, "read")
                try:
                    await PreparedUsers(session).by_id(id=1)
                finally:
                    await session.close()

        asyncio.run(body())

    arms = [
        Arm("noop", payload=lambda n: [None for _ in range(n)]),
        Arm("Session() + close()", payload=session_lifecycle),
        Arm("build Select+where", payload=build_query),
        Arm("shape_of() cache key", payload=shape_key_only),
        Arm("compile_select (prebuilt)", payload=compile_prebuilt),
        Arm("build + compile_select", payload=build_and_compile),
        Arm("full fetch_one", payload=full_read),
        Arm("prepared declared read", payload=prepared_read),
        Arm("noop (A/A)", payload=lambda n: [None for _ in range(n)]),
    ]
    measure_callables(arms, rounds=rounds, iterations=max(2000, iterations // 2))
    result = report(arms, "noop", "noop (A/A)")

    medians = {arm.label: arm.median for arm in arms}
    full = medians["full fetch_one"]
    prepared = medians["prepared declared read"]
    prep = medians["build Select+where"] + medians["compile_select (prebuilt)"]
    path = _hydration_path(database)
    result["hydration_path"] = path
    print(
        f"\n  Building the query and deriving its cache key is {prep:.2f}us of a "
        f"{full:.2f}us read ({prep / full * 100:.0f}%).\n"
        f"  The compiled SQL is cached; `shape_of` re-derives the key that finds it,\n"
        f"  per request, because the query object is rebuilt per request.\n"
        f"  The existing declared-query path is {prepared:.2f}us: it binds values\n"
        f"  into the retained plan without rebuilding the query or its shape key."
    )
    if path != "native":
        print(
            f"\n  HYDRATION PATH: {path.upper()} -- these arms do NOT measure what a\n"
            f"  deployment runs. A scripted connection installs no `_decode_dest`, so\n"
            f"  `fetch_one` receives Records and batches them through the native Record\n"
            f"  hydrator; a real connection decodes straight into the model's cells.\n"
            f"  Measured on 10,000 rows, the Record path is ~{_RECORD_PATH_PENALTY:.1f}x "
            f"slower, so\n"
            f"  `full fetch_one` above is an upper bound and every percentage taken\n"
            f"  against it -- including the one printed just now -- has an inflated\n"
            f"  denominator. The Record path is still what joined loads and the reference\n"
            f"  driver run, which is what these arms legitimately describe.\n\n"
            f"  For the production path:\n"
            f"    uv run python -m benchmarks.postgres.bench_orm_hydrate \\\n"
            f"      --dsn $WREATH_TEST_POSTGRES_DSN"
        )
    return result


def suite_calibrate(rounds: int, iterations: int) -> dict[str, Any]:
    """Measure constants for converting exact crossing counts to time."""
    from inspect import isawaitable

    print("\n== calibration ==\n")

    async def async_hook(_x: object) -> None:
        return None

    def sync_hook(_x: object) -> None:
        return None

    def drive_await(n: int) -> None:
        async def body() -> None:
            for _ in range(n):
                await async_hook(None)

        asyncio.run(body())

    def drive_sync_guarded(n: int) -> None:
        async def body() -> None:
            for _ in range(n):
                result = sync_hook(None)
                if result is not None and isawaitable(result):
                    await result

        asyncio.run(body())

    arms = [
        Arm("await (never suspends)", payload=drive_await),
        Arm("guarded sync call", payload=drive_sync_guarded),
        Arm("await (A/A)", payload=drive_await),
    ]
    measure_callables(arms, rounds=rounds, iterations=100_000)
    medians = {arm.label: arm.median for arm in arms}
    floor = abs(medians["await (never suspends)"] - medians["await (A/A)"])
    await_cost = (
        medians["await (never suspends)"] - medians["guarded sync call"]
    ) * 1000
    print(f"  await (never suspends)   {medians['await (never suspends)'] * 1000:7.1f} ns")
    print(f"  guarded sync call        {medians['guarded sync call'] * 1000:7.1f} ns")
    print(f"  A/A floor                {floor * 1000:7.1f} ns")
    print(f"\n  a non-suspending await costs {await_cost:.1f} ns more than a sync call")
    print(f"  14 hook calls/request (7 middleware x before+after) -> "
          f"{14 * await_cost / 1000:.2f} us")

    frame_ns = _calibrate_frames(rounds, iterations)
    print(f"\n  slope = {frame_ns:.1f} ns per Python frame")
    print(f"  a fix removing 11 frames is worth ~{11 * frame_ns / 1000:.2f} us -- "
          "usually below\n  a single A/B's floor, and still real. Track those with "
          "`wreath-request-trace`,\n  whose counts are exact, and re-measure time once "
          "several have landed.")
    return {
        "ns_per_await": round(await_cost, 2),
        "ns_per_frame": round(frame_ns, 2),
    }


def _frame_chain(depth: int) -> Any:
    def leaf(value: int) -> int:
        return value + 1

    current = leaf
    for _ in range(depth):
        def step(value: int, _previous: Any = current) -> int:
            return _previous(value)

        current = step
    return current


class _FrameMiddleware:
    global_scope = True

    def __init__(self, depth: int) -> None:
        self.depth = depth
        self._chain = _frame_chain(depth) if depth else None

    async def before(self, request: Any) -> None:
        if self._chain is not None:
            self._chain(0)
        return None


def _calibrate_frames(rounds: int, iterations: int) -> float:
    from .sample_app import build_realistic_app

    depths = (0, 100, 200, 400, 800)
    arms: list[Arm] = []
    for depth in depths:
        app, headers, _method, path = build_realistic_app()
        hook: Any = _FrameMiddleware(depth)
        app._global_middleware.append((99, 99, hook))
        app._dirty = True
        app._compile_routes()
        arms.append(Arm(f"{depth} extra frames", app))
    template = scope("GET", path, headers)
    run_apps(arms, template, rounds, iterations, DEFAULT_WARMUP)

    xs = [float(d) for d in depths]
    ys = [arm.median for arm in arms]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sum(
        (x - mean_x) ** 2 for x in xs
    )
    return slope * 1000


SUITES = ("request", "orm", "calibrate")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-decomp",
        description="Decompose a Wreath request into what each part of it costs.",
    )
    parser.add_argument(
        "--suite",
        choices=(*SUITES, "all"),
        default="all",
        help="request: lifecycle stages. orm: inside one read. calibrate: constants.",
    )
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--json", type=Path, help="write results plus environment metadata")
    args = parser.parse_args(argv)

    selected = SUITES if args.suite == "all" else (args.suite,)
    results: dict[str, Any] = {}
    print(f"rounds={args.rounds} iterations={args.iterations} warmup={args.warmup}\n")
    sys.stdout.flush()

    if "request" in selected:
        results["request"] = suite_request(args.rounds, args.iterations, args.warmup)
    if "orm" in selected:
        results["orm"] = suite_orm(args.rounds, args.iterations)
    if "calibrate" in selected:
        results["calibrate"] = suite_calibrate(args.rounds, args.iterations)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "environment": {
                        "python": sys.version,
                        "implementation": platform.python_implementation(),
                        "platform": platform.platform(),
                        "machine": platform.machine(),
                        "processor": platform.processor(),
                    },
                    "parameters": {
                        "suite": args.suite,
                        "rounds": args.rounds,
                        "iterations": args.iterations,
                        "warmup": args.warmup,
                    },
                    "results": results,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwreath-decomp: wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
