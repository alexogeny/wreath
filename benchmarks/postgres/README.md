# PostgreSQL driver benchmarks

This package is reserved for reproducible Wreath PostgreSQL driver benchmarks.
Add workloads alongside the implementation slice they measure; do not add a
synthetic benchmark before the corresponding correctness and parity tests.

Every result must record PostgreSQL version, Python version, backend
(`native` or `pure`), platform, connection/pool configuration, concurrency,
warmup, repetitions, errors, throughput, median, p95, and p99 latency.

Run the automatic-pipeline comparison against a disposable PostgreSQL server:

```bash
uv sync --group benchmark
uv run --with asyncpg --with 'psycopg[binary]' --with psycopg2-binary \
  python -m benchmarks.postgres.bench_pipeline \
  --dsn postgresql://neo:secret@127.0.0.1:55433/neo \
  --latency-ms 20 --require-win
```

The benchmark uses a local TCP relay to add the recorded round-trip latency.
Wreath submits 32 cached operations concurrently on one connection; asyncpg runs
the same 32 operations sequentially on one connection. Raw trial durations are
included in the JSON output.

Run the extension-owned receive-buffer benchmark with a retained Slice 2
allocation baseline:

```bash
uv run python -m benchmarks.postgres.bench_receive \
  --dsn postgresql://neo:secret@127.0.0.1:55433/neo \
  --iterations 10000 --slice2-allocations-per-query 4 \
  --require-improvement
```

It records slab growth after warmup, pure/native latency, peak traced memory,
and the exact baseline used for the allocation comparison.

Run the three-column multi-row decode comparison:

```bash
uv run --with asyncpg --with 'psycopg[binary]' --with psycopg2-binary \
  python -m benchmarks.postgres.bench_decode \
  --dsn postgresql://neo:secret@127.0.0.1:55433/neo \
  --rows 10000 --require-improvement
```

This compares Wreath's Slice 3 scalar decoder, Slice 4 batch decoder, asyncpg,
psycopg3, and psycopg2 while retaining every raw trial duration. The pipeline
benchmark uses the same three competitors and labels each sequential baseline.

Run the persistent read/write workload comparison against a disposable
PostgreSQL server:

```bash
uv run --with asyncpg --with 'psycopg[binary]' --with psycopg2-binary \
  python -m benchmarks.postgres.bench_workload \
  --dsn postgresql://neo:secret@127.0.0.1:55433/neo
```

It seeds a five-column table (int4/bool/float8/text/bytea), then measures
single-row inserts and updates, point selects, 100-row range reads, full-table
bulk reads, and a 32-deep concurrent point-select batch for Wreath, asyncpg,
psycopg3, and psycopg2. Wreath pipelines the batch scenario on one connection;
competitors run it sequentially on one connection and the metadata labels
this. `synchronous_commit` is set to `off` for every driver so write scenarios
compare driver and server CPU rather than host-disk fsync latency. Each run
appends `<UTC-timestamp>.json` and refreshes `latest.json` under
`benchmark-results-postgres/` (mirroring the web framework harness), and
`--require-win` exits non-zero if Wreath loses any scenario median.

Run the business-rule validation comparison against hand-written Python and
pydantic (needs no database):

```bash
uv sync --group benchmark --inexact
uv run python -m benchmarks.postgres.bench_orm_constraints
```

It validates a body against an `Intern` — an `Employee` narrowed to a 50,000
salary cap and an 8-month tenure cap, plus a rule spanning the two — and
measures both the accepting and the rejecting path. pydantic is a benchmark
dependency and never a dependency of `src/wreath`.

Two things keep the comparison honest, and both are enforced rather than
documented. Pydantic is configured `strict=True` and `extra="forbid"`, because
wreath's column types never coerce `"5"` to `5` and wreath rejects unknown fields;
lax pydantic would be solving an easier problem. And the rejecting path
materializes each contender's error list, because pydantic raises from Rust and
builds its errors only when `.errors()` is called — timing the raise alone
credits it for work a 422 still has to do. `_agree()` refuses to report numbers
unless every contender accepts the same body and rejects it for the same two
fields.

## `bench_orm_flush.py` — unit-of-work bookkeeping

```bash
uv run python -m benchmarks.postgres.bench_orm_flush --trials 9
uv run python -m benchmarks.postgres.bench_orm_flush --legacy --trials 9
```

Needs no database. It drives `Session.add()`, the flush ordering step, and
`Session.delete()` against the fake-database seam from `tests/orm/conftest.py`,
so what it times is scheduling, membership, and ordering rather than statement
building or a round trip. Sizes are 1,000 to 10,000 pending objects, with the
two models interleaved so ordering has real work to do.

Each phase is reported separately, with a `vs-previous` ratio: the shape is the
result, not the absolute time. Linear bookkeeping holds `per-object` flat and
roughly doubles between adjacent sizes; the quadratic version quadrupled.

`--legacy` runs a reconstruction of the pre-remediation implementation (the
`in self._new` scan, the `_new.index()` sort key, and the per-key `specs` scan)
through the same harness. It is a reconstruction, not a checkout: use it to
compare the two shapes, not as a historical record.

The `probes` counter is the load-bearing check, because it does not depend on
the machine being idle: `_count_probes()` records every identity membership
probe and model-order lookup, and `per-object` must stay constant.
