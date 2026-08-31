"""Seven routes: one in, five out, and one that never quite ends.

The camera-trap example teaches routing, nested routers, cursor paging, generated
CRUD and the read API. None of that is repeated here. What these handlers are
for is the four things this example is about, and each route exists because one
of them needs somewhere to happen:

* ``POST /ingest/positions`` -- binary in, binary out, and a malformed body that
  is a 400 rather than a 500.
* ``GET /fixes/near`` and ``GET /fixes/nearest`` -- the two halves of a
  proximity search, and the tier-1 answer to a nearest-neighbour question.
* ``GET /animals/{id}/track`` -- a `Trajectory`, and the place where precision
  stops being a policy and becomes a number on the wire.
* ``GET /animals/{id}/daily`` -- the sealed view, and its corrections.
* ``GET /live/positions`` -- the same precision decision, applied per frame to
  a stream instead of per key to a document.

**Every handler asks for the precision grid once.** `precision_grid` evaluates
three Cedar decisions per protection tier; a list of two hundred fixes spans at
most three tiers, so asking once per request rather than once per row is three
evaluations instead of six hundred, and it is only safe because the decision is
a function of the tier and the identity and nothing else. That property is
asserted in `tests/tracking/test_policy.py`, not assumed here.

**The routes are built by a function.** `routers(live)` closes over the live map
rather than reaching for a module-level singleton, because the map owns open
connections and hidden global state that owns connections is the kind that
outlives a test. It is the same reason the camera-trap example mounts its upload
routes from `build` instead of importing them.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Any

from wreath import Request, Router
from wreath.auth import authenticated, identify
from wreath.binding import Query
from wreath.exceptions import BadRequest, NotFound
from wreath.geospatial import Coordinate, GeospatialError
from wreath.orm import FromORM, Session
from wreath.protobuf import ProtobufDecodeError, decode, encode
from wreath.response import Response, SSEResponse
from wreath.series import Range
from wreath.temporal import from_wall_clock, zone

from .config import CONSERVANCY_ZONE
from .ingest import IngestRefused, accept
from .live import LiveMap
from .models import Animal, Fix, Landmark
from .place import SearchTooBroad, nearest, nearest_landmark, track, within
from .policies import precision_grid
from .views import daily_distance
from .wire import (
    MEDIA_TYPE_HEADER,
    BatchReceipt,
    PositionBatch,
    fix_json,
    landmark_json,
    milliseconds,
)

#: One session per request, leased only if a handler touches it.
ReadSession = Annotated[Session, FromORM("main", workload="read")]
WriteSession = Annotated[Session, FromORM("main", workload="write")]

#: The widest proximity search one request may ask for. Past this a caller is
#: asking for the whole conservancy and should ask for the whole conservancy.
MAX_RADIUS_M = 50_000

#: The longest track one request may ask for. A collar reporting every twenty
#: minutes produces about 2,200 fixes in a month, which is a response somebody
#: can actually draw.
MAX_TRACK_DAYS = 31


def routers(live: LiveMap) -> tuple[Router, ...]:
    """Every route, bound to one live map. Included by `tracking.app.build`."""
    animals = Router(prefix="/animals", tags=("animals",))
    fixes = Router(prefix="/fixes", tags=("fixes",))
    stream = Router(prefix="/live", tags=("live",))
    relay = Router(prefix="/ingest", tags=("ingest",))

    @relay.post("/positions", summary="A field station relays a batch of positions")
    @authenticated()
    async def ingest_positions(request: Request, session: WriteSession) -> Response:
        """Decode a protobuf batch, land it, and answer in protobuf.

        **The refusal matters as much as the parse.** Truncation, a length
        prefix past the end of the buffer, a varint longer than ten bytes,
        invalid UTF-8 in the relay name -- every one of them raises
        `ProtobufDecodeError`, so one `except` covers the lot and turns it into
        a 400. Letting them surface as a 500 tells a retrying station to try
        again, and a satellite relay will, forever, at the rate its spool
        refills.

        Note what is *not* here: no annotation binds this body. `wreath.protobuf`
        is a codec and not a content negotiator, so the bytes are read and
        decoded by hand. That is the shape
        the protobuf decoder expects.
        """
        body = await request.body()
        try:
            batch = decode(PositionBatch, body)
        except ProtobufDecodeError as error:
            raise BadRequest(f"malformed position batch: {error}") from error

        try:
            receipt = await accept(session, batch, now=datetime.datetime.now(tz=datetime.UTC))
        except IngestRefused as error:
            raise BadRequest(str(error)) from error

        # After the commit, never before: a broadcast for a batch that then
        # failed to land would put positions on every open map that are in no
        # table. The live map is a view of what happened, so it follows the
        # write rather than racing it.
        await live.publish(receipt.published)

        return Response(
            encode(
                BatchReceipt(
                    accepted=receipt.accepted,
                    rejected=receipt.rejected,
                    watermark_ms=(milliseconds(receipt.watermark) if receipt.watermark else 0),
                )
            ),
            media_type=MEDIA_TYPE_HEADER,
        )

    @animals.get("/", summary="The collared animals")
    @identify()
    async def list_animals(request: Request, session: ReadSession) -> dict:
        """The roster, and what each animal's positions cost to see.

        Public, and the protection tier is on the wire. A conservancy announces
        its rhinos -- that they exist is not the secret and pretending otherwise
        would make the programme unfundable. *Where* they are is the secret, and
        that is what every other route in this file is careful about.

        `precision_m` here is what this caller would get, so a client can grey
        out a map layer it is not going to be able to draw rather than
        discovering it per fix.
        """
        grid = precision_grid(request.identity)
        found = await session.fetch(Animal.select().order_by(Animal.id))
        return {
            "items": [
                {
                    "id": animal.id,
                    "name": animal.name,
                    "taxon": animal.taxon,
                    "protection": animal.protection,
                    **(
                        {"precision_m": grid[animal.protection].metres}
                        if grid[animal.protection] is not None
                        else {}
                    ),
                }
                for animal in found
            ]
        }

    @animals.get("/{animal_id}/track", summary="One animal's path through a window")
    @identify()
    async def read_track(
        request: Request,
        animal_id: int,
        session: ReadSession,
        since: datetime.date,
        days: Annotated[int, Query(minimum=1, maximum=MAX_TRACK_DAYS)] = 7,
    ) -> dict:
        """The fixes, and the measures a `Trajectory` derives from them.

        `distance_m` is the sum of the legs and never the straight line from
        first fix to last: an animal that walks a circuit back to the waterhole
        it started at still walked all day, and a straight-line figure would
        report zero.

        **The summary is not degraded and the fixes are.** How far something
        walked is not a location, and coarsening it would destroy the one number
        the welfare question actually turns on while protecting nothing --
        knowing that a rhino covered 11 km yesterday tells a poacher where it is
        to within the whole conservancy, which is where it already was. The
        *positions* are what carry the risk, and they are what `fix_json`
        coarsens.
        """
        animal = await session.fetch_one(Animal.select().where(Animal.id == animal_id))
        if animal is None:
            raise NotFound(f"no animal {animal_id}")
        start, end = _window(since, days)
        path = await track(session, animal_id, since=start, until=end)
        precision = precision_grid(request.identity)[animal.protection]
        rows = await session.fetch(
            Fix.select()
            .where(
                Fix.animal_id == animal_id,
                Fix.recorded_at >= start,
                Fix.recorded_at < end,
            )
            .order_by(Fix.recorded_at)
        )
        return {
            "animal": {"id": animal.id, "name": animal.name, "taxon": animal.taxon},
            "window": {"since": start.isoformat(), "until": end.isoformat()},
            "distance_m": round(path.distance, 1),
            "duration_s": path.duration,
            "speed_ms": None if path.speed is None else round(path.speed, 4),
            "fixes": [fix_json(row, precision=precision) for row in rows],
        }

    @animals.get("/{animal_id}/daily", summary="Distance per local day, sealed")
    @identify()
    async def read_daily(
        request: Request,
        animal_id: int,
        session: ReadSession,
        since: datetime.date,
        days: Annotated[int, Query(minimum=1, maximum=MAX_TRACK_DAYS)] = 14,
    ) -> dict:
        """The daily chart, and which of its days carry a correction.

        `corrections` is on the wire on purpose. A chart that quietly folds a
        late arrival into a settled day is a chart whose numbers change between
        two readers looking at it, and neither of them can tell. Naming the days
        that moved is what makes late data legible as late data.

        Every day in the range is present even when the animal produced no
        fixes: `fixes` is `0` and `distance_m` is `0` for a day the collar was
        silent, because that is a fact, where a missing row would let every
        caller reinvent the same interpolation slightly differently.
        """
        animal = await session.fetch_one(Animal.select().where(Animal.id == animal_id))
        if animal is None:
            raise NotFound(f"no animal {animal_id}")
        start, end = _window(since, days)
        view = daily_distance(CONSERVANCY_ZONE)
        result = await view.run(session, range=Range(start, end), animal=animal_id)
        # Keyed by measure name rather than by position. `series` is ordered by
        # the declaration, so indexing it works right up until somebody adds a
        # third measure in the middle and every chart silently swaps two lines.
        values = {item.measure: item.values for item in result.series}
        return {
            "animal_id": animal_id,
            "zone": CONSERVANCY_ZONE,
            "days": [
                {
                    "day": bucket.isoformat(),
                    "fixes": values["fixes"][position],
                    "distance_m": _metres(values["distance_m"][position]),
                }
                for position, bucket in enumerate(result.buckets)
            ],
            "sealed_through": (
                None
                if result.state is None or result.state.sealed_through is None
                else result.state.sealed_through.isoformat()
            ),
            "corrections": [
                day.isoformat()
                for day in (() if result.state is None else result.state.corrections)
            ],
        }

    @fixes.get("/near", summary="Every fix within a radius of a point")
    @identify()
    async def read_near(
        request: Request,
        session: ReadSession,
        lat: float,
        lon: float,
        metres: Annotated[float, Query(minimum=1, maximum=MAX_RADIUS_M)] = 5_000.0,
    ) -> dict:
        """The proximity query, with both of its halves.

        A rectangle narrows -- that is the part an index can answer -- and then
        the exact great-circle test decides. `tracking.place.within` is where
        both live and where the reasoning is.

        A caller whose latitude and longitude are the wrong way round gets a 422
        naming the field rather than an empty list, because
        `wreath.geospatial.Coordinate` refuses a longitude past 90 and this
        handler does not catch that into a shrug.
        """
        grid = precision_grid(request.identity)
        centre = _point(lat, lon)
        try:
            found = await within(session, centre, metres)
        except SearchTooBroad as error:
            # A 400 rather than a truncated page. Answering with an arbitrary
            # subset of the rectangle and calling it "within 40 km" would be a
            # wrong answer that looks exactly like a right one.
            raise BadRequest(str(error)) from error
        landmarks = await session.fetch(Landmark.select().order_by(Landmark.id))
        animals = await _animals(session, {row.animal_id for row in found})
        return {
            "centre": {"lat": centre.lat, "lon": centre.lon},
            "metres": metres,
            "items": [_fix_with_landmark(row, animals, landmarks, grid) for row in found],
        }

    @fixes.get("/nearest", summary="The fixes closest to a point")
    @identify()
    async def read_nearest(
        request: Request,
        session: ReadSession,
        lat: float,
        lon: float,
        count: Annotated[int, Query(minimum=1, maximum=50)] = 5,
    ) -> dict:
        """Nearest-neighbour, by widening a circle rather than walking an index.

        There is no KNN index on a pair of ordinary columns, so this doubles a
        radius until enough fixes fall inside it or the search gives up. See
        `tracking.place.nearest` for what that costs and why the answer can
        legitimately come back short.
        """
        grid = precision_grid(request.identity)
        centre = _point(lat, lon)
        found = await nearest(session, centre, count=count)
        landmarks = await session.fetch(Landmark.select().order_by(Landmark.id))
        animals = await _animals(session, {row.animal_id for row in found})
        return {
            "centre": {"lat": centre.lat, "lon": centre.lon},
            "items": [_fix_with_landmark(row, animals, landmarks, grid) for row in found],
        }

    @stream.get("/positions", summary="The live map")
    @identify()
    async def live_positions(request: Request) -> SSEResponse:
        """One `EventSource`, framed at this reader's own resolution.

        The grid is computed **here**, once, when the stream opens, and held for
        its life. Every event this reader is ever sent is coarsened by that
        grid, and a reader beside them on the same room with a different grid
        gets a different number out of the same broadcast. That is the whole
        composition this example exists to prove, and
        `tests/tracking/test_live.py` is where it is proved.

        No database session is taken. A stream that held a connection for as
        long as a browser tab is open is a pool that empties over an afternoon,
        and there is nothing to read: every position on this channel arrives
        from the ingest path, not from a query.
        """
        subscriber = await live.subscribe(precision_grid(request.identity))

        async def events():
            try:
                async for event in subscriber.events():
                    yield event
            finally:
                # Reached when the client disconnects, when the stream is
                # closed on shutdown, and when the generator is discarded --
                # which is why leaving the room lives here and not after the
                # loop. A subscriber left in a room is a broadcast that fills a
                # queue nobody drains, forever.
                await live.unsubscribe(subscriber)

        return SSEResponse(events())

    return (relay, animals, fixes, stream)


def _window(since: datetime.date, days: int) -> tuple[datetime.datetime, datetime.datetime]:
    """The half-open instant range a local date and a day count actually mean.

    The end is the local midnight `days` later rather than the start plus
    `days * 24h`. Africa/Nairobi has no daylight saving so the two agree here
    today -- and this is the version that stays right when the programme adds a
    site somewhere that does, which is the only reason to write it this way
    before you need it.
    """
    tz = zone(CONSERVANCY_ZONE)
    midnight = datetime.time()
    start = from_wall_clock(datetime.datetime.combine(since, midnight), tz)
    end = from_wall_clock(
        datetime.datetime.combine(since + datetime.timedelta(days=days), midnight), tz
    )
    return start, end


def _point(lat: float, lon: float) -> Coordinate:
    """Two query parameters as a coordinate, refusing an impossible one.

    `Coordinate` is keyword-only and this call names both, which is the whole
    of what that refusal buys: a reader of this line can see which is which, and
    a future edit that swaps them cannot be silent.
    """
    try:
        return Coordinate(lat=lat, lon=lon)
    except GeospatialError as error:
        raise BadRequest(str(error)) from error


async def _animals(session: Session, animal_ids: set[int]) -> dict[int, Animal]:
    """The animals a page of fixes refers to, in one query rather than per row."""
    if not animal_ids:
        return {}
    found = await session.fetch(Animal.select().where(Animal.id.in_(sorted(animal_ids))))
    return {animal.id: animal for animal in found}


def _fix_with_landmark(
    row: Fix,
    animals: dict[int, Animal],
    landmarks: list[Landmark],
    grid: dict[str, Any],
) -> dict[str, Any]:
    """One fix, plus the landmark it is nearest -- but only at full resolution.

    The gate is the point. A distance to a published waterhole is very nearly a
    position, so attaching one to a coarsened coordinate would undo the
    coarsening completely. See `tracking.wire.landmark_json`.
    """
    animal = animals.get(row.animal_id)
    precision = grid.get(animal.protection) if animal is not None else None
    body = fix_json(row, precision=precision, animal=None if animal is None else animal.name)
    if precision is not None and precision.metres == 0.0:
        found = nearest_landmark(Coordinate(lat=row.latitude, lon=row.longitude), landmarks)
        if found is not None:
            mark, metres = found
            body["nearest"] = landmark_json(mark.name, mark.kind, metres)
    return body


def _metres(value: Any) -> float | None:
    """A summed `float8` for the wire.

    `sum()` over a `float8` column is a `float8`, so this is a rounding rule
    rather than a conversion -- but a day with no fixes sums to `None` rather
    than `0.0`, and a chart that read that as zero would be claiming the animal
    stood still rather than that the collar was silent. Zero *is* the honest
    reading here, because a day the collar produced no fixes contributed no
    legs, and the fix count beside it says which of the two happened.
    """
    return 0.0 if value is None else round(float(value), 1)
