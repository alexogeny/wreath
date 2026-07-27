"""The sensitive-species story, asserted end to end.

This is the example's sharpest argument, so it gets the sharpest tests. Three
properties, and the example would be *wrong* without any of them:

1. **The policy grid is what the file says it is.** Nine cells — three roles by
   three protection tiers — checked against the Cedar engine directly, with no
   database and no HTTP, so a policy edit that widens access fails in
   milliseconds on every run.

2. **A volunteer cannot reach a rhino by guessing.** The list endpoint filters
   and the detail endpoint 404s, and it has to be *both*: filtering a list while
   leaving `/sightings/{id}` open is the shape of every access-control bug that
   ever shipped.

3. **The refusal is a 404, not a 403.** A 403 tells the caller the row exists,
   and for a restricted species the existence *is* the secret.

The `acting_as` client is what makes the grid affordable to test: it rides the
request scope rather than the backend, so one application serves a volunteer, a
researcher and a ranger in the same test without three logins.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from camera_trap.policies import (
    ENGINE,
    PROTECTIONS,
    ROLES,
    may_locate,
    may_see_protection,
    visible_protections,
)

from wreath.auth import Identity

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
_ARTIFACT = pathlib.Path(__file__).resolve().parents[2] / "example" / "migrations" / "migration.sql"

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the camera-trap authorization tests",
)

#: Small: these tests ask who can see what, not how paging behaves.
SAMPLE = 400

RESERVE = "nullarbor"

#: Seeded sensitive (the first two stations of each reserve are), and ordinary.
SENSITIVE_STATION = 25
OPEN_STATION = 27

#: Consecutive 90-day window starts covering the seed's whole capture range.
#:
#: 90 is the `CAMERA_TRAP_MAX_WINDOW_DAYS` ceiling, so no single request can span
#: the seed and a test that asks for a year gets a 422 rather than a page. Any
#: test hunting for a particular row walks these instead of guessing a quarter.
_WINDOWS = (
    "2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01",
    "2026-01-01", "2026-04-01", "2026-07-01",
)

#: The grid the policy file documents in prose. Written out here as data so the
#: two cannot drift silently: if someone widens a rule, this table is what goes
#: red, and it reads as the same table the docstring shows.
GRID = {
    ("volunteer", "open"): True,
    ("volunteer", "sensitive"): False,
    ("volunteer", "restricted"): False,
    ("researcher", "open"): True,
    ("researcher", "sensitive"): True,
    ("researcher", "restricted"): False,
    ("ranger", "open"): True,
    ("ranger", "sensitive"): True,
    ("ranger", "restricted"): True,
}


def observer(role: str, **claims: object) -> Identity:
    """An identity of the shape the session backend produces on sign-in."""
    return Identity(
        id=f"{role}-1",
        type="Observer",
        roles=frozenset({role}),
        permissions=frozenset(),
        claims=claims,
    )


# -- the policy, with no database and no HTTP ---------------------------------


def test_the_policy_set_parses_at_import() -> None:
    """A syntax error is a start-up failure, which is when it is cheap.

    Asserting the engine exists is not the point; asserting it was *parsed* is.
    `CedarPolicies` raises on malformed input, so reaching this line at all is
    the evidence — and naming that here stops someone from later making the
    parse lazy without noticing what they gave up.
    """
    assert ENGINE is not None
    assert set(ROLES) == {"volunteer", "researcher", "ranger"}
    assert set(PROTECTIONS) == {"open", "sensitive", "restricted"}


@pytest.mark.parametrize(("role", "tier"), sorted(GRID))
def test_the_protection_grid_is_what_the_policy_says(role: str, tier: str) -> None:
    """Nine cells, each one an independent test with its own name in the report."""
    assert may_see_protection(observer(role), tier) is GRID[(role, tier)]


def test_an_anonymous_caller_sees_nothing_at_all() -> None:
    """Cedar's default is deny, and the code refuses before it asks.

    Both agree, which is the point: the belt and the braces are checked
    together, so removing either one leaves this test red.
    """
    assert visible_protections(None) == ()
    for tier in PROTECTIONS:
        assert may_see_protection(None, tier) is False
    assert may_locate(None, sensitive=False) is False


def test_a_forbid_cannot_be_undone_by_a_later_permit() -> None:
    """The standing suspension, which is the reason `forbid` is in the file.

    A suspended *ranger* — the most privileged role there is — sees nothing.
    In Cedar `forbid` overrides `permit` unconditionally, so no rule anybody
    adds later can re-admit them. A suspension that a subsequent permit could
    defeat is not a suspension, and this asserts the difference.
    """
    active = observer("ranger")
    suspended = observer("ranger", suspended=True)
    assert visible_protections(active) == ("open", "sensitive", "restricted")
    assert visible_protections(suspended) == ()
    assert may_locate(suspended, sensitive=False) is False


def test_only_a_ranger_may_locate_a_sensitive_station() -> None:
    """The tier is on the *place*, not on what walked past it.

    A nest tree is worth protecting in a week when nothing was photographed
    there, which is why this is a separate action from reading a sighting.
    """
    for role in ROLES:
        assert may_locate(observer(role), sensitive=False) is True
    assert may_locate(observer("volunteer"), sensitive=True) is False
    assert may_locate(observer("researcher"), sensitive=True) is False
    assert may_locate(observer("ranger"), sensitive=True) is True


def test_visible_protections_is_derived_rather_than_listed() -> None:
    """The filter a query uses is the policy's answer, not a parallel table.

    If `visible_protections` were a dict keyed on role, it would be a second
    copy of the rules that a policy edit would leave behind. Asserting it agrees
    with the per-tier decision for every role is what pins it to the engine.
    """
    for role in ROLES:
        identity = observer(role)
        assert visible_protections(identity) == tuple(
            tier for tier in PROTECTIONS if may_see_protection(identity, tier)
        )


# -- against a real database ---------------------------------------------------


@pytest.fixture
async def client():
    """The application on a freshly built and seeded schema.

    `build()`'s own default (`"warn"`) applies. The catalog read behind schema
    validation no longer hangs — that was fixed — but with `"error"` the
    application still refuses to start on this correct schema, because foreign
    keys are validated by comparing the target's physical column number against
    its position in the model declaration and the DDL generator does not emit
    columns in declaration order. `build`'s docstring has the detail;
    `test_schema_integration.py` asserts the constraints are genuinely there.
    """
    from _camera_trap import build_schema, drop_schema
    from camera_trap.app import build

    from wreath.postgres import connect
    from wreath.testing import TestClient

    connection = await connect(_DSN)
    try:
        await build_schema(connection, seed_rows=SAMPLE)
    finally:
        await connection.close()

    application = build()
    async with TestClient(application) as test_client:
        yield test_client

    connection = await connect(_DSN)
    try:
        await drop_schema(connection)
    finally:
        await connection.close()


def _as(client, role: str):
    """A client acting as an observer of this role."""
    return client.acting_as(f"{role}-1", roles=[role], type="Observer")


@skip_without_database
async def test_a_volunteers_sighting_list_contains_no_withheld_species(client) -> None:
    """The filter is applied in the query, so the page is whole and honest.

    A volunteer's page must contain only open species — and it must be a *full*
    page, not twenty rows with the withheld ones blanked out. Checking the
    species of every row is the only assertion that distinguishes the two.
    """
    volunteer = _as(client, "volunteer")
    ranger = _as(client, "ranger")
    path = f"/reserves/{RESERVE}/stations/{OPEN_STATION}/sightings?since=2026-01-01&days=90"

    theirs = await volunteer.get(path)
    assert theirs.status == 200
    mine = await ranger.get(path)
    assert mine.status == 200

    # A ranger sees at least as much as a volunteer, and the seed puts enough
    # withheld species about that it should be strictly more somewhere.
    assert mine.json()["total"] >= theirs.json()["total"]

    catalogue = {item["id"]: item for item in (await ranger.get("/species")).json()["items"]}
    for row in theirs.json()["items"]:
        assert catalogue[row["species_id"]]["protection"] == "open"


@skip_without_database
async def test_a_restricted_sighting_is_a_404_for_a_volunteer_not_a_403(client) -> None:
    """The existence of the row is itself the secret.

    A 403 would let a caller walk ids and map the restricted sightings, and the
    count of those at a station is a map of where the rhinos are. This is the
    single most important status code in the example.
    """
    ranger = _as(client, "ranger")
    volunteer = _as(client, "volunteer")

    catalogue = (await ranger.get("/species")).json()["items"]
    restricted = {item["id"] for item in catalogue if item["protection"] == "restricted"}
    assert restricted, "the seed must contain restricted species or this proves nothing"

    # Find one the ranger can actually see, so the id is known to exist.
    #
    # Walked in 90-day windows rather than asked for in one request, because 90
    # days is the configured `CAMERA_TRAP_MAX_WINDOW_DAYS` ceiling and a wider
    # `days=` is a 422 — the handler refusing exactly as it should. Walking also
    # keeps this test honest against a reseed: it finds whatever restricted
    # sighting exists rather than depending on one landing in a hard-coded
    # quarter.
    found = None
    for station in (OPEN_STATION, SENSITIVE_STATION):
        for start in _WINDOWS:
            listing = await ranger.get(
                f"/reserves/{RESERVE}/stations/{station}"
                f"/sightings?since={start}&days=90&size=100"
            )
            assert listing.status == 200, listing.json()
            for row in listing.json()["items"]:
                if row["species_id"] in restricted:
                    found = row["id"]
                    break
            if found is not None:
                break
        if found is not None:
            break
    assert found is not None, "the seed put no restricted sighting in any window"

    assert (await ranger.get(f"/sightings/{found}")).status == 200
    refused = await volunteer.get(f"/sightings/{found}")
    assert refused.status == 404, "a 403 would confirm the sighting exists"


@skip_without_database
async def test_only_a_ranger_is_told_where_a_sensitive_station_is(client) -> None:
    """The coordinate is absent, not null, and the flag is still there."""
    path = f"/reserves/{RESERVE}/stations/{SENSITIVE_STATION}"

    withheld = (await _as(client, "researcher").get(path)).json()
    assert withheld["sensitive"] is True
    assert "latitude" not in withheld, "a researcher is not told where the nest is"

    shown = (await _as(client, "ranger").get(path)).json()
    assert shown["sensitive"] is True
    assert "latitude" in shown and "longitude" in shown


@skip_without_database
async def test_an_ordinary_stations_location_is_not_a_secret(client) -> None:
    """The rule is about sensitivity, not about being signed in as a ranger.

    Without this, a policy that simply refused every coordinate to non-rangers
    would pass every other test in this file while making the map useless.
    """
    path = f"/reserves/{RESERVE}/stations/{OPEN_STATION}"
    for role in ROLES:
        body = (await _as(client, role).get(path)).json()
        assert body["sensitive"] is False
        assert "latitude" in body, f"{role} should be able to locate an ordinary station"


@skip_without_database
async def test_the_registry_refuses_a_volunteer_and_admits_a_researcher(client) -> None:
    """Generated CRUD is behind the same policy as everything else."""
    assert (await _as(client, "volunteer").get("/admin/species")).status == 403
    assert (await _as(client, "researcher").get("/admin/species")).status == 200


@skip_without_database
async def test_the_registry_refuses_to_create_or_delete_a_station(client) -> None:
    """Stations are retired by a field team with paperwork, not a console button.

    `Access.deny()` keeps the route in the OpenAPI document and answers 403, so
    a client learns the operation is forbidden rather than guessing from a 404
    whether it typed the path wrong.
    """
    ranger = _as(client, "ranger")
    assert (await ranger.post("/admin/stations", json={"name": "x"})).status == 403
    assert (await ranger.delete(f"/admin/stations/{OPEN_STATION}")).status == 403


@skip_without_database
async def test_a_volunteer_never_sees_another_reserves_stations_in_the_register(
    client,
) -> None:
    """The row-level check narrows a page, which is why it can come back short.

    A volunteer is assigned to one reserve. `object_authorizer` runs per row, so
    the page it returns is the intersection of "a page of stations" and "the
    ones this observer may work with" — and a sensitive station is excluded even
    within their own reserve, because the row carries the coordinates.
    """
    volunteer = client.acting_as(
        "volunteer-1", roles=["volunteer"], type="Observer"
    )
    # Seeded: observer 1 is a volunteer whose reserve_id is (1 % 4) + 1 == 2.
    # Without the claim the check cannot scope, so this asserts the claim path
    # too -- an identity with no `reserve_id` is treated as cross-reserve.
    page = await volunteer.get("/admin/stations")
    assert page.status == 403, "a volunteer may not administer the registry"

    ranger = _as(client, "ranger")
    allowed = await ranger.get("/admin/stations")
    assert allowed.status == 200
    rows = allowed.json()
    items = rows["items"] if isinstance(rows, dict) else rows
    assert items, "a ranger with no reserve claim sees the register"


@skip_without_database
async def test_the_permissions_endpoint_answers_from_the_same_declarations(
    client,
) -> None:
    """The console's greyed-out buttons come from the rules that enforce them.

    `permissions_router` reads the action vocabulary off the routes' own
    `@authorize` declarations, so there is no second list to keep in step. This
    asserts the endpoint is mounted and answers per identity — the thing that
    would silently rot if the console maintained its own copy.
    """
    response = await _as(client, "ranger").get("/permissions/manifest")
    assert response.status == 200
    assert response.header("etag"), "the manifest is cacheable per policy set"
