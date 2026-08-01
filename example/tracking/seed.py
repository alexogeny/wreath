"""Deterministic seed data: eighteen animals, forty days, 51,840 fixes.

Two people running this get the same rows, the same distances and the same
charts, which is what lets the documentation quote a number and this example's
tests assert one. Everything random comes from one seeded generator and every
timestamp is derived from :data:`EPOCH` rather than from the clock -- the only
two sources of non-determinism a data generator normally has, and both closed.

The data is *shaped* rather than uniform, because a uniform walk would make
every question here answerable and therefore prove nothing:

* **Animals move like animals.** A correlated random walk with a drift that
  turns slowly, so a track has direction and a day has a distance worth
  charting. A pure random walk would put every animal back where it started and
  make `Trajectory.distance` the only interesting number in the file.
* **They come to water.** Each animal has a home range centred on one of the
  three waterholes and is pulled back towards it, so `within(waterhole, 5 km)`
  returns something and the proximity query has a real answer.
* **Two collars go quiet.** One rhino spends four days under riverine canopy
  and one wildebeest spends two, then both dump their buffers. Those are the
  rows the sealed daily view has to record a correction for; without them the
  late-data chapter would be prose.
* **Three positions are junk.** One collar reports a latitude of 91.4 degrees
  after a firmware fault. It is in the *ingest fixture*, not in this table --
  see `tests/tracking/test_ingest.py` -- because this seeder writes rows and the
  refusal is the ingest path's job.

Six decimal places is about a tenth of a metre, which is far finer than a
collar, but the coordinates are generated as `float` and stored as `float8`, so
nothing is rounded on the way in and the stored value is the generated one.
"""

from __future__ import annotations

import datetime
import math
import random
from typing import Any

from wreath.geospatial import Coordinate, distance

from .config import SCHEMA

#: Every timestamp is an offset from here. Fixed, so the seed is reproducible
#: and the documentation can name a date.
EPOCH = datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC)

#: Forty days of history: long enough that most of it is past a 36-hour sealing
#: horizon and short enough to seed in a couple of seconds.
DAYS = 40

#: One generator, one seed, threaded through every helper below.
SEED = 20260801

#: How often a collar takes a position. Twenty minutes is the usual duty cycle
#: for a large-mammal collar with a solar panel: 72 fixes a day, which is 2,880
#: over the seeded window per animal.
FIX_MINUTES = 20

#: Rows per INSERT. See `tracking.ingest.BATCH` for the arithmetic.
BATCH = 250

#: The conservancy's rough centre, in the southern Rift Valley -- the same
#: landscape the camera-trap example's Olkiramatian reserve sits in, because
#: these are two applications watching one place.
CENTRE_LAT = -1.9705
CENTRE_LON = 36.1042

#: Landmarks: three waterholes, a gate, a ranger post, and the airstrip.
#: Offsets in degrees from the centre, so the whole conservancy fits in a box
#: about 24 km across -- which is why 10 km precision means "north or south".
LANDMARKS: tuple[tuple[int, str, str, float, float], ...] = (
    (1, "Ndovu Waterhole", "waterhole", 0.0412, -0.0330),
    (2, "Simba Waterhole", "waterhole", -0.0455, 0.0512),
    (3, "Kifaru Spring", "waterhole", 0.0128, 0.0741),
    (4, "North Gate", "gate", 0.0902, 0.0044),
    (5, "Ranger Post Kimana", "post", -0.0210, -0.0688),
    (6, "Airstrip", "airstrip", -0.0733, 0.0125),
)

#: Eighteen collared animals. ``(id, name, taxon, protection, home landmark)``.
#:
#: Two rhinos are `restricted`, four cats and the pangolin are `sensitive`, and
#: eleven herbivores are `open` -- the same tiers, and mostly the same species,
#: as the camera-trap example's `Species.protection`, because it is one
#: conservancy and an animal that is restricted on a camera is not open on a
#: collar.
ANIMALS: tuple[tuple[int, str, str, str, int], ...] = (
    (1, "Naserian", "Black rhinoceros", "restricted", 3),
    (2, "Olekuoo", "Black rhinoceros", "restricted", 3),
    (3, "Nashipae", "Leopard", "sensitive", 1),
    (4, "Sirwa", "Lion", "sensitive", 2),
    (5, "Meoli", "Lion", "sensitive", 2),
    (6, "Kirrua", "Cheetah", "sensitive", 1),
    (7, "Tinka", "Ground pangolin", "sensitive", 3),
    (8, "Sarara", "Plains zebra", "open", 1),
    (9, "Loita", "Plains zebra", "open", 1),
    (10, "Ewaso", "Plains zebra", "open", 2),
    (11, "Nkoteiya", "Blue wildebeest", "open", 2),
    (12, "Sidai", "Blue wildebeest", "open", 2),
    (13, "Tipilit", "Blue wildebeest", "open", 3),
    (14, "Maiyan", "Masai giraffe", "open", 1),
    (15, "Sopa", "Masai giraffe", "open", 3),
    (16, "Nalotuesha", "African elephant", "open", 2),
    (17, "Parsaloi", "Cape buffalo", "open", 1),
    (18, "Naiperreu", "Common eland", "open", 3),
)

