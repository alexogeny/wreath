"""Deterministic seed data for the camera-trap example.

Two people running this see the same rows, the same ids, and the same charts.
That is not tidiness — it is what lets the walkthrough quote a row count, and
what lets the example's own tests assert numbers instead of shapes.

**Everything random here comes from one seeded generator**, and every timestamp
is derived from :data:`EPOCH` rather than from the clock. Those are the only two
sources of non-determinism a data generator normally has, and both are closed.
Re-running against a clean schema produces byte-identical rows.

The data is shaped, not uniform. Species have diurnal and nocturnal habits, so
activity peaks at dawn and dusk rather than spreading evenly; eleven deployments
are collected long after their last capture, so the late-data query returns
something; and eight stations are marked sensitive, so the authorization story
has rows to protect.
"""

from __future__ import annotations

import datetime
import random
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .models import SCHEMA

#: Every timestamp is an offset from here. Fixed, so the seed is reproducible
#: and so a walkthrough can name a date.
EPOCH = datetime.datetime(2025, 1, 6, tzinfo=datetime.UTC)

#: 18 months of history.
DAYS = 548

#: One generator, one seed, threaded through every helper below.
SEED = 20260727

#: Rows per INSERT.
#:
#: Two ceilings bind here and the smaller one is not the obvious one.
#: PostgreSQL caps a statement at 65535 parameters, which for the 13-column
#: ``sightings`` row would allow ~5000. But the driver caps one outbound
#: operation at ``Connection.max_outbound_batch`` -- 256 KiB -- and the
#: *statement text* dominates that: every placeholder past ``$9999`` is six
#: characters, times thirteen per row. 250 rows keeps the widest table's packet
#: comfortably inside the cap.
#:
#: The driver has no ``COPY`` path, so a multi-row ``INSERT`` is the bulk shape
#: available. That is worth knowing before anyone plans a million-row seed.
BATCH = 250

RESERVES = (
    (1, "Olkiramatian Conservancy", "olkiramatian", "Africa/Nairobi", 22400),
    (2, "Serra da Estrela Reserve", "serra-da-estrela", "Europe/Lisbon", 8900),
    (3, "Nullarbor Station", "nullarbor", "Australia/Adelaide", 41200),
    (4, "Chiquibul Forest", "chiquibul", "America/Belize", 17300),
)

HABITATS = (
    "riverine forest", "open grassland", "acacia scrub", "rocky escarpment",
    "waterhole", "fence line", "dry riverbed", "canopy edge",
)

CAMERA_MODELS = ("Reconyx HP2X", "Bushnell Core DS", "Browning Spec Ops", "Cuddeback J3")

