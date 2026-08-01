```hero
eyebrow: The protocol example
title: Binary in. Live map out. Precision by policy.
lede: Collars on eighteen animals report over a metered satellite link. A field
  station relays the bytes. Researchers watch a map that updates as the
  positions land — and four people watching the same map are shown four
  different maps, because where a rhino is, is poaching intelligence.
action: Run it in three minutes -> #run-it
action: Read the code -> https://github.com/alexogeny/wreath/tree/main/example/tracking
```

One conservancy in the southern Rift Valley. Eighteen collared animals: two
black rhinos, four cats, a pangolin, and eleven herbivores nobody is hiding. A
collar takes a position every twenty minutes and uploads it when it next sees a
satellite, which is minutes later on a good day and *days* later under canopy. A
field station on the ground relays what comes down.

That is the whole domain, and it needs four things the
[camera-trap example](../example/index.md) does not have: a binary body, a
coordinate that *is* the record, a stream, and an answer whose resolution
depends on who is asking.

## The one to try first

Ask for one leopard's first fix of the day, four times, as four different
people. Same route, same row, same code path:

```
ranger     {"animal_id": 3, "collar_id": 3, "recorded_at": "2026-03-09T21:00:00+00:00",
            "received_at": "2026-03-09T21:03:00+00:00", "battery_pct": 94,
            "position": {"lat": -1.9200878285402727, "lon": 36.066374200707045},
            "precision_m": 0.0, "accuracy_m": 25.4}

partner    {"animal_id": 3, ..., "battery_pct": 94,
            "position": {"lat": -1.9200503619767075, "lon": 36.069533949753264},
            "precision_m": 1000.0}

volunteer  {"animal_id": 3, ..., "battery_pct": 94,
            "position": {"lat": -1.9335401771662395, "lon": 36.03832547745748},
            "precision_m": 10000.0}

public     {"animal_id": 3, ..., "battery_pct": 94}
```

Read the partner's line twice, because it is the one that looks wrong. Its
latitude is almost the ranger's — the animal happened to be near the middle of
its cell — and only the longitude moved. **That is what a bound is.** A grade
promises the answer is no further from the truth than the cell's half-diagonal;
it does not promise the answer is *far*. Landing close is luck, and a scheme
that guaranteed a minimum displacement would be leaking the direction it moved.

Four things are happening and none of them is a filter bolted onto a response.

**The position key is absent, not null.** `"position": null` is a different
claim — it says this fix has no coordinates, which is false, and a client
plotting it would put a leopard at the intersection of the equator and the prime
meridian. Absent means *not for you*. This is the camera-trap example's argument
about a station's latitude, and it survives being generalised.

**The generalisation is the point.** Camera-trap asks a yes/no question and gets
a key or no key. Here the answer is a *resolution*, and `precision_m` says which
one on the wire — so a client can draw the right circle instead of guessing, and
a reader knows they are looking at an approximation rather than at the truth.

**`accuracy_m` only appears at full resolution.** "A 10 km cell, accurate to
25 m" is a contradiction a client resolves in favour of the smaller number.
Drawing a 25 m circle around a 10 km answer is exactly the map a degraded
coordinate exists to prevent.

**Cedar still answers yes or no.** There is no graded decision anywhere and
nothing scores a principal. There are three actions — one per grade — and the
application asks about each in turn, finest first, and takes the first
permission it is given. Quoted from `tracking/policies.py`, unedited:

```
// A collared zebra in a gait study, a wildebeest in the movement survey: the
// track is the published output of the programme, at the resolution it was
// recorded.
permit(principal, action == Action::"Position::locate_exact", resource)
  when { resource.protection == "open" };

// A ranger drives out to a snared animal, and a snare is found by walking to a
// coordinate. Degrading this would not protect anything; it would leave a wire
// round a leg for another day.
permit(principal in Role::"ranger", action == Action::"Position::locate_exact", resource);

// A partner institution asks about range and habitat use. Those are
// kilometre-scale questions and a kilometre-scale answer is a complete answer.
permit(principal in Role::"partner", action == Action::"Position::locate_coarse", resource)
  when { resource.protection == "sensitive" };

permit(principal in Role::"volunteer", action == Action::"Position::locate_approximate", resource)
  when { resource.protection == "sensitive" };
```

The whole grid, which `tests/tracking/test_policy.py` holds as data:

| protection | ranger | partner | volunteer | public |
|---|---|---|---|---|
| `open` | exact | exact | exact | exact |
| `sensitive` | exact | 1 km | 10 km | absent |
| `restricted` | exact | absent | absent | absent |

