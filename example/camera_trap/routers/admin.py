"""The registry console: generated CRUD over the two tables people edit.

`Species` and `Station` are the example's only genuinely administrative tables.
The vocabulary gains a species when a new animal is recorded in the region; the
station register changes when a camera is moved to a new tree. Everything else
in this schema is either observed data (`Sighting`, `Deployment`) that arrives
through ingest rather than a form, or identity (`Observer`, `Assignment`) whose
editing is a different job with a different audit story.

That distinction is why this file mounts CRUD for two models and not nine.
Generated CRUD is worth reaching for exactly when the shape of the work is "a
person edits rows in a table"; for `Sighting` it is not, and generating it there
would produce a `DELETE /sighting/{id}` that no domain rule wants to exist.

**Both opt-ins are deliberate.** `enable_crud()` is the application saying it
wants generated routes at all, and `crud(...)` is this module saying which
models. A framework that generated CRUD by default would put
`DELETE /observer/{id}` on the network the first time somebody declared a model.

**What the row-level check is actually for.** `object_authorizer` runs after the
row is loaded — on retrieve, update and delete, and on *every row of a list
page*, which is why a page can come back shorter than `size`. That is the only
place a decision about "this reserve's stations, not that one's" can be made,
because until the row exists there is no `reserve_id` to compare. Route-level
`authorize=` cannot express it and does not try to.
"""

from __future__ import annotations

from typing import Any

from wreath.crud import Access

from ..models import Species, Station
from ..policies import ADMINISTER, may_locate

#: Columns the station register hands back. An allow-list rather than
#: `exclude=`, because the deny-list form stays correct only until somebody adds
#: a column: a new `access_notes` field would be published by an `exclude` list
#: written before it existed, and withheld by this one.
#:
#: `latitude` and `longitude` are *in* the list, and the row-level check below is
#: what withholds them — see its docstring for why that is the honest split
#: rather than a per-row field filter.
STATION_FIELDS = ("id", "reserve_id", "name", "habitat", "sensitive", "latitude", "longitude")

#: The vocabulary is small, public and entirely non-secret. Naming the columns
#: anyway keeps the two models' surfaces described in the same style.
SPECIES_FIELDS = ("id", "code", "common_name", "scientific_name", "protection", "nocturnal")

#: The two registers, as Cedar entity references.
#:
#: `Access.cedar(resource=...)` takes an entity *reference* -- `Type::"id"` --
#: not a bare type name. A bare `"Registry"` parses at request time and raises
#: `CedarParseError`, which reaches the client as a 500 rather than the 403 the
#: declaration plainly intends. The two registers are named separately because
#: they are two resources: a policy that later admits a curator to the species
#: vocabulary but not the station register has somewhere to attach.
SPECIES_REGISTRY = 'Registry::"species"'
STATION_REGISTRY = 'Registry::"stations"'


async def _station_visible(request: Any, operation: str, station: Any) -> bool:
    """May this observer work with this station row?

    Two rules, in the order a reader should think about them:

    1. **Scope.** A volunteer is assigned to one reserve and sees only its
       stations. A researcher works across reserves. The identity carries the
       role; the observer's own reserve arrives in the session claims, because
       re-reading the `observers` row on every row of every page is the N+1 this
       framework spends most of its effort refusing to write elsewhere.

    2. **Sensitivity.** A sensitive station's row carries its coordinates, so
       being handed the row *is* being told where the nest is. The Cedar policy
       already answers "may this principal locate a sensitive station", and this
       asks it rather than re-deciding.

    **Why the whole row rather than blanking two columns.** `fields` and
    `expose` are static: they describe the model's surface, not one caller's
    view of one row, and there is no per-row field filter to reach for. Given
    the choice between publishing a coordinate to someone who may not have it
    and withholding a row they may not locate, withholding the row is the only
    one of the two that is safe. The read API takes the other half of this
    problem — `station_json` redacts coordinates and returns the rest — and that
    asymmetry is deliberate: a register is for editing, a read API is for
    looking.
    """
    identity = request.identity
    if identity is None:
        return False
    if station.sensitive and not may_locate(identity, sensitive=True):
        return False
    scope = identity.claims.get("reserve_id")
    if scope is None:
        # A researcher or ranger with no single home reserve. The Cedar policy
        # has already admitted them to the registry; there is nothing further
        # to narrow.
        return True
    return int(scope) == int(station.reserve_id)


def mount(application: Any, open_session: Any) -> None:
    """Attach the two generated routers to `application`.

    A function rather than a module-level pair of routers because
    `crud_router` needs a session opener, and the session opener needs the
    registry that `app.orm(...)` returns — which does not exist until the
    application is being assembled. Passing it in keeps this module importable
    without a database, like every other module in the package.
    """
    application.enable_crud()
    application.crud(
        Species,
        open_session,
        prefix="/admin/species",
        fields=SPECIES_FIELDS,
        readonly=("id",),
        tags=("admin",),
        # One rule for every operation: the vocabulary has no row-level
        # question, so a per-operation mapping here would be four copies of one
        # sentence.
        authorize=Access.cedar(action=ADMINISTER, resource=SPECIES_REGISTRY),
    )
    application.crud(
        Station,
        open_session,
        prefix="/admin/stations",
        fields=STATION_FIELDS,
        readonly=("id",),
        tags=("admin",),
        authorize={
            # Reading the register is open to anyone who may administer it; the
            # row-level check below is what narrows *which* rows.
            "list": Access.cedar(action=ADMINISTER, resource=STATION_REGISTRY),
            "retrieve": Access.cedar(action=ADMINISTER, resource=STATION_REGISTRY),
            "update": Access.cedar(action=ADMINISTER, resource=STATION_REGISTRY),
            # Stations are created and retired by a field team through a process
            # with paperwork, not by a console button. Refusing here is more
            # honest than omitting the operation: `Access.deny()` answers 403
            # with the route present in the OpenAPI document, so a client
            # learns the route exists and is forbidden rather than guessing at
            # a 404.
            "create": Access.deny(),
            "delete": Access.deny(),
        },
        object_authorizer=_station_visible,
    )
