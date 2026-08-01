"""Accepting a batch of positions: decode, refuse, land, repair, announce.

This is the one path in the example that writes, and it does five things in an
order that matters.

1. **Decode.** `wreath.protobuf` turns bytes into a `PositionBatch`, or raises
   `ProtobufDecodeError` -- one exception covering truncation, a length prefix
   past the end of the buffer, a varint longer than ten bytes, and invalid
   UTF-8. The router turns that into a 400. Letting it surface as a 500 would
   tell a retrying station to try again, and it will, forever.

2. **Refuse the individual position, not the batch.** A collar with a corrupt
   almanac reporting a latitude of 320 degrees must not stop the other
   thirty-nine collars in the same upload from being recorded. Those positions
   are dropped and *counted* in the receipt, so a station operator can see one
   collar going wrong rather than discovering it a season later.

3. **Land it idempotently.** `(collar_id, recorded_at)` is the primary key and
   the insert is `ON CONFLICT DO NOTHING`, so a station whose upload timed out
   after the write can retry the whole batch and land nothing. That property is
   in the schema rather than in a flag somebody remembered to check.

4. **Repair the legs.** `Fix.leg_m` is the distance from the previous fix, and
   a batch that arrives *late* lands in the middle of history -- so the fix that
   used to follow the gap has a leg measured across it. See
   :func:`repair_legs`; this is the whole reason yesterday's distance can change
   after yesterday ended, which is what the sealed daily view in
   `tracking.views` then has to report honestly rather than rewrite.

5. **Announce it.** One broadcast per batch onto the live map's room.

## The framework edge this path works around

`wreath.protobuf` is a codec, and it is *only* a codec: nothing maps
`application/x-protobuf` to it the way `application/json` is mapped to the JSON
serializer, and no annotation binds a request body into a `@message` class the
way one binds a dataclass. So this handler reads `await request.body()` and
calls `decode` itself, which is exactly what
`docs/cookbook/recipes/accept-a-protobuf-body.md` prescribes -- the recipe is
the sanctioned shape, not a workaround for a gap. It is worth knowing before you
look for a `Body(...)` spelling that is not there.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from wreath.geospatial import Coordinate, GeospatialError, distance

from .config import SCHEMA
from .models import Collar
from .wire import Position, PositionBatch, instant

#: Positions one request may carry. A station draining a week's spool for forty
#: collars reporting every twenty minutes has about 20,000 positions, so this is
#: "one day of one relay" and a longer outage drains over several requests.
#: Unbounded would mean one collar's failure mode is a request that allocates
#: until the worker dies.
MAX_POSITIONS = 5_000

#: Rows per INSERT. The driver caps one outbound operation at 256 KiB and the
#: *statement text* dominates that -- every placeholder past `$9999` is six
#: characters, times eleven columns. The camera-trap seeder's note has the full
#: arithmetic; 250 keeps this table's packet comfortably inside the cap.
BATCH = 250


class IngestRefused(ValueError):
    """The batch as a whole cannot be accepted.

    A `ValueError` so a router can turn it into a 4xx alongside
    `ProtobufDecodeError`, which is also one. Individual bad positions do *not*
    raise this -- they are counted; see the module docstring.
    """


@dataclass(frozen=True, slots=True)
class Receipt:
    """What one batch did. The protobuf reply is built from this."""

    accepted: int
    rejected: int
    watermark: datetime.datetime | None
    #: The exact positions, for the live map. Exact because degradation happens
    #: at each reader's edge -- see `tracking.live`.
    published: list[dict[str, Any]]


async def accept(
    session: Any,
    batch: PositionBatch,
    *,
    now: datetime.datetime,
) -> Receipt:
    """Land `batch`, repair the affected legs, and report what happened.

    `now` is a parameter rather than a call to the clock, because
    `Fix.received_at` is the one column in the schema that a test cannot
    predict, and an example whose pasted output moves every time it runs is not
    reproducible. Production passes `datetime.now(UTC)`; the seeder and the
    tests pass a fixed instant.

    Args:
        session: An ORM write session.
        batch: A decoded `PositionBatch`.
        now: What to record as `received_at` for every position in it.

    Returns:
        A `Receipt`.

    Raises:
        IngestRefused: the batch names no relay, or carries more than
            `MAX_POSITIONS` positions.
    """
    if not batch.relay:
        raise IngestRefused("a batch must name the relay that forwarded it")
    if len(batch.positions) > MAX_POSITIONS:
        raise IngestRefused(
            f"{len(batch.positions)} positions in one batch; the limit is "
            f"{MAX_POSITIONS}, so drain a long outage over several requests"
        )

    fleet = await _fleet(session, {position.collar_id for position in batch.positions})
    rows: list[tuple[Any, ...]] = []
    published: list[dict[str, Any]] = []
    rejected = 0
    for position in batch.positions:
        collar = fleet.get(position.collar_id)
        point = _coordinate(position)
        if collar is None or point is None:
            rejected += 1
            continue
        recorded_at = instant(position.recorded_at_ms)
        rows.append(
            (
                position.collar_id,
                recorded_at,
                collar.animal_id,
                now,
                point.lat,
                point.lon,
                position.accuracy_m,
                position.battery_pct,
                None,
                batch.relay,
                position.satellites if position.satellites is not None else 0,
            )
        )
        published.append(
            {
                "animal_id": collar.animal_id,
                "animal": collar.animal.name,
                "protection": collar.animal.protection,
                "collar_id": position.collar_id,
                "recorded_at": recorded_at.isoformat(),
                "battery_pct": position.battery_pct,
                "lat": point.lat,
                "lon": point.lon,
            }
        )

    if not rows:
        return Receipt(0, rejected, None, [])

    # Deduplicated *before* the statement, not just by the index. PostgreSQL
    # refuses `ON CONFLICT DO NOTHING` for two rows colliding inside one
    # command -- "cannot affect row a second time" -- so a station that sent
    # the same position twice in one batch would fail the whole request rather
    # than landing it once, which is the opposite of what the conflict clause
    # is here for.
    unique: dict[tuple[int, Any], tuple[Any, ...]] = {}
    for row in rows:
        unique.setdefault((row[0], row[1]), row)
    rows = list(unique.values())

    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        await session.raw(_insert_sql(len(chunk)), *_flatten(chunk)).execute()

    # Legs are repaired per animal from the earliest position that landed for
    # it, which for an ordinary live batch is the batch itself and for a buffer
    # dump is however far back the collar had been holding.
    earliest: dict[int, Any] = {}
    for row in rows:
        animal_id, recorded_at = row[2], row[1]
        if animal_id not in earliest or recorded_at < earliest[animal_id]:
            earliest[animal_id] = recorded_at
    for animal_id, since in earliest.items():
        await repair_legs(session, animal_id, since=since)

    await session.flush()
    return Receipt(len(rows), rejected, max(row[1] for row in rows), published)


async def repair_legs(session: Any, animal_id: int, *, since: Any) -> int:
    """Recompute `Fix.leg_m` for one animal, from `since` forward.

    **Why this exists at all.** A leg is the distance from the previous fix, and
    "previous" is decided by `recorded_at`, not by arrival. When a collar that
    lost the sky for three days uploads its buffer, the rows land *between* rows
    that are already there -- and the fix that used to come straight after the
    gap now has a leg measured across three days of walking that has since been
    filled in. Leaving it would make the animal's total distance count that
    stretch twice.

    So the repair starts one fix *before* `since`, which is the only way to give
    the first repaired fix a predecessor to measure from, and walks forward.

    **The distance is computed in Python, not in SQL,** and that is a decision
    rather than a shortcut. Writing the haversine formula into this `UPDATE`
    would give two implementations of "how far apart", and the stored column and
    `wreath.geospatial.Trajectory` would be free to disagree by a rounding rule
    nobody would notice for a year. `tests/tracking/test_ingest.py` asserts they
    agree, and they can only agree because there is one of them.

    The cost is one round trip per animal per batch and a read of everything
    from `since` forward. For a live batch that is a handful of rows. For a
    week-old buffer dump it is a week of that animal's fixes, once -- which is
    the honest price of a stored derivation, and is why `Fix.leg_m`'s docstring
    says it is maintained by this path.

    Returns:
        How many fixes had their leg rewritten.
    """
    rows = await (
        session.raw(
            f'SELECT collar_id, recorded_at, latitude, longitude FROM "{SCHEMA}"."fixes" '
            "WHERE animal_id = $1 AND recorded_at >= COALESCE("
            f'  (SELECT max(recorded_at) FROM "{SCHEMA}"."fixes" '
            "   WHERE animal_id = $1 AND recorded_at < $2), $2) "
            "ORDER BY recorded_at, collar_id",
            animal_id,
            since,
        ).fetch()
    )
    if not rows:
        return 0

    legs: list[tuple[int, Any, float | None]] = []
    previous: Coordinate | None = None
    for collar_id, recorded_at, latitude, longitude in rows:
        here = Coordinate(lat=latitude, lon=longitude)
        # The row before `since` is read only to give the next one a
        # predecessor; its own leg was already right and is not rewritten.
        if recorded_at >= since:
            legs.append((collar_id, recorded_at, None if previous is None else
                         distance(previous, here)))
        previous = here

    for start in range(0, len(legs), BATCH):
        chunk = legs[start : start + BATCH]
        await session.raw(_update_legs_sql(len(chunk)), *_flatten(chunk)).execute()
    return len(legs)


async def _fleet(session: Any, collar_ids: set[int]) -> dict[int, Collar]:
    """The collars named by a batch, with their animals, in one query.

    `load="raise"` on the relationship means a handler that forgot to include
    the animal gets an exception rather than a query per collar, which for a
    two-hundred-position batch is the difference between two statements and two
    hundred and one.
    """
    if not collar_ids:
        return {}
    found = await session.fetch(
        Collar.select()
        .where(Collar.id.in_(sorted(collar_ids)))
        .include(Collar.animal.joined())
    )
    return {collar.id: collar for collar in found}


def _coordinate(position: Position) -> Coordinate | None:
    """`position` as a `Coordinate`, or None if it cannot be one.

    `Coordinate` refuses a latitude past the pole and a longitude past the
    antimeridian, raising `GeospatialError` -- a `ValueError`, which the binding
    layer would render as a 422 if this were a bound body. It is not: this is a
    field inside a protobuf message that has otherwise parsed perfectly, and one
    bad reading is not a bad request. Catching it here is what turns "this
    collar is confused" into a number in the receipt instead of a 4xx for the
    thirty-nine collars that were fine.
    """
    try:
        return Coordinate(lat=position.lat, lon=position.lon)
    except GeospatialError:
        return None


_COLUMNS = (
    "collar_id", "recorded_at", "animal_id", "received_at", "latitude",
    "longitude", "accuracy_m", "battery_pct", "leg_m", "relay", "satellites",
)


def _insert_sql(count: int) -> str:
    width = len(_COLUMNS)
    names = ", ".join(f'"{name}"' for name in _COLUMNS)
    tuples = ", ".join(
        "(" + ", ".join(f"${row * width + col + 1}" for col in range(width)) + ")"
        for row in range(count)
    )
    return (
        f'INSERT INTO "{SCHEMA}"."fixes" ({names}) VALUES {tuples} '
        "ON CONFLICT (collar_id, recorded_at) DO NOTHING"
    )


def _update_legs_sql(count: int) -> str:
    """One `UPDATE ... FROM (VALUES ...)` for a chunk of recomputed legs.

    The casts on the first tuple are not decoration: a `VALUES` list gives
    PostgreSQL nothing to infer a parameter's type from, and an untyped `$3`
    that is `NULL` in the first row -- which it is, for an animal's very first
    fix -- infers as `text` and then fails to compare against a `float8` column.
    """
    tuples = ", ".join(
        "("
        + ", ".join(
            f"${row * 3 + col + 1}" + ("::bigint", "::timestamptz", "::float8")[col]
            if row == 0
            else f"${row * 3 + col + 1}"
            for col in range(3)
        )
        + ")"
        for row in range(count)
    )
    return (
        f'UPDATE "{SCHEMA}"."fixes" AS f SET "leg_m" = v.leg '
        f"FROM (VALUES {tuples}) AS v(collar_id, recorded_at, leg) "
        "WHERE f.collar_id = v.collar_id AND f.recorded_at = v.recorded_at"
    )


def _flatten(rows: list[Any]) -> list[Any]:
    return [value for row in rows for value in row]
