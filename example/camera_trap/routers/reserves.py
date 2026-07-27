"""Reserves, their stations, and what a station recorded.

The URL is the domain's own hierarchy: a reserve owns stations, a station owns
sightings and the collection trips that carried them off the card. Two routers
build it — ``stations`` carries the ``/{slug}/stations`` prefix and is included
into ``reserves`` — so no handler in this file spells the whole path.

Every handler that names a station resolves the reserve first and then the
station *within that reserve*. That is one extra query and it is not
optimisation-shy: ``/reserves/nullarbor/stations/3`` must be a 404 when station
3 belongs to Olkiramatian, or the URL's hierarchy is decoration and any station
can be reached from any reserve's path.
"""

from __future__ import annotations

import datetime
from typing import Annotated

from wreath import Request, Router
from wreath.auth import authenticated
from wreath.binding import Query
from wreath.exceptions import NotFound, UnprocessableEntity
from wreath.orm import FromORM, Session
from wreath.pagination import MAX_PAGE, MAX_SIZE, PageParams, paginate, parse_sort
from wreath.temporal import from_wall_clock, zone

from ..config import SETTINGS
from ..models import Reserve, Sighting, Species, Station
from ..policies import may_locate, visible_protections
from ..queries import RecentDeployments, Reserves, SightingsByStation, Stations
from ..wire import deployment_json, reserve_json, sighting_json, station_json

#: One read-workload session per request, leased only if a handler touches it.
ReadSession = Annotated[Session, FromORM("main", workload="read")]

#: Which columns a caller may sort the sighting list by. An allow-list, so
#: ``?sort=notes`` is refused rather than reaching the SQL, and it is short
#: because every column in it has to be worth an index.
SORTABLE = ("captured_at", "confidence", "id")

#: Newest first, with the primary key as the tiebreaker. Without the tiebreaker
#: two sightings captured in the same second can swap places between page 1 and
#: page 2, and a row is seen twice while another is never seen at all.
DEFAULT_SORT = ("-captured_at", "-id")

reserves = Router(prefix="/reserves", tags=("reserves",))
stations = Router(prefix="/{slug}/stations", tags=("stations",))


async def _reserve(session: Session, slug: str) -> Reserve:
    found = await Reserves(session).by_slug(slug=slug)
    if found is None:
        raise NotFound(f"no reserve with slug {slug!r}")
    return found


async def _station(session: Session, reserve: Reserve, station_id: int) -> Station:
    found = await Stations(session).by_id(station=station_id)
    if found is None or found.reserve_id != reserve.id:
        raise NotFound(f"no station {station_id} in reserve {reserve.slug!r}")
    return found


def _window(
    reserve: Reserve, since: datetime.date, days: int
) -> tuple[datetime.datetime, datetime.datetime]:
    """The half-open instant range a local date and a day count actually mean.

    A camera records local wall-clock time, so "the 1st of June at Nullarbor"
    starts at midnight *there*, which is 14:30 the previous day in UTC. Reading
    the date as a UTC midnight would shift every window by the reserve's offset
    and quietly move sightings between days.

    The end is the local midnight ``days`` later rather than the start plus
    ``days * 24h``. Across a daylight-saving change those differ by an hour, and
    the version that adds hours drops or double-counts the sightings in it.
    """
    tz = zone(reserve.timezone)
    midnight = datetime.time()
    start = from_wall_clock(datetime.datetime.combine(since, midnight), tz)
    end = from_wall_clock(
        datetime.datetime.combine(since + datetime.timedelta(days=days), midnight), tz
    )
    return start, end


@reserves.get("/", summary="Every reserve")
@authenticated()
async def list_reserves(request: Request, session: ReadSession) -> dict:
    found = await Reserves(session).all_reserves()
    return {"items": [reserve_json(item) for item in found]}


@reserves.get("/{slug}", summary="One reserve")
@authenticated()
async def read_reserve(request: Request, slug: str, session: ReadSession) -> dict:
    return reserve_json(await _reserve(session, slug))


@stations.get("/", summary="The stations in a reserve")
@authenticated()
async def list_stations(request: Request, slug: str, session: ReadSession) -> dict:
    reserve = await _reserve(session, slug)
    found = await Stations(session).in_reserve(reserve=reserve.id)
    # One policy question per *sensitivity*, not per station: the answer depends
    # only on the flag and the identity, so asking it 48 times would be 48
    # evaluations of one decision.
    locate = {
        sensitive: may_locate(request.identity, sensitive=sensitive)
        for sensitive in (False, True)
    }
    return {
        "items": [
            station_json(item, locate=locate[bool(item.sensitive)])
            for item in sorted(found, key=lambda s: s.id)
        ]
    }


@stations.get("/{station_id}", summary="One station and every camera it has held")
@authenticated()
async def read_station(
    request: Request, slug: str, station_id: int, session: ReadSession
) -> dict:
    reserve = await _reserve(session, slug)
    station = await _station(session, reserve, station_id)
    # `Station.cameras` is `load="raise"`, so reading it now would raise rather
    # than emit a query nobody asked for. `session.load` is the ask: one batched
    # statement, said out loud at the call site that wants the data. Every other
    # handler in this file leaves the relationship alone and pays nothing.
    await session.load(station, Station.cameras)
    history = sorted(station.cameras, key=lambda camera: camera.deployed_at)
    return station_json(
        station,
        cameras=history,
        locate=may_locate(request.identity, sensitive=bool(station.sensitive)),
    )


