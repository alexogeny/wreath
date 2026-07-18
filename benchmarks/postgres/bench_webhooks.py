"""Measure real PostgreSQL webhook inbox/outbox transactions and queue drain."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import resource
import statistics
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath.postgres import connect
from wreath.webhooks import PostgresWebhookInbox, PostgresWebhookOutbox, WebhookEnvelope


async def _apply_schema(connection: Any, schema_sql: str) -> None:
    """Run a multi-statement schema one statement at a time.

    ``schema_sql()`` returns a CREATE TABLE followed by a CREATE INDEX, but
    ``connection.execute`` speaks the extended-query protocol, which rejects more
    than one command per prepared statement ("cannot insert multiple commands
    into a prepared statement"). This schema has no semicolons inside literals or
    bodies, so splitting on ``;`` is safe.
    """
    for statement in schema_sql.split(";"):
        statement = statement.strip()
        if statement:
            await connection.execute(statement)


class Raw:
    def __init__(self, connection: Any, sql: str, args: tuple[Any, ...]) -> None:
        self.connection = connection
        self.sql = sql
        self.args = args

    async def execute(self) -> Any:
        return await self.connection.execute(self.sql, *self.args)

    async def fetchrow(self) -> Any:
        return await self.connection.fetchrow(self.sql, *self.args)

    async def fetchval(self) -> Any:
        return await self.connection.fetchval(self.sql, *self.args)


class Session:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def raw(self, sql: str, *args: Any) -> Raw:
        return Raw(self.connection, sql, args)


def envelope(event_id: str) -> WebhookEnvelope:
    return WebhookEnvelope(
        id=event_id,
        type="benchmark.event",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":1}',
    )


def summary(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "median_ns": statistics.median(samples),
        "p95_ns": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "raw_ns": samples,
    }


async def measure(operation: Callable[[], Awaitable[None]], trials: int) -> list[float]:
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        await operation()
        samples.append(float(perf_counter_ns() - started))
    return samples


async def run(dsn: str, iterations: int, trials: int) -> dict[str, Any]:
    suffix = uuid.uuid4().hex
    inbox = PostgresWebhookInbox(f"bench_webhook_inbox_{suffix}")
    outbox = PostgresWebhookOutbox(f"bench_webhook_outbox_{suffix}")
    connection = await connect(dsn)
    session = Session(connection)
    sequence = 0
    try:
        await _apply_schema(connection, inbox.schema_sql())
        await _apply_schema(connection, outbox.schema_sql())

        async def inbox_transaction() -> None:
            nonlocal sequence
            event = envelope(f"inbox-{sequence}")
            sequence += 1
            await connection.execute("BEGIN")
            try:
                claim = await inbox.claim(
                    session,
                    source="benchmark",
                    envelope=event,
                    lease_owner="benchmark",
                    lease_seconds=30,
                )
                await inbox.complete(
                    session,
                    source="benchmark",
                    message_id=event.id,
                    fencing_token=claim.fencing_token,
                    result_status=204,
                )
            except BaseException:
                await connection.execute("ROLLBACK")
                raise
            await connection.execute("COMMIT")

        async def outbox_transaction() -> None:
            nonlocal sequence
            event = envelope(f"outbox-{sequence}")
            sequence += 1
            await connection.execute("BEGIN")
            try:
                await outbox.enqueue(
                    session,
                    destination="receiver",
                    envelope=event,
                    key_id="benchmark",
                )
            except BaseException:
                await connection.execute("ROLLBACK")
                raise
            await connection.execute("COMMIT")

        for _ in range(iterations):
            await inbox_transaction()
            await outbox_transaction()

        inbox_samples = await measure(inbox_transaction, trials)
        outbox_samples = await measure(outbox_transaction, trials)

        started = perf_counter_ns()
        drained = 0
        while True:
            delivery = await outbox.claim_due(
                session, lease_owner="benchmark", lease_seconds=30
            )
            if delivery is None:
                break
            await outbox.mark_sending(session, delivery)
            await outbox.mark_delivered(session, delivery, status=204)
            drained += 1
        drain_ns = perf_counter_ns() - started

        completed = await connection.fetchval(
            f"SELECT count(*) FROM {inbox.table} WHERE state='completed'"
        )
        delivered = await connection.fetchval(
            f"SELECT count(*) FROM {outbox.table} WHERE state='delivered'"
        )
        if completed != iterations + trials or delivered != iterations + trials:
            raise RuntimeError("PostgreSQL webhook work counts failed integrity validation")
        if drained != delivered:
            raise RuntimeError("dispatcher drain count differs from delivered rows")

        # Same query against the same connection provides a measured A/A floor.
        aa_left = await measure(
            lambda: connection.fetchval("SELECT count(*) FROM pg_catalog.pg_type"),
            trials,
        )
        aa_right = await measure(
            lambda: connection.fetchval("SELECT count(*) FROM pg_catalog.pg_type"),
            trials,
        )
        aa_delta = [abs(a - b) for a, b in zip(aa_left, aa_right, strict=True)]
        return {
            "metadata": {
                "python": sys.version,
                "platform": platform.platform(),
                "iterations": iterations,
                "trials": trials,
                "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "rss_unit": "KiB on Linux, bytes on macOS",
            },
            "aa_noise": {
                "left_raw_ns": aa_left,
                "right_raw_ns": aa_right,
                "absolute_delta_ns": aa_delta,
                "noise_floor_ns": max(aa_delta),
            },
            "results": {
                "inbox_transaction": summary(inbox_samples),
                "outbox_transaction": summary(outbox_samples),
                "dispatcher_drain": {
                    "total_ns": drain_ns,
                    "per_delivery_ns": drain_ns / drained,
                    "deliveries": drained,
                },
            },
            "counts": {
                "authenticated_claimed_completed": completed,
                "committed_intents": iterations + trials,
                "delivered": delivered,
                "failed": 0,
                "unknown": 0,
                "pending": 0,
            },
        }
    finally:
        await connection.execute(f"DROP TABLE IF EXISTS {outbox.table}")
        await connection.execute(f"DROP TABLE IF EXISTS {inbox.table}")
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.iterations, args.trials) <= 0:
        parser.error("iterations and trials must be positive")
    result = asyncio.run(run(args.dsn, args.iterations, args.trials))
    print(json.dumps(result, indent=2))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