There is **no statement for `restricted`** other than the ranger's, and that
absence is the rule: Cedar's default is deny, so a tier nobody wrote a permit
for is withheld — and stays withheld when somebody later adds a grade without
thinking about rhinos.

### An honest note about mutation-testing this file

`wreath mutant` has operators for exactly this: `cedar.delete-policy`,
`cedar.drop-condition`, `cedar.flip-effect`. Point them at `policies.py` and
**every one of them survives**, which reads as a hole in the tests and is not
one.

The reason is in the harness's own docstring. A policy set written as a
module-level constant has no enclosing function to recompile, so those mutants
are installed by *rebinding the name*. But this file — like the camera trap's,
and like the shape [the guide](../guides/permissions.md) recommends — compiles
its policies at import:

```python
ENGINE = CedarPolicies(POLICY_SOURCE)
```

By the time the constant is rebound, `ENGINE` has already consumed it. The
mutation lands on a string nothing reads again.

Deleting each policy *before* import and rebuilding the engine changes 1–3 cells
of the grid above every time, and `test_policy.py` asserts all twelve — so the
statements are load-bearing and the tests do object. The survivors are the
harness reaching a shape it cannot reach, and they are reported as such rather
than argued away.

## Why the coarse answer is a grid and not a blur

This is the decision the whole thing rests on, and it is the one that is easy to
get wrong in a way nothing catches.

The obvious way to hide a position is to add a random offset. It is defeated by
a `for` loop: ask twenty times, average the answers, and the noise cancels. Any
*unbiased* jitter has that property by definition, and a biased one is a lie
about where the animal was.

So the coordinate is snapped to the centre of a grid cell instead. The same fix
always produces the same coarse answer, so twenty requests are one request and
repetition buys nothing. `tests/tracking/test_api.py` asks twenty times and
asserts it got one answer, because that is the property the scheme is made of.

The grid is fixed to the equator and the prime meridian rather than to the
animal, so it does not move with what it is hiding — and an animal that crosses
a cell boundary does change answer, which is honest: it moved a kilometre.

## Where this example stops

**Tier-1 geospatial has no spatial join.** Bounding boxes and haversine answer
"which collars came within 5 km of the waterhole" and "how far did this animal
walk". They do not answer "which animals crossed the northern boundary" or
"infer the migration corridor", and no amount of application code fixes that —
those are polygon containment and a spatial join, and they need PostGIS.
[Place and policy](place.md) says exactly what would change and what would not.

It is also deliberately the smaller of the two examples. Routing, nested
routers, cursor paging, generated CRUD, background jobs, an object store and
migrations-as-artifacts are taught by the [camera trap](../example/index.md) and
are not taught again. Three pages, not six.

## Run it

Every command below was run in the order it appears and every block of output is
what came back. The seed is deterministic, so your numbers will match — if they
do not, something is wrong rather than merely different.

You need Python 3.14, a checkout of the repository, and Docker (or Podman, or
nerdctl — the commands are the same).

### 1. A database

```bash
docker run -d --name wreath-tracking-pg \
  -e POSTGRES_PASSWORD=wreath -e POSTGRES_USER=wreath -e POSTGRES_DB=wreath_test \
  -p 55478:5432 pgvector/pgvector:pg17

export TRACKING_DSN="postgresql://wreath:wreath@127.0.0.1:55478/wreath_test"
export TRACKING_SESSION_SECRET="tracking-quickstart-secret-0123456789abcd"
```

The example reads `TRACKING_DSN` and has **no default** — guessing at
`localhost` and connecting to the wrong database is worse than refusing to
start, so it refuses and the message names the variable. It also honours
`WREATH_TEST_POSTGRES_DSN`, so anyone who has run wreath's own database suites
has already set it.

### 2. A schema, and the artifact

```bash
docker exec -i wreath-tracking-pg psql -U wreath -d wreath_test \
  -c 'CREATE SCHEMA IF NOT EXISTS tracking'

cd example
export WREATH_MIGRATION_DSN="$TRACKING_DSN"   # apply never reuses request credentials
PYTHONPATH=. wreath migrations apply tracking.app:app tracking/migrations/migration.bin
```

