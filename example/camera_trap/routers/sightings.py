"""One sighting, with the things it points at.

A sighting is a row of foreign keys. On its own it says species 14 walked past
station 3 in front of camera 51, which is useless to a human and one round trip
short of useful to a client. This endpoint is where the example pays the cost of
resolving them — once, in one query, because the relationships are asked for.

That is the whole argument for ``load="raise"`` in a shape you can see. Nothing
here is lazy: reading ``sighting.species`` without having included it raises,
so the N+1 that a list endpoint would otherwise grow cannot be written by
accident. The list endpoint next door deliberately does *not* include them, and
returns ids instead — twenty rows are twenty ids, not sixty joins.
"""

from __future__ import annotations

from typing import Annotated

from wreath import Request, Router
from wreath.auth import authenticated
from wreath.exceptions import NotFound
from wreath.orm import FromORM, Session

from ..models import Sighting
from ..policies import may_locate, may_see_protection
from ..wire import sighting_json

ReadSession = Annotated[Session, FromORM("main", workload="read")]

sightings = Router(prefix="/sightings", tags=("sightings",))


@sightings.get("/{sighting_id}", summary="One sighting with its station, camera and species")
@authenticated()
async def read_sighting(request: Request, sighting_id: int, session: ReadSession) -> dict:
    """One sighting, or a 404 if this observer may not see it.

    **A 404 and not a 403, and this is the sharpest decision in the example.**
    A 403 says "this exists and you may not have it". For a rhino, that
    sentence *is* the leak: a caller walking ids learns exactly which sightings
    are restricted, and the count of restricted sightings at a station is a map
    of where the rhinos are. Withholding the fact of existence is the only
    answer that protects the animal, and it costs a caller nothing they were
    entitled to.

    The rule is the same Cedar policy the list endpoint filters by, asked here
    about one row instead of a tier — so a sighting that cannot appear in the
    list cannot be reached by guessing its id either. Two endpoints, one rule.
    """
    found = await session.fetch_one(
        Sighting.select()
        .where(Sighting.id == sighting_id)
        .include(
            # Three to-one relationships, so a join is cheaper than three extra
            # statements. `selectin` would be the right call for a to-many,
            # where a join multiplies the parent row by its children.
            Sighting.station.joined(),
            Sighting.camera.joined(),
            Sighting.species.joined(),
        )
    )
    if found is None:
        raise NotFound(f"no sighting {sighting_id}")
    if not may_see_protection(request.identity, found.species.protection):
        raise NotFound(f"no sighting {sighting_id}")
    return sighting_json(
        found,
        related=True,
        locate=may_locate(request.identity, sensitive=bool(found.station.sensitive)),
    )