@stations.get("/{station_id}/sightings", summary="What a station recorded, by page")
@authenticated()
async def list_sightings(
    request: Request,
    slug: str,
    station_id: int,
    session: ReadSession,
    since: datetime.date,
    days: Annotated[int, Query(minimum=1, maximum=SETTINGS.max_window_days)] = 7,
    min_confidence: Annotated[int, Query(minimum=0, maximum=100)] = 0,
    page: Annotated[int, Query(minimum=1, maximum=MAX_PAGE)] = 1,
    size: Annotated[int, Query(minimum=1, maximum=MAX_SIZE)] = 20,
    sort: str = "",
) -> dict:
    """One page of a station's sightings inside a bounded local-date window.

    ``since`` has no default on purpose. "Everything ever recorded here" is a
    scan of 140,000 rows that any caller could ask for, and a default window of
    "the last week" would be a moving target that made this page's examples
    untrue a week after they were written.

    ``days`` is bounded by the binding layer, so a caller asking for a decade
    gets a structured 422 before a query is built. The bound comes from
    ``CAMERA_TRAP_MAX_WINDOW_DAYS`` and is therefore fixed when this module is
    imported — which is what start-up configuration means.

    ``page``, ``size`` and ``sort`` are bound here rather than through
    ``Depends(page_params)``, which is how the pagination guide writes it. That
    form does not work today: a dependency is always called as ``fn(request)``
    and its own scalar parameters are never bound from the query string, so
    ``page_params`` receives the request where it expects a page number. Bounds
    and defaults are taken from ``wreath.pagination`` so the two cannot drift,
    and this reverts to one dependency the day that does work.
    """
    reserve = await _reserve(session, slug)
    station = await _station(session, reserve, station_id)
    start, end = _window(reserve, since, days)

    # Bound rather than called: `bind` hands back an ordinary `Select`, which is
    # what `paginate` needs. Calling the declaration would run it and fetch
    # every row in the window.
    query = SightingsByStation.in_window.bind(station=station.id, since=start, until=end)

    # The sensitive-species rule, applied *in the query* rather than to the page
    # it returns. Filtering after the fetch would make `total` a lie and hand a
    # volunteer a 20-row page with four rows in it — and, worse, would have
    # loaded the withheld rows onto this machine before discarding them.
    #
    # One statement: the species this caller may see are named by a subquery, so
    # the vocabulary is never carried to the application and back. `total` and
    # the page are then filtered by construction, because `paginate` counts the
    # same query it fetches. A list of ids would have worked at forty species
    # and become a wire-sized problem at a million; this shape does not change.
    allowed = visible_protections(request.identity)
    if not allowed:
        # Anonymous, or suspended. A 404 rather than a 403: telling an
        # unauthenticated caller that this station has sightings they may not
        # see is itself a disclosure, and the station is reachable only through
        # a reserve they are not signed in to browse.
        raise NotFound(f"no station {station_id} in reserve {slug!r}")
    query = query.where(
        Sighting.species_id.in_(
            Species.select(Species.id).where(Species.protection.in_(list(allowed)))
        )
    )

    if min_confidence:
        # Added here rather than declared: the filter is optional, and a
        # parameter in the declared shape would have to be supplied by every
        # caller including the ones that do not want it. The cost is a second
        # compiled shape, which is the honest price of a sometimes-absent
        # filter.
        query = query.where(Sighting.confidence >= min_confidence)

    # `apply_sort` appends to the ordering a query already has, so a declaration
    # that sorted would make the caller's `?sort=` a tiebreaker instead of the
    # sort. The declaration deliberately does not order; the default lives here.
    tokens = parse_sort(sort)
    # Checked before `paginate` rather than caught after it: the allow-list
    # refusal is a `ValueError`, and a `ValueError` out of a handler is a 500 --
    # which is the wrong answer for a caller who mistyped a column name.
    unknown = [token.lstrip("-") for token in tokens if token.lstrip("-") not in SORTABLE]
    if unknown:
        raise UnprocessableEntity(
            f"cannot sort by {', '.join(sorted(unknown))}; "
            f"sortable columns are {', '.join(SORTABLE)}"
        )
    wanted = PageParams(page=page, size=size, sort=tokens or DEFAULT_SORT)
    result = await paginate(session, query, wanted, allow_sort=SORTABLE)
    return {
        "station_id": station.id,
        "since": start,
        "until": end,
        **result.as_dict(),
        "items": [sighting_json(item) for item in result.items],
    }


@stations.get("/{station_id}/deployments", summary="The last few SD cards from a station")
@authenticated()
async def list_deployments(
    request: Request, slug: str, station_id: int, session: ReadSession
) -> dict:
    reserve = await _reserve(session, slug)
    station = await _station(session, reserve, station_id)
    found = await RecentDeployments(session).for_station(station=station.id)
    return {"items": [deployment_json(item) for item in found]}


reserves.include_router(stations)
