# A tour of the schema, in psql

The camera-trap example seeds 141,398 rows across nine tables. This page is a
tour of them with `psql`, because a schema you can query is a schema you can
argue with — and because several of wreath's harder ideas are visible in the
data before you read a line of application code.

Every query below was run against the seeded database and the output is what it
printed. If yours differs, something is wrong: the seed is deterministic.

Set up the database and seed it first — [the quickstart](quickstart.md) has the
container command — then:

```bash
psql "$CAMERA_TRAP_DSN"
```

## What is here

```sql
SELECT 'sightings' t, count(*) FROM camera_trap.sightings
UNION ALL SELECT 'deployments', count(*) FROM camera_trap.deployments
UNION ALL SELECT 'cameras',     count(*) FROM camera_trap.cameras
UNION ALL SELECT 'stations',    count(*) FROM camera_trap.stations
ORDER BY 2 DESC;
```

```
       t       | count
---------------+--------
 sightings     | 140000
 deployments   |    576
 cameras       |     61
 stations      |     48
```

Use `count(*)`, not `pg_stat_user_tables.n_live_tup`. That column is a planner
*estimate*; immediately after a bulk load it read 147,500 against a true 140,000
here. The seed runs `ANALYZE` at the end so the estimate is close, but close is
not the same as right, and a walkthrough that quotes an estimate as a fact
teaches the wrong habit.

## Four reserves, four timezones

```sql
SELECT id, name, timezone, area_hectares FROM camera_trap.reserves ORDER BY id;
```

```
 id |           name           |      timezone      | area_hectares
----+--------------------------+--------------------+---------------
  1 | Olkiramatian Conservancy | Africa/Nairobi     |         22400
  2 | Serra da Estrela Reserve | Europe/Lisbon      |          8900
  3 | Nullarbor Station        | Australia/Adelaide |         41200
  4 | Chiquibul Forest         | America/Belize     |         17300
```

These are not decoration. `Africa/Nairobi` has no daylight saving,
`Europe/Lisbon` does, `America/Belize` does not, and **`Australia/Adelaide` sits
at +09:30** — a fractional offset, which is the case that breaks code assuming
whole-hour zones. An analysis comparing these four reserves is comparing four
different wall clocks.

## The night belongs to different animals

A camera records the local wall-clock time it saw something. So "how much moved
last night" has to be asked in the reserve's own zone, not the server's:

```sql
SELECT date_part('hour', s.captured_at AT TIME ZONE r.timezone)::int AS local_hour,
       count(*) FILTER (WHERE sp.nocturnal)     AS night_species,
       count(*) FILTER (WHERE NOT sp.nocturnal) AS day_species
FROM camera_trap.sightings s
JOIN camera_trap.species  sp ON sp.id = s.species_id
JOIN camera_trap.stations st ON st.id = s.station_id
JOIN camera_trap.reserves  r ON  r.id = st.reserve_id
WHERE date_part('hour', s.captured_at AT TIME ZONE r.timezone) IN (2,3,12,13,19,20)
GROUP BY 1 ORDER BY 1;
```

```
 local_hour | night_species | day_species
------------+---------------+-------------
          2 |          3963 |           0
          3 |          3956 |           0
         12 |             0 |        4785
         13 |             0 |        4894
         19 |          7729 |           0
         20 |          7826 |           0
```

Clean separation, with dusk busier than deep night — which is the shape real
camera-trap data has.

Drop the `AT TIME ZONE r.timezone` and the separation blurs, because the same
UTC hour is a different time of day at each reserve. That is the entire argument
for `wreath.temporal` in one query, and it is why the analysis layer buckets by
*night* rather than by calendar day.

## A station outlives its cameras

Cameras get stolen, eaten, and replaced. A station is the *place*, and its
history has to survive the hardware:

```sql
SELECT c.id AS camera, c.serial, c.deployed_at::date AS deployed,
       c.retired_at::date AS retired, count(s.id) AS sightings
FROM camera_trap.cameras c
LEFT JOIN camera_trap.sightings s ON s.camera_id = c.id
WHERE c.station_id = 3
GROUP BY c.id ORDER BY c.deployed_at;
```

```
 camera |  serial  |  deployed  |  retired   | sightings
--------+----------+------------+------------+-----------
      3 | CT-00003 | 2025-01-06 | 2025-08-15 |      1169
     51 | CT-00051 | 2025-08-15 |            |      1809
```

Two devices, one place, one continuous record. Collapse `Station` and `Camera`
into one table — the tempting simplification — and station 3's activity history
restarts in August for no ecological reason.

## The late card

This is the query the whole analysis design turns on.

```sql
SELECT d.card_serial,
       d.collected_at::date                            AS collected,
       max(s.captured_at)::date                        AS last_image,
       d.collected_at::date - max(s.captured_at)::date AS days_stale,
       count(*)                                        AS images
FROM camera_trap.deployments d
JOIN camera_trap.sightings   s ON s.deployment_id = d.id
GROUP BY d.id
HAVING d.collected_at::date - max(s.captured_at)::date > 7
ORDER BY days_stale DESC LIMIT 6;
```