#: ``(animal_id, day the silence starts, how many days)``. The two collars whose
#: buffers land past the sealing horizon.
SILENCES: tuple[tuple[int, int, int], ...] = ((1, 12, 4), (11, 25, 2))

#: How far an animal moves between fixes, in metres, by taxon. A resting lion
#: and a walking elephant are different problems and a single constant would
#: make every track look the same.
STEP_M: dict[str, float] = {
    "Black rhinoceros": 95.0,
    "Leopard": 140.0,
    "Lion": 120.0,
    "Cheetah": 165.0,
    "Ground pangolin": 45.0,
    "Plains zebra": 110.0,
    "Blue wildebeest": 130.0,
    "Masai giraffe": 100.0,
    "African elephant": 155.0,
    "Cape buffalo": 105.0,
    "Common eland": 125.0,
}

#: Metres in one degree of latitude, and the cosine of the conservancy's
#: latitude for longitude. Local and constant over 24 km, which is why the seed
#: can use a flat approximation to *place* animals while every measurement made
#: of them goes through `wreath.geospatial.distance`.
_M_PER_DEG_LAT = 111_195.0
_M_PER_DEG_LON = 111_195.0 * math.cos(math.radians(CENTRE_LAT))


def _at(day: int, minute: int) -> datetime.datetime:
    """A timestamp `day` days and `minute` minutes after the epoch."""
    return EPOCH + datetime.timedelta(days=day, minutes=minute)


def _walk(
    rng: random.Random, home: Coordinate, taxon: str, count: int
) -> list[tuple[float, float]]:
    """A correlated random walk that keeps coming back to water.

    Two forces. The heading turns by a small random amount each step, which is
    what makes a *track* rather than a cloud -- consecutive steps in similar
    directions. And a weak pull towards `home`, proportional to how far away the
    animal has wandered, which keeps a season inside a home range instead of
    diffusing an elephant into Tanzania.

    Neither is biology. Both exist so the questions this example asks have
    answers: without the correlation a day's distance is noise, and without the
    pull `within(waterhole, 5 km)` finds nothing after the first week.
    """
    step = STEP_M[taxon]
    heading = rng.uniform(0.0, 2.0 * math.pi)
    lat, lon = home.lat, home.lon
    path: list[tuple[float, float]] = []
    for _ in range(count):
        heading += rng.gauss(0.0, 0.55)
        metres = max(5.0, rng.gauss(step, step * 0.45))
        lat += metres * math.cos(heading) / _M_PER_DEG_LAT
        lon += metres * math.sin(heading) / _M_PER_DEG_LON
        # The pull, applied in degrees because it is a nudge rather than a
        # measurement. 1.5% per step brings an animal 4 km out back inside its
        # range over about a day.
        lat += (home.lat - lat) * 0.015
        lon += (home.lon - lon) * 0.015
        path.append((lat, lon))
    return path


