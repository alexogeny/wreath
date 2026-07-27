# Run the camera-trap example

Five steps: a database, a schema, 141,398 rows of seed data, the server, and a
first request. It takes about a minute of typing and a minute of waiting.

Every command on this page was run in the order it appears, and every block of
output is what came back. The seed is deterministic, so your numbers will match
these — if they do not, something is wrong rather than merely different.

You need Python 3.14, a checkout of the repository, and Docker (or Podman, or
nerdctl — the commands are the same).

## 1. A database

```bash
docker run -d --name wreath-test-pg \
  -e POSTGRES_PASSWORD=wreath -e POSTGRES_USER=wreath -e POSTGRES_DB=wreath_test \
  -p 55432:5432 postgres:17-alpine

export CAMERA_TRAP_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"
```

Port 55432 rather than 5432 so this cannot collide with a PostgreSQL you already
run. The example reads `CAMERA_TRAP_DSN` and has **no default** — guessing at
`localhost` and connecting to the wrong database is worse than refusing to
start, so it refuses, and the message names the variable.

## 2. A schema

The application owns its namespace. A migration artifact describes tables, not
which schema they land in, so creating it is a separate statement:

```bash
psql "$CAMERA_TRAP_DSN" -c 'CREATE SCHEMA IF NOT EXISTS camera_trap'
```

No `psql` on your machine? It is inside the container you just started:

```bash
docker exec -i wreath-test-pg psql -U wreath -d wreath_test \
  -c 'CREATE SCHEMA IF NOT EXISTS camera_trap'
```

Then apply the checked-in v1 artifact:

```bash
cd example
export WREATH_MIGRATION_DSN="$CAMERA_TRAP_DSN"   # apply never reuses request credentials
PYTHONPATH=. wreath migrations apply camera_trap.app:app migrations/migration.bin
```

```
applied migration 0000000000000000000000000000c101
  applied: True
  checksum: bee756e60319f983e7324618eaa02beb9c99931e9cef0f6720bcb93ae9b2d4da
  source fingerprint: 59ba344334afa8fdd8ed07d7ddd68bdfb38c37d0710f9bd860051f8cb5f991e4
  target fingerprint: 45e00e91b95227e60e0247e7761b28e7ae4fe2e4618311e76dba9f0125a844b9
  destructive approved: False
```

The two fingerprints are the point of the line. `apply` reads the catalog before
and after and checks both against what the artifact says it expected, so an
artifact built against a different starting schema is refused rather than
applied onto a shape it does not fit.

`WREATH_MIGRATION_DSN` is a separate variable on purpose: applying DDL wants an
owner, and serving requests should not have one.

**Starting over.** If you drop the schema and re-apply, drop its ledger row too —
the artifact declares no parent, and the engine refuses to replay it onto a
history that already has a tip:

```bash
psql "$CAMERA_TRAP_DSN" -c \
  "DELETE FROM wreath_migrations.history WHERE target_schema = 'camera_trap'"
```

## 3. Seed it

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

```
{'reserves': 4, 'stations': 48, 'cameras': 61, 'species': 40, 'observers': 24,
 'assignments': 45, 'deployments': 576, 'sightings': 140000, 'audit_entries': 600}
```

About twelve seconds. The [schema tour](walkthrough.md) explores what just
landed — four reserves in four timezones, one of them at a fractional `+09:30`
offset, and a `review_state` column full of free text that a later chapter will
have to clean up.

## 4. Serve it

```bash
PYTHONPATH=. wreath run camera_trap.app:app --port 8000
```

```
/home/you/wreath/example/camera_trap/app.py:78: RuntimeWarning:
CAMERA_TRAP_SESSION_SECRET is unset; signing sessions with the public
development secret. Set it to a random string of at least 32 characters before
serving this to anyone: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`
```

**That warning is correct and you should read it once.** The example signs
sessions with a constant whose value says what it is, so a fresh clone runs with
no setup. The two alternatives are both worse: refusing to start makes your
first experience an error about a variable you have no opinion on yet, and
generating a random secret works perfectly on one process and silently signs
every user out the moment a second replica starts. Set the variable before
anyone else can reach it.

The application starts on the framework default, `validate_schema="error"`, so
it has already read the catalog and confirmed that every table, column, type,
constraint and index the models declare is really there. A schema that does not
match is a refusal at startup rather than a surprise on the first query.

## 5. A first request

The species vocabulary is public:

```bash
curl -s localhost:8000/species
```

```json
{"items":[{"id":9,"code":"AARD","common_name":"Aardvark",
"scientific_name":"Orycteropus afer","protection":"sensitive","nocturnal":true}, ...]}
```

The observations are not:

```bash
curl -s localhost:8000/reserves
```

```json
{"type":"about:blank","title":"Unauthorized","status":401,"detail":"Unauthorized"}
```

So sign in. There is no password — the example's login is deliberately one
lookup, because a real OIDC dance would double the setup before you could see an
authorization rule work, and [the cookbook](../cookbook/recipes/oauth2-login.md)
already owns that ground:

```bash
curl -s -c jar.txt -X POST 'localhost:8000/session?email=volunteer1@example.org'
```

```json
{"id":1,"display_name":"Volunteer 1","role":"volunteer"}
```

```bash
curl -s -b jar.txt localhost:8000/reserves
```

```json
{"items":[
  {"id":4,"slug":"chiquibul","name":"Chiquibul Forest",
   "timezone":"America/Belize","area_hectares":17300},
  {"id":3,"slug":"nullarbor","name":"Nullarbor Station",
   "timezone":"Australia/Adelaide","area_hectares":41200},
  {"id":1,"slug":"olkiramatian","name":"Olkiramatian Conservancy",
   "timezone":"Africa/Nairobi","area_hectares":22400},
  {"id":2,"slug":"serra-da-estrela","name":"Serra da Estrela Reserve",
   "timezone":"Europe/Lisbon","area_hectares":8900}]}
```

## The one worth trying twice

Station 25 in Nullarbor is marked sensitive. Ask for it as the volunteer you are
already signed in as:

```bash
curl -s -b jar.txt localhost:8000/reserves/nullarbor/stations/25
```

```json
{"id":25,"reserve_id":3,"name":"Nullarbor 01","habitat":"riverine forest",
 "sensitive":true,"cameras":[...]}
```

Now sign in as a ranger and ask again:

```bash
curl -s -c ranger.txt -X POST 'localhost:8000/session?email=ranger1@example.org'
curl -s -b ranger.txt localhost:8000/reserves/nullarbor/stations/25
```

```json
{"id":25,"reserve_id":3,"name":"Nullarbor 01","habitat":"riverine forest",
 "sensitive":true,"latitude":0.6,"longitude":40.1,"cameras":[...]}
```

The volunteer's response has **no `latitude` key at all** — not `null`. That is
deliberate: `"latitude": null` means "this station has no coordinates", which is
false and would be plotted at the intersection of the equator and the prime
meridian. An absent key means "not for you", and a client can tell the
difference.

Neither response was filtered after the fact. The rule is a Cedar policy the
application also uses to answer *what may I do?*, so the console greys out the
buttons a volunteer cannot press from the same declaration that refuses them.

## Where to go next

- **[The schema, in psql](walkthrough.md)** — the nine tables, the timezones,
  the partial indexes, and the late-arriving data.
- **[The read API](read-api.md)** — the nine routes, nested routers, binding,
  declared queries, and the cache the ORM clears without being asked.
- **[The code](https://github.com/alexogeny/wreath/tree/main/example)** — about
  1,200 lines, every file with a docstring saying why it exists.

## Cleaning up

```bash
docker rm -f wreath-test-pg
```
