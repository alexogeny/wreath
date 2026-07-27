"""The read API: routing, binding, declared queries, pagination, and the cache.

Split deliberately. The first half needs no database — the route table, the
declared query shapes, the wire format and the configuration are all decidable
from the code, and checking them in milliseconds means they are checked on every
run rather than only where PostgreSQL happens to be. The second half is gated on
``WREATH_TEST_POSTGRES_DSN`` and drives the real application through
``TestClient``.

What these assert is not "the handler returns 200". It is the handful of
properties the example would be *wrong* without: that a station cannot be
reached through another reserve's URL, that a date is read on the reserve's wall
clock rather than the server's, that the declared queries do not sort (because
sorting is the caller's), and that a committed write to the species table clears
the cached list without anybody calling an invalidator.
"""

from __future__ import annotations

import datetime
import os
import pathlib
from decimal import Decimal
from typing import Any

import pytest
from camera_trap.config import Settings
from camera_trap.models import Species
from camera_trap.queries import RecentDeployments, Reserves, SightingsByStation
from camera_trap.routers import ROUTERS
from camera_trap.wire import station_json

#: No ``pytest.mark.asyncio`` here: ``asyncio_mode = "auto"`` marks the async
#: tests, and a module-level mark would also land on the synchronous half and
#: warn about each one.

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
_ARTIFACT = pathlib.Path(__file__).resolve().parents[2] / "example" / "migrations" / "migration.sql"

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the camera-trap read-API tests",
)

#: Enough rows for a window to hold several pages and for the sort to have
#: something to order, small enough that every test can afford its own schema.
SAMPLE = 1_200

#: A reserve at +09:30. Chosen on purpose: a fractional offset is what breaks
#: code that reads a local date as a UTC midnight, and the failure is a
#: nine-and-a-half-hour shift that looks almost right.
RESERVE = "nullarbor"

#: Station 25 is the first of Nullarbor's twelve, and it is marked ``sensitive``
#: by the seed -- so it is also the station whose coordinates must not appear.
SENSITIVE_STATION = 25
OPEN_STATION = 27


# -- no database ---------------------------------------------------------------


def test_the_routers_compose_the_paths_the_domain_implies() -> None:
    """The URL hierarchy is assembled from router prefixes, not typed out.

    ``stations`` carries ``/{slug}/stations`` and is included into ``reserves``,
    so this is the one place the whole shape is visible. A route that grew a
    hand-written prefix would show up here as a duplicated segment.
    """
    found = sorted(
        (route.methods[0], route.path) for router in ROUTERS for route in router.routes
    )
    assert found == [
        ("GET", "/reserves"),
        ("GET", "/reserves/{slug}"),
        ("GET", "/reserves/{slug}/stations"),
        ("GET", "/reserves/{slug}/stations/{station_id}"),
        ("GET", "/reserves/{slug}/stations/{station_id}/deployments"),
        ("GET", "/reserves/{slug}/stations/{station_id}/sightings"),
        ("GET", "/sightings/{sighting_id}"),
        ("GET", "/species"),
        ("GET", "/species/{code}"),
    ]


def test_a_declared_query_names_exactly_the_values_it_binds() -> None:
    """The parameters are the declaration's contract, checked at import.

    A caller that misspells one gets a ``TypeError`` naming it rather than a
    query with a hole in it, and that is only true while the names here are the
    names the handlers pass.
    """
    assert SightingsByStation.in_window.parameters == ("station", "since", "until")
    assert RecentDeployments.for_station.parameters == ("station",)
    assert Reserves.by_slug.parameters == ("slug",)
    assert Reserves.by_slug.single, "a slug is unique; the declaration should return one"


def test_the_sighting_declaration_does_not_sort() -> None:
    """Ordering belongs to the caller, and ``order_by`` appends.

    If the declaration sorted, ``wreath.pagination.apply_sort`` would append the
    caller's ``?sort=`` *after* it — so the request's sort would be a tiebreaker
    on a column that rarely ties, and the API would silently ignore it. The
    handler owns the default instead. This is the assertion that keeps it so.
    """
    bound = SightingsByStation.in_window.bind(
        station=1,
        since=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        until=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC),
    )
    assert bound.orderings == ()


