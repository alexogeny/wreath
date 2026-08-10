"""The same request, four principals, four answers — over HTTP this time.

`test_policy.py` asserts the grid against the Cedar engine with no database and
no HTTP. This file asserts that the *application* applies it: that a handler
reads the identity it was given, coarsens with it, and puts an honest
``precision_m`` beside the result.

Both are needed, and the gap between them is where the bugs live. The policy
test would pass unchanged on an application that forgot `@identify()` on its
public routes -- and that application would silently treat every reader as the
public, which is the failure this file exists to catch.
"""

from __future__ import annotations

import os

import pytest

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the tracking API tests",
)

#: Nashipae, a leopard: `sensitive`, which is the tier with a full ladder.
SENSITIVE = 3

#: Sarara, a plains zebra: `open`.
OPEN = 8

#: Naserian, a black rhinoceros: `restricted`.
RESTRICTED = 1

#: One day well inside the seeded window.
WINDOW = "since=2026-03-10&days=1"


@pytest.fixture(scope="module")
def seeded_schema():
    """Build and seed this worker's schema once for the whole file.

    **Every test below only reads** -- the file issues sixteen `.get()`s and no
    write of any kind -- which is the precondition for sharing and the reason
    this is safe here rather than generally. A test that wrote would see its
    neighbours' rows, and the failure would be order-dependent, so if one is
    added it builds its own schema instead of joining this.

    The seed was previously rebuilt per test: 1.7s of `setup` on each of
    nineteen tests made this the slowest file in the suite at 87.3s.

    Deliberately synchronous, driving its own loop with `asyncio.run`, following
    `test_place.py`: the tests are function-scoped and async, so each gets its
    own event loop, and only the DDL is shared here. The `TestClient` below stays
    per test, where its loop affinity is correct by construction.
    """
    import asyncio

    from _tracking import build_schema, drop_schema

    from wreath.postgres import connect

    async def _apply(step):
        connection = await connect(_DSN)
        try:
            await step(connection)
        finally:
            await connection.close()

    asyncio.run(_apply(build_schema))
    try:
        yield
    finally:
        asyncio.run(_apply(drop_schema))


@pytest.fixture
async def client(seeded_schema):
    """The application on the schema `seeded_schema` built."""
    from tracking.app import build

    from wreath.testing import TestClient

    application = build(cross_worker=False)
    async with TestClient(application) as test_client:
        yield test_client


def acting(client, role: str | None):
    """A client acting as one of the four principals.

    `None` is the public: the same client with no identity, which is what an
    unauthenticated browser is. Not a role with no permissions -- the two are
    different principals and the policy set distinguishes them.
    """
    if role is None:
        return client
    return client.acting_as(f"{role}-1", roles=[role], type="Observer")


async def one_fix(client, role: str | None, animal: int) -> dict:
    """The first fix of a day, as this principal is shown it."""
    response = await acting(client, role).get(f"/animals/{animal}/track?{WINDOW}")
    assert response.status == 200, response.text
    body = response.json()
    assert body["fixes"], "the seed must put fixes in this window"
    return body["fixes"][0]


# -- one test per principal ---------------------------------------------------


@skip_without_database
async def test_a_ranger_is_shown_what_the_collar_said(client) -> None:
    """Exact, with the collar's own accuracy estimate beside it.

    The accuracy is only ever published at full resolution: "10 km cell, accurate
    to 18 m" is a contradiction a client would resolve in favour of the smaller
    number, and drawing an 18 m circle round a 10 km answer is exactly the map a
    coarsened coordinate exists to prevent.
    """
    fix = await one_fix(client, "ranger", SENSITIVE)
    assert fix["precision_m"] == 0.0
    assert "accuracy_m" in fix
    assert -3.0 < fix["position"]["lat"] < -1.0
    assert 35.0 < fix["position"]["lon"] < 37.0


@skip_without_database
async def test_a_partner_is_shown_a_kilometre(client) -> None:
    """One kilometre, and no accuracy estimate to undo it."""
    fix = await one_fix(client, "partner", SENSITIVE)
    assert fix["precision_m"] == 1_000.0
    assert "accuracy_m" not in fix


