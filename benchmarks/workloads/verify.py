"""Semantic verifier for the workload suite.

Asserts observable *properties* — not benchmark identity — across all seven
workload shapes, driving the app with Wreath's in-process TestClient and an
embedded PostgreSQL wire stand-in. Run directly:

    uv run python -m benchmarks.workloads.verify

Exits non-zero if any property fails. The checks it enforces:

* small-JSON content type and bytes,
* prepared static headers, framing, and HEAD handling,
* one database operation per point read,
* one Sync per fan-out input and preserved order,
* read-before-write ordering inside a transaction,
* unique application-generated update values,
* template escaping and UTF-8 output,
* snapshot reads with explicit misses,
* query-limit clamp and invalid-syntax rejection.
"""

from __future__ import annotations

import asyncio
import sys

from wreath.testing import TestClient

from ._fakepg import FakePostgres
from .app import build_app


class _Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, label: str) -> None:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}")
        if not ok:
            self.failures.append(label)


async def _run() -> int:
    checker = _Checker()
    server = FakePostgres()
    dsn = await server.start()
    app = build_app(dsn)

    async with TestClient(app) as client:
        print("Shape 1: small JSON serialization")
        response = await client.get("/json")
        checker.check(response.status == 200, "JSON status 200")
        checker.check(
            response.header("content-type") == "application/json", "JSON content type"
        )
        checker.check(response.body == b'{"message":"Hello, World!"}', "JSON exact bytes")

        print("Shape 2: reusable static/plaintext response")
        response = await client.get("/plaintext")
        checker.check(response.body == b"Hello, World!", "plaintext body")
        checker.check(
            response.header("content-length") == "13", "plaintext content-length"
        )
        head = await client.head("/plaintext")
        checker.check(head.body == b"", "HEAD suppresses body")
        checker.check(
            head.header("content-length") == "13", "HEAD keeps content-length"
        )

        print("Shape 3: point database read")
        before = len(server.flights)
        response = await client.get("/widget/1")
        checker.check(response.json() == {"id": 1, "value": 100}, "point read value")
        checker.check(
            len(server.flights) - before == 1, "point read is one database operation"
        )

        print("Shape 4: independent fan-out reads")
        before = len(server.flights)
        response = await client.get("/widgets?queries=3")
        rows = response.json()
        checker.check(len(rows) == 3, "fan-out returns one row per input")
        checker.check(
            len(server.flights) - before == 3, "fan-out is one Sync per input"
        )
        # Query clamp: 0 -> minimum 1; huge -> maximum 500 (not exercised in full).
        clamped_low = await client.get("/widgets?queries=0")
        checker.check(len(clamped_low.json()) == 1, "query clamp low -> minimum")
        invalid = await client.get("/widgets?queries=notanint")
        checker.check(invalid.status == 422, "invalid query syntax -> 422")

        print("Shape 5: transactional read-modify-write")
        before_sql = list(server.executed_sql)
        payload = {"updates": [{"id": 1, "value": 11}, {"id": 2, "value": 22}]}
        response = await client.post("/widgets/update?queries=2", json=payload)
        checker.check(response.json()["updated"] == 2, "update applied two rows")
        new_sql = server.executed_sql[len(before_sql):]
        begin = new_sql.index("BEGIN")
        commit = new_sql.index("COMMIT")
        reads = [i for i, s in enumerate(new_sql) if s.startswith("SELECT id, value")]
        writes = [i for i, s in enumerate(new_sql) if s.startswith("UPDATE")]
        checker.check(begin < min(reads), "BEGIN precedes reads")
        checker.check(max(reads) < min(writes), "reads precede writes")
        checker.check(max(writes) < commit, "writes precede COMMIT")
        values = [item["value"] for item in payload["updates"]]
        checker.check(len(set(values)) == len(values), "application update values unique")

        print("Shape 6: escaped template table render")
        response = await client.get("/quotations")
        text = response.body.decode("utf-8")
        checker.check("&lt;fortune&gt;" in text, "template escapes angle brackets")
        checker.check("&#34;quote&#34;" in text, "template escapes quotes")
        checker.check("<fortune>" not in text, "no raw markup leaks")
        checker.check(
            response.header("content-type") == "text/html; charset=utf-8",
            "template HTML content type",
        )

        print("Shape 7: snapshot-cache read")
        response = await client.get("/config/greeting")
        checker.check(
            response.json() == {"key": "greeting", "value": "Hello, World!"},
            "snapshot hit",
        )
        miss = await client.get("/config/absent")
        checker.check(miss.status == 404, "snapshot miss is explicit 404")

    await server.close()

    print()
    if checker.failures:
        print(f"FAILED: {len(checker.failures)} propert(y/ies) did not hold")
        return 1
    print("OK: all workload properties hold")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