def build_rows() -> dict[str, list[tuple[Any, ...]]]:
    """Every row, deterministically, without touching a database.

    Split out from the insert so the determinism claim is checkable in
    milliseconds and without PostgreSQL -- which is what
    `tests/tracking/test_seed.py` does.
    """
    rng = random.Random(SEED)
    rows: dict[str, list[tuple[Any, ...]]] = {}

    rows["landmarks"] = [
        (mark_id, name, kind, CENTRE_LAT + dlat, CENTRE_LON + dlon)
        for mark_id, name, kind, dlat, dlon in LANDMARKS
    ]
    homes = {
        mark_id: Coordinate(lat=CENTRE_LAT + dlat, lon=CENTRE_LON + dlon)
        for mark_id, _name, _kind, dlat, dlon in LANDMARKS
    }

    rows["animals"] = [
        (animal_id, name, taxon, protection)
        for animal_id, name, taxon, protection, _home in ANIMALS
    ]

    #: One collar per animal, fitted a week before the first fix. A collar's
    #: second life on another animal is a case the schema models and the seed
    #: does not exercise -- inventing a refurbishment to demonstrate a foreign
    #: key would be the sort of padding this example is trying not to have.
    rows["collars"] = [
        (animal_id, animal_id, f"CL-{animal_id:04d}", _at(-7, 0), None)
        for animal_id, *_ in ANIMALS
    ]

    silence = {animal: (start, length) for animal, start, length in SILENCES}
    per_day = (24 * 60) // FIX_MINUTES
    fixes: list[tuple[Any, ...]] = []
    for animal_id, _name, taxon, _protection, home_id in ANIMALS:
        path = _walk(rng, homes[home_id], taxon, DAYS * per_day)
        quiet_from, quiet_days = silence.get(animal_id, (None, 0))
        previous: Coordinate | None = None
        for step, (lat, lon) in enumerate(path):
            day, slot = divmod(step, per_day)
            recorded_at = _at(day, slot * FIX_MINUTES)
            # A collar under canopy keeps recording and uploads afterwards, so
            # `received_at` jumps while `recorded_at` does not. That gap is the
            # entire late-data story, in one branch.
            if quiet_from is not None and quiet_from <= day < quiet_from + quiet_days:
                received_at = _at(quiet_from + quiet_days, 30)
            else:
                received_at = recorded_at + datetime.timedelta(
                    minutes=rng.randrange(2, 25)
                )
            here = Coordinate(lat=lat, lon=lon)
            leg = None if previous is None else distance(previous, here)
            previous = here
            fixes.append((
                animal_id,
                recorded_at,
                animal_id,
                received_at,
                lat,
                lon,
                round(rng.uniform(4.0, 28.0), 1),
                max(3, 100 - (day * 100) // (DAYS * 3) - rng.randrange(0, 4)),
                leg,
                "relay-kimana",
                rng.randrange(4, 12),
            ))
    rows["fixes"] = fixes
    return rows


#: Column order per table, matching :func:`build_rows`.
COLUMNS: dict[str, tuple[str, ...]] = {
    "animals": ("id", "name", "taxon", "protection"),
    "collars": ("id", "animal_id", "serial", "fitted_at", "removed_at"),
    "landmarks": ("id", "name", "kind", "latitude", "longitude"),
    "fixes": (
        "collar_id", "recorded_at", "animal_id", "received_at", "latitude",
        "longitude", "accuracy_m", "battery_pct", "leg_m", "relay", "satellites",
    ),
}

#: Insert order: a table's referenced tables must already hold their rows.
ORDER = ("animals", "collars", "landmarks", "fixes")


def _insert_sql(table: str, columns: tuple[str, ...], count: int) -> str:
    width = len(columns)
    names = ", ".join(f'"{name}"' for name in columns)
    tuples = ", ".join(
        "(" + ", ".join(f"${row * width + col + 1}" for col in range(width)) + ")"
        for row in range(count)
    )
    return f'INSERT INTO "{SCHEMA}"."{table}" ({names}) VALUES {tuples}'


async def seed(connection: Any, *, fixes: bool = True) -> dict[str, int]:
    """Insert every row and return the count written per table.

    Idempotent by truncation: the tables are emptied first, so re-seeding is the
    same operation rather than a duplicate-key error.

    Args:
        connection: A `wreath.postgres` connection.
        fixes: `False` leaves the animals, collars and landmarks and writes no
            positions. That is the state a *new* deployment is in on its first
            morning -- the collars are fitted and nothing has come down yet --
            and it is what the ingest tests want, because a batch landing into
            an empty table is the only way to assert what a batch landed.
    """
    rows = build_rows()
    if not fixes:
        rows["fixes"] = []
    await connection.execute(
        "TRUNCATE "
        + ", ".join(f'"{SCHEMA}"."{table}"' for table in ORDER)
        + " RESTART IDENTITY CASCADE"
    )
    written: dict[str, int] = {}
    for table in ORDER:
        columns = COLUMNS[table]
        data = rows[table]
        for start in range(0, len(data), BATCH):
            chunk = data[start : start + BATCH]
            flat = [value for row in chunk for value in row]
            await connection.execute(_insert_sql(table, columns, len(chunk)), *flat)
        written[table] = len(data)
    # Planner statistics, so the proximity query's `EXPLAIN` reflects the data
    # that is there rather than a table PostgreSQL still believes is empty.
    # Without this the box query reads as a sequential scan on 26,000 rows and
    # the index it was written for is never chosen.
    for table in ORDER:
        await connection.execute(f'ANALYZE "{SCHEMA}"."{table}"')
    return written