```
 card_serial | collected  | last_image | days_stale | images
-------------+------------+------------+------------+--------
 SD-0480     | 2026-07-21 | 2026-07-07 |         14 |    244
 SD-0384     | 2026-07-16 | 2026-07-07 |          9 |    223
 SD-0252     | 2026-07-16 | 2026-07-07 |          9 |    210
 SD-0516     | 2026-07-16 | 2026-07-07 |          9 |    198
 SD-0120     | 2026-07-16 | 2026-07-07 |          9 |    161
 SD-0372     | 2026-07-15 | 2026-07-07 |          8 |    187
```

Card SD-0480 was collected on 21 July carrying 244 images, the newest of which
was already two weeks old. Those rows arrive in the database *after* charts
covering their dates were computed.

That is not an edge case in this domain; it is how the data always arrives. It
is why a chart of last week cannot simply be recomputed and forgotten, and why
the analysis layer treats a settled bucket as final and reports late arrivals as
*corrections* rather than silently rewriting history.

`deployment_id` is what makes this answerable. Without it a sighting could
belong to any card collected at its station, and "how late was this row" would
have no query — only a paragraph claiming it mattered.

## The mess chapter two cleans up

```sql
SELECT review_state, count(*) FROM camera_trap.sightings GROUP BY 1 ORDER BY 2 DESC;
```

```
 review_state | count
--------------+-------
 confirmed    | 64495
 needs-review | 23737
 Confirmed    | 12628
 rejected     |  9687
              |  8467
 ok           |  8338
 needs review |  7089
 no           |  2797
 ?            |  2762
```

Nine spellings for four meanings. Eighteen months of a review console posting
whatever it liked into a free-text column, and now nobody can count how many
sightings are confirmed — `confirmed` and `Confirmed` and `ok` are the same
answer, and `""` and `?` are not obviously anything.

This is deliberate. The example ships v1 with the flaw so that
chapter two (stage 7, not yet written) can fix it with a deferred migration, and so the
transitional scan has something true to catch.

## What the partial indexes are for

The review console only ever asks for work a human has not settled — a few
thousand rows out of 140,000. That ratio is what a partial index is for:

```sql
EXPLAIN (COSTS OFF)
SELECT id FROM camera_trap.sightings
WHERE station_id = 7 AND review_state = 'needs-review'
ORDER BY captured_at LIMIT 20;
```

```
 Limit
   ->  Index Scan using wreath_d1006d8f04327f48 on sightings
         Index Cond: (station_id = 7)
```

Read the `Index Cond` carefully: it mentions `station_id` and **not**
`review_state`. The predicate is not a filter the query has to apply, because
every row in that index already satisfies it. The index is declared on the model
as

```python
_unreviewed = index("station_id", "captured_at", where=eq("review_state", "needs-review"))
```

and the migration engine names it, emits it, and recognises it again when it
next compares the models to the database.

Five of this schema's indexes are partial, and the other four follow the same
reasoning: the sensitive stations, the two withheld protection levels, the
cameras still in service (`retired_at IS NULL`), and the cards not yet ingested
(`ingested_at IS NULL`). Each one indexes the minority its query actually asks
for.

## The rows access control exists to protect

```sql
SELECT sp.common_name, sp.protection,
       count(DISTINCT s.id) AS sightings,
       count(DISTINCT a.id) AS audited
FROM camera_trap.species sp
JOIN camera_trap.sightings s ON s.species_id = sp.id
LEFT JOIN camera_trap.audit_entries a ON a.sighting_id = s.id
WHERE sp.protection = 'restricted'
GROUP BY sp.id ORDER BY 3 DESC;
```

```
   common_name    | protection | sightings | audited
------------------+------------+-----------+---------
 Ground pangolin  | restricted |      3534 |     208
 Black rhinoceros | restricted |      3474 |     190
 White rhinoceros | restricted |      3428 |     202
```

Three restricted species, ten thousand sightings between them, and a permit
condition that every look at one of those locations is logged. Publishing a
rhino's coordinates assists poachers — this is the reason conservation databases
have row-level access control, and it is what the example's Cedar policy
enforces rather than demonstrates.

Eight of the 48 stations carry `sensitive = true` for the same reason, and their
`latitude`/`longitude` are hidden from volunteers by the admin console's
field-level rules.

## Where to go next

- [The read API](read-api.md) is this same data over HTTP: nine routes, the local-date
  window, stable paging, and the one endpoint worth caching. It is written but
  not yet in the navigation, so this is deliberately not a link.
- Chapter two (stage 7, not yet written) recodes `review_state` and shows what the
  transitional scan refuses.
- The models are in `example/camera_trap/models.py`; every table above has a
  docstring explaining why it exists rather than what it contains.
