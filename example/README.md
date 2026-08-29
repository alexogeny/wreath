# The camera-trap example

A wildlife camera-trap network for four reserves, built on wreath. This is the
framework's canonical example: **one application that uses the parts together**,
rather than a gallery of snippets that each use one.

It is a complete application rather than a tutorial.

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

The seed writes 141,398 rows in about 13 seconds and is deterministic.

Finally, serve it:

```bash
PYTHONPATH=. wreath run camera_trap.app:app --port 8000

curl -s localhost:8000/species                                  # public
curl -s -c jar.txt -X POST 'localhost:8000/session?email=ranger1@example.org'
curl -s -b jar.txt localhost:8000/reserves                      # needs a session
```

The observations are not public — only the species vocabulary is — so the
interesting routes want a session.

## What is here so far

Stages one to three of eight: the schema and the data, the read API over them,
and the authorization that decides who sees what.

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

Later stages add object storage and uploads, the analysis layer with its charts,
a second chapter that recodes `review_state` with a deferred migration, and an
operations appendix.

## The application runs the framework default

`build()` takes `validate_schema="error"` — the framework default — so the
application reads the PostgreSQL catalog at startup and refuses to serve against
a schema that does not match its models. The example's own tests build it that
way too, rather than turning the check off, because a default that the canonical
example does not exercise is a default nobody is checking.

That was not always possible. The catalog read used to hang at lifespan startup,
and once that was fixed it reported every foreign key missing on a correct
schema. Both are fixed, and running the default here is what would catch a
third.
