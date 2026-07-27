"""The controlled vocabulary, and the one cached endpoint in the example.

Forty rows that change a few times a year, read by every client on every screen
that has to render an animal's name. That is the shape a cache is for, and it is
worth being precise about why this endpoint qualifies when the sighting list
does not:

* the answer is the same for every caller, so one entry serves all of them;
* it is small and bounded, so the store cannot be filled by a caller varying a
  parameter;
* and wreath knows when it goes stale. ``invalidate_on=[Species]`` puts the
  cache on the ORM's own write announcement, so a committed write to the species
  table clears it — from a handler, an admin console, a job, or a psql session
  going through the application. The TTL is what remains: a backstop for a change
  wreath cannot see, such as a row edited directly in the database.

The sighting list is none of those things. It is per-station, per-window and
per-page, it grows with the request space rather than with the data, and it is
answered from an index anyway.
"""

from __future__ import annotations

from typing import Annotated

from wreath import Request, Router
from wreath.exceptions import NotFound
from wreath.orm import FromORM, Session
from wreath.response_cache import cached

from ..config import SETTINGS
from ..models import Species
from ..queries import SpeciesCatalog
from ..wire import species_json

ReadSession = Annotated[Session, FromORM("main", workload="read")]

species = Router(prefix="/species", tags=("species",))


@species.get("/", summary="The whole species vocabulary")
@cached(ttl=SETTINGS.species_cache_ttl, invalidate_on=[Species])
async def list_species(request: Request, session: ReadSession) -> dict:
    found = await SpeciesCatalog(session).all_species()
    return {"items": [species_json(item) for item in found]}


@species.get("/{code}", summary="One species by its four-letter code")
async def read_species(request: Request, code: str, session: ReadSession) -> dict:
    # Not cached: forty separate entries for the same forty rows the list
    # endpoint already holds in one, and each of them evicting something that
    # was earning its place.
    found = await SpeciesCatalog(session).by_code(code=code.upper())
    if found is None:
        raise NotFound(f"no species with code {code!r}")
    return species_json(found)
