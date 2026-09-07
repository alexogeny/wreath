from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from time import process_time_ns

_SELECT = 'SELECT id, value FROM "widget" WHERE id = $1'
_UPDATE = 'UPDATE "widget" SET value = $1 WHERE id = $2'
_QUOTATIONS = 'SELECT id, message FROM "quotation" ORDER BY id'
SCENARIOS = (
    "plaintext",
    "small-json",
    "point-read",
    "fan-out-8",
    "template",
    "snapshot",
    "transaction",
)


def expected(name):
    bodies = {
        "plaintext": b"Hello, World!",
        "small-json": b'{"message":"Hello, World!"}',
        "point-read": b'{"id":1,"value":100}',
        "fan-out-8": b"[" + b",".join([b'{"id":1,"value":100}'] * 8) + b"]",
        "template": (
            b"<table>\n<tr><td>0</td><td>Additional &lt;fortune&gt; &amp; "
            b"&#34;quote&#34;</td></tr>\n"
            b"<tr><td>1</td><td>alpha</td></tr>\n<tr><td>2</td><td>beta</td></tr>\n</table>"
        ),
        "snapshot": b'{"key":"greeting","value":"Hello, World!"}',
        "transaction": b'{"updated":2,"read":2}',
    }
    content_type = {
        "plaintext": b"text/plain; charset=utf-8",
        "template": b"text/html; charset=utf-8",
    }.get(name, b"application/json")
    body = bodies[name]
    headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode())]
    sql = {
        "point-read": [_SELECT],
        "fan-out-8": [_SELECT] * 8,
        "template": [_QUOTATIONS],
        "transaction": ["BEGIN", _SELECT, _SELECT, _UPDATE, _UPDATE, "COMMIT"],
    }.get(name, [])
    return body, headers, sql


def verify_responses(name, responses, count):
    body, headers, _ = expected(name)
    if len(responses) != count or count < 1:
        raise ValueError("completed response count differs from requested work")
    for response in responses:
        if response is None or (response.status, response.headers, response.body) != (
            200,
            headers,
            body,
        ):
            raise ValueError(f"{name} response differs from independent wire oracle")


def verify(name, responses, sql, flights, count):
    verify_responses(name, responses, count)
    _, _, operations = expected(name)
    if sql != operations * count:
        raise ValueError(f"{name} SQL operations differ from expected count/order")
    if len(flights) != len(sql) or any(
        not flight or flight[-1] != b"S" or flight.count(b"S") != 1 for flight in flights
    ):
        raise ValueError("expected exactly one completed Sync-delimited flight per SQL operation")
    digest = hashlib.sha256()
    for response in responses:
        digest.update(repr((response.status, response.headers, response.body)).encode())
    digest.update(json.dumps(sql, separators=(",", ":")).encode())
    return {
        "scenario": name,
        "responses": count,
        "sql_operations": len(sql),
        "sha256": digest.hexdigest(),
    }


def module_paths(*modules):
    return [Path(module.__file__).resolve() for module in modules]


async def run_case(name, count, warmup, source):
    from benchmarks.workloads import app as workloads_app
    from benchmarks.workloads._fakepg import FakePostgres
    from benchmarks.workloads.bench import SHAPES
    from wreath import app, testing
    from wreath._native import _core, _postgres

    if name not in SCENARIOS or count < 1 or warmup < 2:
        raise ValueError(
            "known scenario, positive count and at least two warmup operations required"
        )
    paths = module_paths(app, testing, _core, _postgres)
    if any(not path.is_relative_to(source) for path in paths):
        raise ValueError("workload imported a module outside the selected source root")
    shapes = {shape[0]: shape[1:] for shape in SHAPES}
    shapes["transaction"] = (
        "POST",
        "/widgets/update?queries=2",
        {
            "json": {"updates": [{"id": 1, "value": 11}, {"id": 2, "value": 22}]},
        },
    )
    method, target, kwargs = shapes[name]
    server = FakePostgres()
    dsn = await server.start()
    try:
        async with testing.TestClient(workloads_app.build_app(dsn)) as client:
            for _ in range(warmup):
                response = await client.request(method, target, **kwargs)
                verify_responses(name, [response], 1)
            server.executed_sql.clear()
            server.flights.clear()
            responses = []
            started = process_time_ns()
            for _ in range(count):
                responses.append(await client.request(method, target, **kwargs))
            elapsed = process_time_ns() - started
            output = verify(name, responses, server.executed_sql, server.flights, count)
    finally:
        await server.close()
    return elapsed, output, paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--warmup", type=int, default=10)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(source))
    os.environ["WREATH_HARDENING"] = "off"
    elapsed, output, paths = asyncio.run(run_case(args.scenario, args.count, args.warmup, source))
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns": elapsed,
                "artifacts": [
                    {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in paths
                ],
                "scope": (
                    "ASGI TestClient plus same-process loopback fake PostgreSQL CPU; "
                    "verification after sample"
                ),
            }
        )
        + "\n"
    )
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