```
applied migration 0000000000000000000000000000d101
  applied: True
  checksum: 16dd46aa58dd9fb065cd079a625892d6cfddbe143a5e1e05aceef0067c77bc31
  source fingerprint: 59ba344334afa8fdd8ed07d7ddd68bdfb38c37d0710f9bd860051f8cb5f991e4
  target fingerprint: a1abddeba138133b671bdef078dee4e23efc5c61a7d3c6ad40cd1d66f7793f0c
  destructive approved: False
```

**One more table set, and it is not in the artifact.** The daily chart seals its
buckets, and settled buckets live in wreath's own `wreath` schema rather than in
this application's — the artifact describes what the author *declared*, and
nobody declared a settled-bucket store. Nothing creates them for you:

```bash
PYTHONPATH=. python -c \
  'from wreath._series.settle import schema_sql; print(schema_sql() + ";")' \
  | docker exec -i wreath-tracking-pg psql -U wreath -d wreath_test
```

```
CREATE SCHEMA
CREATE TABLE
CREATE TABLE
```

That private import is a rough edge and this page would rather say so than hide
it: every other table wreath owns — the job ledger, the message bus — is claimed
through `app.schema_components()` and created during lifespan startup, and this
one is not. See [Ingest and realtime](ingest.md#the-rough-edges-this-example-hit).

### 3. Seed it

```bash
PYTHONPATH=. python -c '
import asyncio, os
from tracking.seed import seed
from wreath.postgres import connect

async def main():
    connection = await connect(os.environ["TRACKING_DSN"])
    try:
        print(await seed(connection))
    finally:
        await connection.close()

asyncio.run(main())'
```

```
{'animals': 18, 'collars': 18, 'landmarks': 6, 'fixes': 51840}
```

Forty days at a twenty-minute duty cycle, in a little over a second — 208
multi-row `INSERT`s, because the driver has no `COPY` path. Two of those
collars go quiet — one rhino for four days, one wildebeest for two — and dump
their buffers afterwards, which is what makes the late-data chapter a fact about
the data rather than a paragraph about a possibility.

### 4. Serve it

```bash
PYTHONPATH=. wreath run tracking.app:app --port 8137
```

```
🌿 wreath 0.1.0a3 serving tracking.app:app on http://127.0.0.1:8137  (http/1.1, native, asyncio loop)
```

It started on the framework default, `validate_schema="error"`, so it has
already read the catalog and confirmed that every table, column, type,
constraint and index the models declare is really there.

### 5. A first request

The roster is public, and it says what each animal will cost you to look at:

```bash
curl -s localhost:8137/animals
```

```json
{"items":[
 {"id":1,"name":"Naserian","taxon":"Black rhinoceros","protection":"restricted"},
 {"id":2,"name":"Olekuoo","taxon":"Black rhinoceros","protection":"restricted"},
 {"id":3,"name":"Nashipae","taxon":"Leopard","protection":"sensitive"},
 {"id":8,"name":"Sarara","taxon":"Plains zebra","protection":"open","precision_m":0.0},
 ...]}
```

The rhinos carry no `precision_m` at all, because an anonymous caller will not
be given a position of any resolution. The zebra carries `0.0`, meaning exact.
That the rhinos *exist* is not the secret — a conservancy announces its rhinos,
and pretending otherwise would make the programme unfundable.

### 6. The four principals

This example ships no sign-in route: sessions, login and the identity seam are
[the camera trap's ground](../example/read-api.md) and are not re-taught here.
So the ladder is easiest to see through `TestClient.acting_as`, which is also
how the tests do it:

```bash
PYTHONPATH=. python -c '
import asyncio, json
from tracking.app import build
from wreath.testing import TestClient

async def main():
    async with TestClient(build(cross_worker=False)) as client:
        for role in ("ranger", "partner", "volunteer", None):
            who = client if role is None else client.acting_as(
                f"{role}-1", roles=[role], type="Observer")
            body = (await who.get("/animals/3/track?since=2026-03-10&days=1")).json()
            print(f"{role or \"public\":10}", json.dumps(body["fixes"][0]))

asyncio.run(main())'
```

which prints the four lines at the top of this page.

### Cleaning up

```bash
docker rm -f wreath-tracking-pg
```

## Where to go next

- **[Ingest and realtime](ingest.md)** — the protobuf wire, why a batch of one
  and a batch of two hundred are the same request, the collar that lost the sky,
  and one broadcast that becomes four different maps.
- **[Place and policy](place.md)** — bounding boxes and the index, the
  nearest-neighbour question a btree cannot answer, the leak that a landmark
  distance opens, and where PostGIS would come in.
- **[The camera-trap example](../example/index.md)** — the canonical one. Read
  it first if you have not.