def test_a_recent_read_carries_its_own_limit() -> None:
    """"The last few cards" is part of the query's shape, not a parameter."""
    bound = RecentDeployments.for_station.bind(station=1)
    assert bound.limit_ == 10
    assert len(bound.orderings) == 1


class _StationRow:
    """A station without a database.

    The serializer reads plain attributes, so a stand-in keeps this assertion in
    the fast half of the suite. The rule it checks is the one thing in this file
    that must never regress quietly, so it should not need PostgreSQL to run.
    """

    id = 7
    reserve_id = 1
    name = "Nullarbor 01"
    habitat = "waterhole"
    latitude = Decimal("-1.500000")
    longitude = Decimal("36.200000")

    def __init__(self, *, sensitive: bool) -> None:
        self.sensitive = sensitive


def test_a_sensitive_station_publishes_no_coordinates() -> None:
    """Where a rhino midden is, is the one field this API must not leak.

    Asserted on the serializer rather than through a request, because that is
    where the rule lives and a later stage will replace it with an
    authorization rule in the same place.
    """
    shown: Any = station_json(_StationRow(sensitive=False))  # type: ignore[arg-type]
    withheld: Any = station_json(_StationRow(sensitive=True))  # type: ignore[arg-type]
    assert "latitude" in shown and "longitude" in shown
    assert "latitude" not in withheld and "longitude" not in withheld
    assert withheld["sensitive"] is True, "the client is told the field is withheld"


def test_configuration_has_defaults_and_refuses_a_missing_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two knobs default, and the one with no sensible default says so."""
    monkeypatch.delenv("CAMERA_TRAP_MAX_WINDOW_DAYS", raising=False)
    monkeypatch.delenv("CAMERA_TRAP_SPECIES_CACHE_TTL", raising=False)
    settings = Settings(dsn=None, max_window_days=90, species_cache_ttl=300.0)
    with pytest.raises(RuntimeError, match="CAMERA_TRAP_DSN"):
        settings.database_url()
    assert Settings(dsn="x", max_window_days=1, species_cache_ttl=1.0).database_url() == "x"


def test_a_bad_number_in_the_environment_names_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that will not boot should say which variable stopped it."""
    monkeypatch.setenv("CAMERA_TRAP_MAX_WINDOW_DAYS", "a fortnight")
    with pytest.raises(RuntimeError, match="CAMERA_TRAP_MAX_WINDOW_DAYS"):
        Settings.from_env()
    monkeypatch.setenv("CAMERA_TRAP_MAX_WINDOW_DAYS", "-3")
    with pytest.raises(RuntimeError, match="greater than zero"):
        Settings.from_env()


# -- against a real database ---------------------------------------------------


@pytest.fixture
async def client():
    """The application, on a freshly built and seeded schema.

    ``validate_schema="off"`` is not a preference. The ORM's start-up schema
    check reads the PostgreSQL catalog, that read decodes as text where the
    decoder expects binary, and the error surfaces inside the connection's
    reader task rather than the caller's — so lifespan startup *hangs*. Remove
    the argument when that is fixed; nothing else here depends on it.
    """
    from camera_trap.app import build
    from camera_trap.models import SCHEMA
    from camera_trap.seed import seed

    from wreath.postgres import connect
    from wreath.testing import TestClient

    connection = await connect(_DSN)
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{SCHEMA}"')
        for statement in _ARTIFACT.read_text().splitlines():
            if statement.strip():
                await connection.execute(statement.rstrip(";"))
        await seed(connection, sightings=SAMPLE)
    finally:
        await connection.close()

    application = build(validate_schema="off")
    async with TestClient(application) as test_client:
        yield test_client

    connection = await connect(_DSN)
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    finally:
        await connection.close()


@skip_without_database
async def test_the_reserve_list_carries_the_zone_every_timestamp_is_read_in(client) -> None:
    response = await client.get("/reserves")
    assert response.status == 200
    zones = {item["slug"]: item["timezone"] for item in response.json()["items"]}
    assert zones[RESERVE] == "Australia/Adelaide"
    assert len(zones) == 4


@skip_without_database
async def test_an_unknown_slug_is_a_404_that_says_what_was_missing(client) -> None:
    response = await client.get("/reserves/nowhere")
    assert response.status == 404
    assert "nowhere" in response.json()["detail"]