#: 40 species. ``protection`` is the column row-level authorization reads:
#: 31 open, 6 sensitive, 3 restricted.
SPECIES: tuple[tuple[str, str, str, str, bool], ...] = (
    ("LEOP", "Leopard", "Panthera pardus", "sensitive", True),
    ("RHIB", "Black rhinoceros", "Diceros bicornis", "restricted", False),
    ("RHIW", "White rhinoceros", "Ceratotherium simum", "restricted", False),
    ("PANG", "Ground pangolin", "Smutsia temminckii", "restricted", True),
    ("LION", "Lion", "Panthera leo", "sensitive", True),
    ("CHEE", "Cheetah", "Acinonyx jubatus", "sensitive", False),
    ("WDOG", "African wild dog", "Lycaon pictus", "sensitive", False),
    ("HYES", "Spotted hyena", "Crocuta crocuta", "sensitive", True),
    ("AARD", "Aardvark", "Orycteropus afer", "sensitive", True),
    ("ELEP", "African elephant", "Loxodonta africana", "open", False),
    ("BUFF", "Cape buffalo", "Syncerus caffer", "open", False),
    ("GIRA", "Masai giraffe", "Giraffa tippelskirchi", "open", False),
    ("ZEBP", "Plains zebra", "Equus quagga", "open", False),
    ("WILD", "Blue wildebeest", "Connochaetes taurinus", "open", False),
    ("IMPA", "Impala", "Aepyceros melampus", "open", False),
    ("KUDU", "Greater kudu", "Tragelaphus strepsiceros", "open", False),
    ("ELAN", "Common eland", "Taurotragus oryx", "open", False),
    ("WART", "Warthog", "Phacochoerus africanus", "open", False),
    ("BABO", "Olive baboon", "Papio anubis", "open", False),
    ("VERV", "Vervet monkey", "Chlorocebus pygerythrus", "open", False),
    ("GENE", "Common genet", "Genetta genetta", "open", True),
    ("CIVE", "African civet", "Civettictis civetta", "open", True),
    ("PORC", "Cape porcupine", "Hystrix africaeaustralis", "open", True),
    ("HONB", "Honey badger", "Mellivora capensis", "open", True),
    ("SERV", "Serval", "Leptailurus serval", "open", True),
    ("CARA", "Caracal", "Caracal caracal", "open", True),
    ("JACK", "Black-backed jackal", "Lupulella mesomelas", "open", True),
    ("BEFO", "Bat-eared fox", "Otocyon megalotis", "open", True),
    ("MONG", "Banded mongoose", "Mungos mungo", "open", False),
    ("HARE", "Scrub hare", "Lepus saxatilis", "open", True),
    ("DUIK", "Common duiker", "Sylvicapra grimmia", "open", True),
    ("BUSH", "Bushbuck", "Tragelaphus scriptus", "open", True),
    ("WATE", "Waterbuck", "Kobus ellipsiprymnus", "open", False),
    ("REED", "Bohor reedbuck", "Redunca redunca", "open", True),
    ("STEE", "Steenbok", "Raphicerus campestris", "open", False),
    ("ORIB", "Oribi", "Ourebia ourebi", "open", False),
    ("HIPP", "Hippopotamus", "Hippopotamus amphibius", "open", True),
    ("CROC", "Nile crocodile", "Crocodylus niloticus", "open", False),
    ("OSTR", "Common ostrich", "Struthio camelus", "open", False),
    ("GUIN", "Helmeted guineafowl", "Numida meleagris", "open", False),
)

#: The free-text mess the second chapter recodes. Eighteen months of a review
#: console posting whatever it liked. Weighted so "confirmed" dominates and the
#: stragglers are rare enough to be a real discovery in psql.
REVIEW_STATES = (
    ("confirmed", 46), ("Confirmed", 9), ("ok", 6),
    ("needs-review", 17), ("needs review", 5), ("", 6),
    ("rejected", 7), ("no", 2), ("?", 2),
)

_ROLES = (("volunteer", 14), ("researcher", 7), ("ranger", 3))


def _weighted(rng: random.Random, table: tuple[tuple[Any, int], ...]) -> Any:
    total = sum(weight for _, weight in table)
    point = rng.randrange(total)
    for value, weight in table:
        point -= weight
        if point < 0:
            return value
    return table[-1][0]


def _at(day: int, hour: int, minute: int, second: int = 0) -> datetime.datetime:
    """A timestamp *day* days after the epoch. Never reads the clock."""
    return EPOCH + datetime.timedelta(days=day, hours=hour, minutes=minute, seconds=second)


def _local(tz: ZoneInfo, day: int, hour: int, minute: int, second: int) -> datetime.datetime:
    """A capture at a *local* wall-clock hour, which is what a camera records.

    Generating in UTC and hoping is the obvious shortcut and it is wrong: a
    reserve at +09:30 would show its nocturnal species peaking at local noon,
    because 20:00 UTC is 05:30 there. Every downstream claim about night
    activity would then be false in the data while looking fine in the code.

    The returned value is aware, in the reserve's own zone, so the driver
    stores the instant the camera actually meant.
    """
    naive = EPOCH.replace(tzinfo=None) + datetime.timedelta(
        days=day, hours=hour, minutes=minute, seconds=second
    )
    return naive.replace(tzinfo=tz)