@skip_without_database
async def test_a_volunteer_is_shown_ten_kilometres(client) -> None:
    """Ten kilometres: which end of the conservancy, and nothing sharper."""
    fix = await one_fix(client, "volunteer", SENSITIVE)
    assert fix["precision_m"] == 10_000.0
    assert "accuracy_m" not in fix


@skip_without_database
async def test_the_public_is_shown_no_position_at_all(client) -> None:
    """**Absent, not null, and the rest of the event survives.**

    `"position": null` is a different claim -- it says this fix has no
    coordinates, which is false, and a client plotting it would put a leopard at
    the intersection of the equator and the prime meridian. An absent key says
    "not for you", and a client can tell the difference.

    Everything that is not a location stays, because a reader who may not be
    told where an animal is may still be told that it is alive and that its
    collar has battery.
    """
    fix = await one_fix(client, None, SENSITIVE)
    assert "position" not in fix
    assert "precision_m" not in fix
    assert fix["battery_pct"] > 0
    assert fix["recorded_at"]


@skip_without_database
async def test_the_four_answers_are_four_different_answers(client) -> None:
    """The ladder, in one request each, asserted as a set.

    Separate from the four tests above on purpose: those would all pass on an
    application that returned the *same* coarse answer to a partner and a
    volunteer while labelling them differently.
    """
    plotted = {}
    for role in ("ranger", "partner", "volunteer"):
        fix = await one_fix(client, role, SENSITIVE)
        plotted[role] = (fix["position"]["lat"], fix["position"]["lon"])
    assert len(set(plotted.values())) == 3

    exact = plotted["ranger"]
    from wreath.geospatial import Coordinate, distance

    truth = Coordinate(lat=exact[0], lon=exact[1])
    coarse = distance(truth, Coordinate(lat=plotted["partner"][0], lon=plotted["partner"][1]))
    rough = distance(
        truth, Coordinate(lat=plotted["volunteer"][0], lon=plotted["volunteer"][1])
    )
    assert coarse <= 1_000.0
    assert rough <= 10_000.0
    assert rough > coarse


# -- the property that makes degradation worth anything -----------------------


@skip_without_database
async def test_a_degraded_position_is_stable_across_repeated_requests(client) -> None:
    """**Asking twenty times must not converge on the truth.**

    This is the assertion the whole scheme rests on. A coarsening implemented as
    random jitter passes every other test in this file -- the value is nearby,
    it carries the right `precision_m`, it differs per grade -- and is completely
    defeated by a `for` loop and a mean. A grid is not, because there is only
    ever one answer to average.
    """
    volunteer = acting(client, "volunteer")
    seen = set()
    for _ in range(20):
        body = (await volunteer.get(f"/animals/{SENSITIVE}/track?{WINDOW}")).json()
        seen.add((body["fixes"][0]["position"]["lat"], body["fixes"][0]["position"]["lon"]))
    assert len(seen) == 1, "twenty requests produced more than one answer"


@skip_without_database
async def test_a_whole_days_track_collapses_onto_a_handful_of_cells(client) -> None:
    """Coarsening has to lose information, not merely round each point.

    Seventy-two fixes over a day, from an animal that stays inside a home range
    a few kilometres across, must come back as a small number of distinct 10 km
    cells. A "coarsening" that returned seventy-two nearby-but-distinct points
    would let a reader recover the track's shape, and the shape is the thing.
    """
    body = (
        await acting(client, "volunteer").get(f"/animals/{SENSITIVE}/track?{WINDOW}")
    ).json()
    assert len(body["fixes"]) == 72
    cells = {
        (fix["position"]["lat"], fix["position"]["lon"]) for fix in body["fixes"]
    }
    assert len(cells) <= 4, f"a day of 10 km answers should be a few cells, got {len(cells)}"


# -- the tier is on the animal, not the badge ---------------------------------