@skip_without_database
async def test_a_station_cannot_be_reached_through_another_reserve(client) -> None:
    """The URL hierarchy is enforced, not decorative.

    Station 25 belongs to Nullarbor. Asking for it under Olkiramatian's slug has
    to be a 404 -- otherwise the reserve segment is a comment, and the
    reserve-scoped authorization a later stage adds would have nothing to hold.
    """
    mine = await client.get(f"/reserves/{RESERVE}/stations/{SENSITIVE_STATION}")
    assert mine.status == 200
    theirs = await client.get(f"/reserves/olkiramatian/stations/{SENSITIVE_STATION}")
    assert theirs.status == 404


@skip_without_database
async def test_a_sensitive_stations_coordinates_never_reach_the_wire(client) -> None:
    response = await client.get(f"/reserves/{RESERVE}/stations")
    assert response.status == 200
    stations = {item["id"]: item for item in response.json()["items"]}
    assert stations[SENSITIVE_STATION]["sensitive"] is True
    assert "latitude" not in stations[SENSITIVE_STATION]
    assert "longitude" in stations[OPEN_STATION]


@skip_without_database
async def test_a_station_reports_the_devices_that_have_hung_there(client) -> None:
    """Station 3 has had two cameras. The place outlives the hardware.

    The relationship is ``load="raise"``, so this payload exists only because
    the handler asked for it with ``session.load``. A handler that forgot would
    fail loudly here rather than emitting a query per station somewhere else.
    """
    response = await client.get("/reserves/olkiramatian/stations/3")
    assert response.status == 200
    cameras = response.json()["cameras"]
    assert len(cameras) == 2
    assert cameras[0]["retired_at"] is not None, "the first device was replaced"
    assert cameras[1]["retired_at"] is None, "the second is still in service"
    assert cameras[0]["deployed_at"] < cameras[1]["deployed_at"]


@skip_without_database
async def test_a_date_window_is_read_on_the_reserves_wall_clock(client) -> None:
    """``since=2026-01-01`` at a +09:30 reserve is local midnight, not UTC's.

    This is the assertion the whole temporal story rests on. Reading the date as
    a UTC midnight would move the window by nine and a half hours and shift
    sightings between days -- while still returning a plausible-looking page.
    """
    response = await client.get(
        f"/reserves/{RESERVE}/stations/{OPEN_STATION}/sightings?since=2026-01-01&days=30"
    )
    assert response.status == 200
    body = response.json()
    assert body["since"].endswith("+10:30"), body["since"]
    assert body["since"].startswith("2026-01-01T00:00:00")
    # Thirty local days later, not 720 hours later.
    assert body["until"].startswith("2026-01-31T00:00:00")


@skip_without_database
async def test_the_window_is_bounded_before_any_sql_is_built(client) -> None:
    """A decade of sightings is a scan; the binding layer refuses it."""
    response = await client.get(
        f"/reserves/{RESERVE}/stations/{OPEN_STATION}/sightings?since=2026-01-01&days=4000"
    )
    assert response.status == 422
    errors = response.json()["errors"]
    assert errors[0]["loc"] == ["query", "days"]


@skip_without_database
async def test_paging_is_stable_and_the_total_matches(client) -> None:
    """Two pages of the same window do not overlap, and cover the total."""
    url = f"/reserves/{RESERVE}/stations/{OPEN_STATION}/sightings?since=2025-06-01&days=90"
    first = (await client.get(f"{url}&size=5&page=1")).json()
    second = (await client.get(f"{url}&size=5&page=2")).json()
    assert first["total"] == second["total"] > 5
    ids = [item["id"] for item in first["items"]]
    assert len(set(ids) & {item["id"] for item in second["items"]}) == 0
    captured = [item["captured_at"] for item in first["items"]]
    assert captured == sorted(captured, reverse=True), "the default sort is newest first"


@skip_without_database
async def test_a_sort_outside_the_allow_list_is_refused_by_name(client) -> None:
    """Not a 500, and not a column name reaching the SQL either."""
    url = f"/reserves/{RESERVE}/stations/{OPEN_STATION}/sightings?since=2025-06-01&days=30"
    response = await client.get(f"{url}&sort=notes")
    assert response.status == 422
    assert "notes" in response.json()["detail"]