def _activity_hour(rng: random.Random, nocturnal: bool) -> int:
    """Dawn and dusk peaks, so an activity chart has a shape worth plotting."""
    if nocturnal:
        return rng.choice((18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 5, 19, 20, 4))
    return rng.choice((6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 7, 8, 16, 17))


def build_rows(*, sightings: int = 140_000) -> dict[str, list[tuple[Any, ...]]]:
    """Generate every row, deterministically, without touching a database.

    Split out from the insert so the example's tests can assert the *data* is
    reproducible without needing PostgreSQL — the determinism claim is about
    this function, and it is checkable in milliseconds.
    """
    rng = random.Random(SEED)
    rows: dict[str, list[tuple[Any, ...]]] = {}

    rows["reserves"] = [
        (rid, name, slug, tz, hectares, _at(0, 9, 0))
        for rid, name, slug, tz, hectares in RESERVES
    ]

    #: 12 stations per reserve; the first two of each are sensitive, which puts
    #: 8 sensitive stations across 4 reserves -- deliberately more than the
    #: design's 6, because two reserves need a sensitive station each for the
    #: authorization tests to have a cross-reserve case.
    stations: list[tuple[Any, ...]] = []
    for reserve_id, *_ in RESERVES:
        for offset in range(12):
            station_id = (reserve_id - 1) * 12 + offset + 1
            stations.append((
                station_id,
                reserve_id,
                f"{RESERVES[reserve_id - 1][2].split('-')[0].title()} {offset + 1:02d}",
                Decimal(f"{-1.5 + reserve_id * 0.7 + offset * 0.011:.6f}"),
                Decimal(f"{36.2 + reserve_id * 1.3 + offset * 0.013:.6f}"),
                HABITATS[offset % len(HABITATS)],
                offset < 2,
            ))
    rows["stations"] = stations

    #: station id -> its reserve's zone, so a capture is generated on the wall
    #: clock the camera actually reads.
    zones: dict[int, ZoneInfo] = {
        station[0]: ZoneInfo(RESERVES[station[1] - 1][3]) for station in stations
    }

    #: 61 cameras over 48 stations: 13 stations have had a replacement, so a
    #: station's history crosses a device change.
    cameras: list[tuple[Any, ...]] = []
    camera_id = 0
    for station_id, *_ in stations:
        camera_id += 1
        cameras.append((
            camera_id, station_id, f"CT-{camera_id:05d}",
            CAMERA_MODELS[camera_id % len(CAMERA_MODELS)],
            #: Three days before the first capture. Captures are generated on
            #: local wall clocks, so a +03 reserve's day-zero midnight is
            #: 21:00 UTC the day before -- deploying at day zero would leave
            #: sightings that predate their own camera.
            _at(-3, 10, 0), None, rng.randrange(28, 100), "3.2.1",
        ))
    for station_id in range(1, 14):
        camera_id += 1
        replaced_on = 200 + station_id * 7
        cameras[station_id - 1] = (*cameras[station_id - 1][:5], _at(replaced_on, 11, 0),
                                   *cameras[station_id - 1][6:])
        cameras.append((
            camera_id, station_id, f"CT-{camera_id:05d}",
            CAMERA_MODELS[camera_id % len(CAMERA_MODELS)],
            _at(replaced_on, 12, 0), None, rng.randrange(40, 100), "4.0.0",
        ))
    rows["cameras"] = cameras

    #: station id -> [(deployed_at, retired_at, camera_id)], so a sighting is
    #: attributed to the device that was actually hanging there at the time.
    #: Getting this wrong is not cosmetic: it would leave every replacement
    #: camera with zero sightings, and the station-survives-the-device story --
    #: the reason these are two tables -- would be untrue in the data.
    fleet: dict[int, list[tuple[Any, Any, int]]] = {}
    for cam_id, station_id, _serial, _model, deployed, retired, *_ in cameras:
        fleet.setdefault(station_id, []).append((deployed, retired, cam_id))
    for entries in fleet.values():
        entries.sort()

    rows["species"] = [
        (index + 1, code, common, scientific, protection, nocturnal)
        for index, (code, common, scientific, protection, nocturnal) in enumerate(SPECIES)
    ]

    observers: list[tuple[Any, ...]] = []
    observer_id = 0
    for role, count in _ROLES:
        for n in range(count):
            observer_id += 1
            reserve = None if role == "researcher" else (observer_id % 4) + 1
            observers.append((
                observer_id, f"{role}{n + 1}@example.org",
                f"{role.title()} {n + 1}", role, reserve,
            ))
    rows["observers"] = observers

    #: Volunteers and rangers see one reserve; researchers see all four.
    assignments: list[tuple[Any, ...]] = []
    for oid, _email, _name, role, reserve in observers:
        if role == "researcher":
            for reserve_id, *_ in RESERVES:
                assignments.append((oid, reserve_id, "read"))
        else:
            assignments.append((oid, reserve, "ranger" if role == "ranger" else "review"))
    rows["assignments"] = assignments

    #: Collection trips: every station is visited roughly every 46 days across
    #: the 18 months, which is what a real network manages. That cadence is what
    #: makes the *late* ones legible -- a card whose last image is three weeks
    #: older than its collection stands out only against a background of cards
    #: collected within a day or two of their last capture.
    #:
    #: Eleven are deliberately late: the camera died early, or the trip slipped.
    deployments: list[tuple[Any, ...]] = []
    #: station id -> [(collected_at, deployment_id)], ascending. Sightings are
    #: attributed to the first collection at or after their capture.
    collections: dict[int, list[tuple[datetime.datetime, int]]] = {}
    deployment_id = 0
    late_ids = {37, 88, 145, 201, 266, 310, 377, 424, 480, 522, 561}
    for station_id in range(1, 49):
        for visit in range(12):
            deployment_id += 1
            collected_day = visit * 46 + 40 + (station_id % 11)
            if deployment_id in late_ids:
                collected_day += rng.randrange(8, 27)
            collected = _at(collected_day, 14, 30)
            deployments.append((
                deployment_id, station_id, collected, f"SD-{deployment_id:04d}",
                rng.randrange(120, 4200),
                _at(collected_day, 18, 0) if deployment_id % 9 else None,
            ))
            collections.setdefault(station_id, []).append((collected, deployment_id))
    rows["deployments"] = deployments

    #: The fact table.
    species_rows = rows["species"]
    sighting_rows: list[tuple[Any, ...]] = []
    for n in range(1, sightings + 1):
        station_id = rng.randrange(1, 49)
        species = species_rows[rng.randrange(0, len(species_rows))]
        day = rng.randrange(0, DAYS)
        hour = _activity_hour(rng, species[5])
        captured = _local(zones[station_id], day, hour,
                          rng.randrange(0, 60), rng.randrange(0, 60))
        #: The device hanging there at the time, which is not always the first.
        camera_at = next(
            (cid for deployed, retired, cid in fleet[station_id]
             if deployed <= captured and (retired is None or captured < retired)),
            fleet[station_id][0][2],
        )
        #: The first card collected at or after this capture. ``None`` means the
        #: card is still in the field -- a real state, and the one a chart has
        #: to be honest about not knowing.
        card = next(
            (did for collected, did in collections[station_id] if collected >= captured),
            None,
        )
        #: Upload follows collection, not capture. That gap is the whole point.
        uploaded = (
            deployments[card - 1][2] + datetime.timedelta(hours=rng.randrange(2, 30))
            if card is not None
            else captured + datetime.timedelta(days=60)
        )
        sighting_rows.append((
            n, station_id, camera_at, species[0], card, captured, uploaded,
            rng.randrange(41, 100),
            f"images/{station_id:02d}/{n:07d}.jpg",
            f"thumbs/{station_id:02d}/{n:07d}.jpg" if n % 3 else None,
            rng.randrange(1, 25) if n % 4 else None,
            _weighted(rng, REVIEW_STATES),
            {"batch": (n // 5000) + 1},
            None,
        ))
    rows["sightings"] = sighting_rows

    #: A by-product of seeded restricted-location reads: rangers looking at
    #: rhino and pangolin sightings.
    restricted = {row[0] for row in species_rows if row[4] == "restricted"}
    rangers = [row[0] for row in observers if row[3] == "ranger"]
    audit: list[tuple[Any, ...]] = []
    for row in sighting_rows:
        if row[3] in restricted and len(audit) < 600:
            audit.append((
                len(audit) + 1, rangers[len(audit) % len(rangers)], row[0],
                "viewed_location", row[5] + datetime.timedelta(days=3),
            ))
    rows["audit_entries"] = audit
    return rows


#: Column order per table, matching :func:`build_rows`.
COLUMNS: dict[str, tuple[str, ...]] = {
    "reserves": ("id", "name", "slug", "timezone", "area_hectares", "created_at"),
    "stations": ("id", "reserve_id", "name", "latitude", "longitude", "habitat", "sensitive"),
    "cameras": ("id", "station_id", "serial", "model", "deployed_at", "retired_at",
                "battery_pct", "firmware"),
    "species": ("id", "code", "common_name", "scientific_name", "protection", "nocturnal"),
    "observers": ("id", "email", "display_name", "role", "reserve_id"),
    "assignments": ("observer_id", "reserve_id", "level"),
    "deployments": ("id", "station_id", "collected_at", "card_serial", "image_count",
                    "ingested_at"),
    "sightings": ("id", "station_id", "camera_id", "species_id", "deployment_id",
                  "captured_at", "uploaded_at", "confidence", "image_key",
                  "thumbnail_key", "identified_by", "review_state", "tags", "notes"),
    "audit_entries": ("id", "observer_id", "sighting_id", "action", "at"),
}

#: Insert order: a table's referenced tables must already hold their rows.
ORDER = (
    "reserves", "stations", "cameras", "species", "observers",
    "assignments", "deployments", "sightings", "audit_entries",
)


def _insert_sql(table: str, columns: tuple[str, ...], count: int) -> str:
    width = len(columns)
    names = ", ".join(f'"{c}"' for c in columns)
    tuples = ", ".join(
        "(" + ", ".join(f"${row * width + col + 1}" for col in range(width)) + ")"
        for row in range(count)
    )
    return f'INSERT INTO "{SCHEMA}"."{table}" ({names}) VALUES {tuples}'


async def seed(connection: Any, *, sightings: int = 140_000) -> dict[str, int]:
    """Insert every row and return the count written per table.

    Idempotent by truncation: the tables are emptied first, so re-seeding is
    the same operation rather than a duplicate-key error.
    """
    import json

    rows = build_rows(sightings=sightings)
    await connection.execute(
        'TRUNCATE ' + ", ".join(f'"{SCHEMA}"."{t}"' for t in ORDER) + " RESTART IDENTITY CASCADE"
    )
    written: dict[str, int] = {}
    for table in ORDER:
        columns = COLUMNS[table]
        data = rows[table]
        for start in range(0, len(data), BATCH):
            chunk = data[start:start + BATCH]
            flat: list[Any] = []
            for row in chunk:
                for column, value in zip(columns, row, strict=True):
                    flat.append(json.dumps(value) if column == "tags" else value)
            await connection.execute(_insert_sql(table, columns, len(chunk)), *flat)
        written[table] = len(data)
    #: Planner statistics, so `n_live_tup` and `reltuples` tell the truth
    #: immediately. Without this a freshly seeded table reports -1 rows, which
    #: is what a progress denominator reads.
    await connection.execute(f'ANALYZE "{SCHEMA}"."sightings"')
    for table in ORDER:
        if table != "sightings":
            await connection.execute(f'ANALYZE "{SCHEMA}"."{table}"')
    return written
