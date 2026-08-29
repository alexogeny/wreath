from __future__ import annotations

import os
import pathlib

import pytest
from camera_trap.policies import (
    ADMINISTER,
    ENGINE,
    PROTECTIONS,
    ROLES,
    may_locate,
    may_see_protection,
    principal_entity,
    visible_protections,
)

from wreath.auth import Identity
from wreath.authorization import CedarEntity, EntityUid

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
    "2025-01-01",
    "2025-04-01",
    "2025-07-01",
    "2025-10-01",
    "2026-01-01",
    "2026-04-01",
    "2026-07-01",
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


def test_the_policy_set_parses_at_import() -> None:
    assert ENGINE is not None
    assert set(ROLES) == {"volunteer", "researcher", "ranger"}
    assert set(PROTECTIONS) == {"open", "sensitive", "restricted"}


@pytest.mark.parametrize(("role", "tier"), sorted(GRID))
def test_the_protection_grid_is_what_the_policy_says(role: str, tier: str) -> None:
    assert may_see_protection(observer(role), tier) is GRID[(role, tier)]


def test_an_anonymous_caller_sees_nothing_at_all() -> None:
    assert visible_protections(None) == ()
    for tier in PROTECTIONS:
        assert may_see_protection(None, tier) is False
    assert may_locate(None, sensitive=False) is False


#: The other half of the policy file, and it had no test of its own until
#: `wreath mutant` could reach a policy set compiled at import: deleting the
#: researcher's `Registry::administer` permit changed nothing any test asserted.
#: The ranger's twin was covered only by a database-gated route test, so on a
#: machine with no PostgreSQL neither was watched at all.
REGISTRY_GRID = {"volunteer": False, "researcher": True, "ranger": True}


@pytest.mark.parametrize("role", sorted(REGISTRY_GRID))
def test_who_may_administer_the_registry(role: str) -> None:
    principal = principal_entity(observer(role))
    registry = EntityUid("Registry", RESERVE)
    decision = ENGINE.is_authorized(
        principal=principal.uid,
        action=EntityUid("Action", ADMINISTER),
        resource=registry,
        context={},
        entities=(principal, CedarEntity(registry, attrs={})),
    )
    assert bool(getattr(decision, "allowed", decision)) is REGISTRY_GRID[role]


def test_a_forbid_cannot_be_undone_by_a_later_permit() -> None:
    active = observer("ranger")
    suspended = observer("ranger", suspended=True)
    assert visible_protections(active) == ("open", "sensitive", "restricted")
    assert visible_protections(suspended) == ()
    assert may_locate(suspended, sensitive=False) is False


def test_only_a_ranger_may_locate_a_sensitive_station() -> None:
    for role in ROLES:
        assert may_locate(observer(role), sensitive=False) is True
    assert may_locate(observer("volunteer"), sensitive=True) is False
    assert may_locate(observer("researcher"), sensitive=True) is False
    assert may_locate(observer("ranger"), sensitive=True) is True


def test_visible_protections_is_derived_rather_than_listed() -> None:
    for role in ROLES:
        identity = observer(role)
        assert visible_protections(identity) == tuple(
            tier for tier in PROTECTIONS if may_see_protection(identity, tier)
        )


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
    ranger = _as(client, "ranger")
    volunteer = _as(client, "volunteer")

    catalogue = (await ranger.get("/species")).json()["items"]
    restricted = {item["id"] for item in catalogue if item["protection"] == "restricted"}
    assert restricted, "the seed must contain restricted species or this proves nothing"

    # Find one the ranger can actually see, so the id is known to exist.
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
                f"/reserves/{RESERVE}/stations/{station}/sightings?since={start}&days=90&size=100"
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
    path = f"/reserves/{RESERVE}/stations/{SENSITIVE_STATION}"

    withheld = (await _as(client, "researcher").get(path)).json()
    assert withheld["sensitive"] is True
    assert "latitude" not in withheld, "a researcher is not told where the nest is"

    shown = (await _as(client, "ranger").get(path)).json()
    assert shown["sensitive"] is True
    assert "latitude" in shown and "longitude" in shown


@skip_without_database
async def test_an_ordinary_stations_location_is_not_a_secret(client) -> None:
    path = f"/reserves/{RESERVE}/stations/{OPEN_STATION}"
    for role in ROLES:
        body = (await _as(client, role).get(path)).json()
        assert body["sensitive"] is False
        assert "latitude" in body, f"{role} should be able to locate an ordinary station"


@skip_without_database
async def test_the_registry_refuses_a_volunteer_and_admits_a_researcher(client) -> None:
    assert (await _as(client, "volunteer").get("/admin/species")).status == 403
    assert (await _as(client, "researcher").get("/admin/species")).status == 200


@skip_without_database
async def test_the_registry_refuses_to_create_or_delete_a_station(client) -> None:
    ranger = _as(client, "ranger")
    assert (await ranger.post("/admin/stations", json={"name": "x"})).status == 403
    assert (await ranger.delete(f"/admin/stations/{OPEN_STATION}")).status == 403


@skip_without_database
async def test_a_volunteer_never_sees_another_reserves_stations_in_the_register(
    client,
) -> None:
    volunteer = client.acting_as("volunteer-1", roles=["volunteer"], type="Observer")
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
    response = await _as(client, "ranger").get("/permissions/manifest")
    assert response.status == 200
    assert response.header("etag"), "the manifest is cacheable per policy set"