@skip_without_database
async def test_an_open_animals_track_is_exact_for_everyone_including_the_public(
    client,
) -> None:
    """Half the reason the collars are funded.

    Without this, a policy that simply coarsened everything for non-rangers
    would pass every other test here while making the public map useless.
    """
    for role in ("ranger", "partner", "volunteer", None):
        fix = await one_fix(client, role, OPEN)
        assert fix["precision_m"] == 0.0, f"{role or 'the public'} should see an open track"


@skip_without_database
async def test_a_restricted_animals_position_is_absent_for_everyone_but_a_ranger(
    client,
) -> None:
    """Two rhinos, and no ladder at all.

    A coarse answer here would be worse than none: it would tell a reader which
    quarter of the conservancy to search, which is exactly the intelligence
    being withheld.
    """
    assert (await one_fix(client, "ranger", RESTRICTED))["precision_m"] == 0.0
    for role in ("partner", "volunteer", None):
        assert "position" not in await one_fix(client, role, RESTRICTED)


# -- the summary is not a location --------------------------------------------


@skip_without_database
async def test_how_far_it_walked_is_the_same_number_for_everyone(client) -> None:
    """Distance is not a position, and coarsening it would protect nothing.

    Knowing that a leopard covered 6 km yesterday locates it to within the whole
    conservancy, which is where it already was. Meanwhile it is the one number a
    welfare question turns on, so degrading it would destroy the useful half of
    the response to no benefit.
    """
    answers = set()
    for role in ("ranger", "partner", "volunteer", None):
        body = (
            await acting(client, role).get(f"/animals/{SENSITIVE}/track?{WINDOW}")
        ).json()
        answers.add(body["distance_m"])
    assert len(answers) == 1
    assert answers.pop() > 0.0


# -- the sharpest leak --------------------------------------------------------


@skip_without_database
async def test_a_landmark_distance_is_never_attached_to_a_coarsened_position(
    client,
) -> None:
    """**A distance to a published waterhole is very nearly a position.**

    The waterhole's coordinates are on the visitor map, so "1.2 km from Ndovu"
    puts the animal on a circle of known centre and known radius -- and a 10 km
    cell centre beside it is decoration on an answer accurate to a metre. Every
    half of that looks harmless in review, which is why it is asserted here
    rather than trusted to a reading of the serializer.
    """
    from tracking.seed import CENTRE_LAT, CENTRE_LON, LANDMARKS

    lat = CENTRE_LAT + LANDMARKS[0][3]
    lon = CENTRE_LON + LANDMARKS[0][4]
    path = f"/fixes/near?lat={lat}&lon={lon}&metres=2000"

    ranger = (await acting(client, "ranger").get(path)).json()
    assert ranger["items"], "the seed must put fixes near Ndovu"
    assert any("nearest" in item for item in ranger["items"])

    for role in ("partner", "volunteer", None):
        body = (await acting(client, role).get(path)).json()
        for item in body["items"]:
            if item.get("precision_m", None) != 0.0:
                assert "nearest" not in item, (
                    "a landmark distance beside a coarsened position undoes the "
                    "coarsening completely"
                )


# -- the rest of the surface ---------------------------------------------------


@skip_without_database
async def test_the_roster_says_what_each_animal_will_cost_to_see(client) -> None:
    """A client can grey out a layer it cannot draw, rather than finding out per fix.

    The tier itself is public: a conservancy announces its rhinos, and
    pretending otherwise would make the programme unfundable. It is *where* they
    are that is withheld.
    """
    body = (await acting(client, None).get("/animals")).json()
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[OPEN]["precision_m"] == 0.0
    assert "precision_m" not in by_id[RESTRICTED]
    assert by_id[RESTRICTED]["protection"] == "restricted"
    assert by_id[RESTRICTED]["name"] == "Naserian"


@skip_without_database
async def test_a_coordinate_that_cannot_be_a_place_is_refused_by_name(client) -> None:
    """`Coordinate` refuses a latitude past the pole, and the handler says so.

    The honest answer is a 4xx naming the field rather than an empty list -- an
    empty list reads as "nothing near there", which is true and useless.

    Note what this test *cannot* check, and why the type is keyword-only. A
    caller who simply swaps this conservancy's two numbers asks about
    `lat=36.10, lon=-1.97`, and both are perfectly legal coordinates: that is a
    place in Turkey, and nothing anywhere can tell it was a mistake. Refusing an
    out-of-range value catches the typo; only naming the arguments catches the
    swap, which is the argument `wreath.geospatial.Coordinate` is built on.
    """
    response = await acting(client, "ranger").get("/fixes/near?lat=136.1&lon=-1.97")
    assert response.status == 400
    assert "lat" in response.text