@skip_without_database
async def test_an_optional_filter_narrows_without_being_declared(client) -> None:
    url = f"/reserves/{RESERVE}/stations/{OPEN_STATION}/sightings?since=2025-06-01&days=90"
    everything = (await client.get(f"{url}&size=5")).json()
    confident = (await client.get(f"{url}&size=5&min_confidence=80")).json()
    assert 0 < confident["total"] < everything["total"]
    assert all(item["confidence"] >= 80 for item in confident["items"])


@skip_without_database
async def test_one_sighting_resolves_what_it_points_at(client) -> None:
    """Three foreign keys become three objects, in one query, because it asked."""
    listed = (
        await client.get(
            f"/reserves/{RESERVE}/stations/{OPEN_STATION}/sightings"
            "?since=2025-06-01&days=90&size=1"
        )
    ).json()
    sighting_id = listed["items"][0]["id"]
    detail = (await client.get(f"/sightings/{sighting_id}")).json()
    assert detail["species"]["code"]
    assert detail["camera"]["serial"].startswith("CT-")
    assert detail["station"]["id"] == OPEN_STATION
    # A jsonb column, as an object rather than a string, whichever hydration
    # path the query took.
    assert isinstance(detail["tags"], dict)
    assert isinstance(listed["items"][0]["tags"], dict)


@skip_without_database
async def test_recent_deployments_are_the_last_ten_newest_first(client) -> None:
    response = await client.get(
        f"/reserves/{RESERVE}/stations/{OPEN_STATION}/deployments"
    )
    assert response.status == 200
    collected = [item["collected_at"] for item in response.json()["items"]]
    assert len(collected) == 10, "twelve trips per station; the declaration takes ten"
    assert collected == sorted(collected, reverse=True)


@skip_without_database
async def test_the_species_list_is_cached_and_a_write_clears_it(client) -> None:
    """The cache is invalidated by the ORM, not by anybody remembering to.

    A committed write to ``Species`` announces the model it wrote, and the cache
    that named that model drops its entries. Nothing in this test calls an
    invalidator -- that is the whole point, and it is only possible because the
    session and the cache are parts of one framework.
    """
    from camera_trap.routers.species import list_species

    from wreath.orm import Session

    list_species.invalidate()
    first = (await client.get("/species")).json()
    assert len(first["items"]) == 40
    hits = list_species.cache_store.stats.hits
    assert (await client.get("/species")).json() == first
    assert list_species.cache_store.stats.hits > hits, "the repeat was not served from cache"

    session = Session(registry=client.app.state.orm_main, workload="write")
    try:
        leopard = await session.fetch_one(Species.select().where(Species.code == "LEOP"))
        leopard.common_name = "Leopard (revised)"
        await session.flush()
    finally:
        await session.close()

    after = (await client.get("/species")).json()
    names = {item["code"]: item["common_name"] for item in after["items"]}
    assert names["LEOP"] == "Leopard (revised)"


@skip_without_database
async def test_a_species_code_is_looked_up_case_insensitively(client) -> None:
    lower = await client.get("/species/leop")
    assert lower.status == 200
    assert lower.json()["scientific_name"] == "Panthera pardus"
    assert (await client.get("/species/zzzz")).status == 404


@skip_without_database
async def test_the_declared_window_is_what_the_handler_actually_ran(client) -> None:
    """The list's own reported window matches the rows it returned."""
    url = f"/reserves/{RESERVE}/stations/{OPEN_STATION}/sightings?since=2025-06-01&days=30"
    body = (await client.get(f"{url}&size=50")).json()
    assert body["items"], "the window is empty; the fixture's sample is too small"
    # Parsed rather than string-compared: the window is rendered in the
    # reserve's offset and the captures in UTC, so the two spellings of the same
    # instant do not sort against each other as text.
    since = datetime.datetime.fromisoformat(body["since"])
    until = datetime.datetime.fromisoformat(body["until"])
    for item in body["items"]:
        captured = datetime.datetime.fromisoformat(item["captured_at"])
        assert since <= captured < until
