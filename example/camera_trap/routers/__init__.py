"""The read API, one router per bounded context.

``reserves`` carries the geography — reserves, their stations, and everything
that hangs off a station. ``sightings`` carries the single record, which is
reached by id from a link rather than by walking the hierarchy. ``species``
carries the controlled vocabulary, which belongs to nobody's reserve and is the
one endpoint the public reads. They are separate routers because they answer to
different people: an ecologist edits the vocabulary, a field team moves the
stations, a reviewer opens one sighting.

Routers nest. ``stations`` is included into ``reserves`` rather than mounted on
the application, so ``/reserves/{slug}/stations/{station_id}/sightings`` is
assembled from the prefixes of the routers that own each part of it. Including a
router takes a snapshot and folds the prefixes into each route, so the
arrangement in these files is exactly the arrangement that runs.

Later stages add the review console and the admin surface to this same list.
"""

from __future__ import annotations

from . import admin
from .reserves import reserves
from .session import session
from .sightings import sightings
from .species import species

#: Included in this order by `camera_trap.app.build`.
ROUTERS = (reserves, sightings, species, session)

#: `admin` is a module rather than a router: its two CRUD surfaces are
#: *generated* from the models, and generating them needs a session opener that
#: only exists once the application has an ORM registry. `admin.mount(app, ...)`
#: is called from `build` after that point.
__all__ = ["ROUTERS", "admin", "reserves", "session", "sightings", "species"]