@skip_without_database
async def test_a_search_wide_enough_to_return_everything_is_refused(client) -> None:
    """The rectangle bound is a refusal, not a truncation -- over HTTP too."""
    from tracking.seed import CENTRE_LAT, CENTRE_LON

    response = await acting(client, "ranger").get(
        f"/fixes/near?lat={CENTRE_LAT}&lon={CENTRE_LON}&metres=50000"
    )
    assert response.status == 400
    assert "narrow the radius" in response.text


@skip_without_database
async def test_an_animal_that_does_not_exist_is_a_404_on_both_routes(client) -> None:
    """A 404, and it is checked before the window is read.

    Both animal routes resolve the row first, so an unknown id costs one lookup
    rather than a series query over a range that can never have rows in it.
    """
    for path in ("track?since=2026-03-10&days=1", "daily?since=2026-03-10&days=2"):
        response = await acting(client, "ranger").get(f"/animals/9999/{path}")
        assert response.status == 404, path
        assert "9999" in response.text


@skip_without_database
async def test_a_window_with_no_fixes_has_no_speed_rather_than_zero(client) -> None:
    """`speed: null` and `distance_m: 0.0` are different claims and both are true.

    A division that cannot be performed has no answer; zero would read as
    "stationary", which a welfare dashboard draws as a dead animal. The distance
    *is* zero, because no legs were walked, and that is a fact.
    """
    body = (
        await acting(client, "ranger").get(
            f"/animals/{OPEN}/track?since=2027-06-01&days=1"
        )
    ).json()
    assert body["fixes"] == []
    assert body["distance_m"] == 0.0
    assert body["speed_ms"] is None


@skip_without_database
async def test_the_daily_chart_names_every_day_and_how_far_it_is_settled(client) -> None:
    """Every bucket present, and an envelope that says where the watermark fell.

    A chart that dropped silent days would let every caller reinvent the same
    interpolation slightly differently, and one that did not report
    `sealed_through` would leave a reader unable to tell a settled number from a
    recomputed one.
    """
    body = (
        await acting(client, "ranger").get(
            f"/animals/{OPEN}/daily?since=2026-03-10&days=4"
        )
    ).json()
    assert len(body["days"]) == 4
    assert body["zone"] == "Africa/Nairobi"
    assert all(day["fixes"] == 72 for day in body["days"])
    assert all(day["distance_m"] > 0.0 for day in body["days"])
    assert body["sealed_through"] is not None
    assert body["corrections"] == []


@skip_without_database
async def test_asking_for_more_neighbours_than_the_route_allows_is_refused(
    client,
) -> None:
    """The ceiling is bound at declaration, so it is refused before a handler runs.

    Fifty is the point past which "nearest" stops being a question about a place
    and becomes a page of the table — which `/fixes/near` already answers, with
    a radius the caller chose rather than a count.
    """
    response = await acting(client, "ranger").get(
        "/fixes/nearest?lat=-1.97&lon=36.10&count=500"
    )
    assert response.status == 422, response.text


@skip_without_database
async def test_the_nearest_route_answers_in_distance_order(client) -> None:
    """The tier-1 nearest-neighbour answer, over HTTP."""
    from tracking.seed import CENTRE_LAT, CENTRE_LON, LANDMARKS

    lat = CENTRE_LAT + LANDMARKS[5][3]
    lon = CENTRE_LON + LANDMARKS[5][4]
    body = (
        await acting(client, "ranger").get(f"/fixes/nearest?lat={lat}&lon={lon}&count=4")
    ).json()
    assert len(body["items"]) == 4
    metres = [item["nearest"]["distance_m"] for item in body["items"] if "nearest" in item]
    assert metres, "a ranger sees the landmark distance"
