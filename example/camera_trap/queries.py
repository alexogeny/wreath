"""The reads this application does, given names.

Four handlers wanted the same three queries, and the version of this file that
does not exist has them inlined at each call site with the filters copied. A
``Queries`` class is that data-access layer declared instead of written: the
shape is fixed when the class is defined, only the values vary per call, and
each declaration therefore compiles exactly once through the ORM's plan cache
however many stations ask it.

Two things to read for rather than past:

**No declaration sorts.** ``Select.order_by`` *appends*, so a declaration that
already ordered would make the caller's ``?sort=`` a tiebreaker rather than the
sort — silently, and only visibly wrong on the second page. The handlers own
ordering, and :mod:`wreath.pagination` applies it against an allow-list.

**Only the mandatory filters are declared.** A sighting list may be narrowed by
confidence, and that filter is optional; a parameter in the declared shape would
have to be supplied on every call, including the calls that do not want it. The
handler adds it, which costs a second compiled shape and is the honest price of
a filter that is genuinely sometimes absent.
"""

from __future__ import annotations

from wreath.queries import Param, Queries, query

from .models import Deployment, Reserve, Sighting, Species, Station


class Reserves(Queries[Reserve]):
    """Reserves are looked up by slug, because that is what the URL carries."""

    all_reserves = query().order_by(Reserve.name)
    by_slug = query(Reserve.slug == Param("slug")).one()


class Stations(Queries[Station]):
    """A reserve's stations, and one of them."""

    in_reserve = query(Station.reserve_id == Param("reserve"))
    by_id = query(Station.id == Param("station")).one()


class SightingsByStation(Queries[Sighting]):
    """The chart query and the list query, which are the same query.

    The window is half-open — ``since <= captured_at < until`` — so two adjacent
    requests neither drop a sighting on the boundary nor count it twice. Closing
    the upper end is the mistake that makes a month's totals disagree with the
    sum of its days.
    """

    in_window = query(
        Sighting.station_id == Param("station"),
        Sighting.captured_at >= Param("since"),
        Sighting.captured_at < Param("until"),
    )


class RecentDeployments(Queries[Deployment]):
    """The collection trips for one station, newest first.

    ``limit`` is part of the shape rather than a parameter, and
    :mod:`wreath.queries` refuses to let it be one: a limit a caller supplies is
    a different query, and this one answers "what were the last few cards".
    """

    for_station = (
        query(Deployment.station_id == Param("station"))
        .order_by(Deployment.collected_at.desc())
        .limit(10)
    )


class SpeciesCatalog(Queries[Species]):
    """The controlled vocabulary: all of it, or one entry by its code."""

    all_species = query().order_by(Species.common_name)
    by_code = query(Species.code == Param("code")).one()
