# The camera-trap example

A wildlife camera-trap network for four reserves, built on wreath. This is the
framework's canonical example: **one application that uses the parts together**,
rather than a gallery of snippets that each use one.

It is not a tutorial. If you have not written a wreath handler yet, start at
`docs/getting-started/`. This is the second thing you read — *I understand the
pieces; show me one that is real.*

## Running it

```bash
docker run -d --name wreath-test-pg \
  -e POSTGRES_PASSWORD=wreath -e POSTGRES_USER=wreath -e POSTGRES_DB=wreath_test \
  -p 55432:5432 postgres:17-alpine

export CAMERA_TRAP_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"
```

Then create the schema and apply the checked-in v1 artifact. The application owns
its namespace — a migration describes tables, not which schema they land in — so
that one statement comes first:

```bash
psql "$CAMERA_TRAP_DSN" -c 'CREATE SCHEMA IF NOT EXISTS camera_trap'

export WREATH_MIGRATION_DSN="$CAMERA_TRAP_DSN"   # apply never reuses request credentials
PYTHONPATH=. wreath migrations apply camera_trap.app:app migrations/migration.bin
```

If you drop the schema and start again, drop its ledger row too — the artifact
declares no parent, and the engine will refuse to replay it onto a history that
already has a tip:

```sql
DELETE FROM wreath_migrations.history WHERE target_schema = 'camera_trap';
```

Then seed it:

```bash
PYTHONPATH=. python -c '
import asyncio, os
from camera_trap.seed import seed
from wreath.postgres import connect

async def main():
    connection = await connect(os.environ["CAMERA_TRAP_DSN"])
    try:
        print(await seed(connection))
    finally:
        await connection.close()

asyncio.run(main())'
```

The seed writes 141,398 rows in about 13 seconds and is deterministic: two
people running it see the same rows and the same ids, which is what lets the
[walkthrough](../docs/example/walkthrough.md) quote real numbers.

Finally, serve it:

```bash
PYTHONPATH=. wreath run camera_trap.app:app --port 8000
curl -s localhost:8000/reserves
```

## What is here so far

Stages one and two of eight: the schema and the data, and the read API over
them.

- `camera_trap/models.py` — the nine tables, each with a docstring saying why it
  exists rather than what it contains
- `camera_trap/config.py` — what is read from the environment before a request
- `camera_trap/queries.py` — the declared named reads
- `camera_trap/routers/` — reserves and their stations, one sighting, the species
  vocabulary
- `camera_trap/wire.py` — the JSON shapes the read API returns
- `camera_trap/app.py` — the application, and the target the CLI loads
- `camera_trap/seed.py` — deterministic seed data
- `migrations/` — the generated v1 artifact

`docs/example/walkthrough.md` tours the schema in `psql`;
`docs/example/read-api.md` walks the nine routes with real transcripts.

Later stages add Cedar authorization, object storage and uploads, the analysis
layer with its charts, and a second chapter that recodes `review_state` with a
deferred migration.

## A known blocker

**`wreath run` cannot start this application yet**, and neither can anything
else that runs its lifespan with schema validation on. The ORM's start-up check
reads the PostgreSQL catalog; the rows arrive in text format while the decoder
that consumes them reads binary only, and it raises inside the connection's
reader task rather than the caller's — so the caller waits on a future nobody
will ever resolve. It is a hang rather than an error, which is the worst shape a
failure can take.

`build(validate_schema="off")` starts and serves normally, and that is what the
example's tests pass. Nothing else about the application is affected: the
migration CLI, the seed, and every route work.
