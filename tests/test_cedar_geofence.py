from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, CedarPolicies, Regions, authorize
from wreath.geospatial import BoundingBox, Coordinate

# Alice Springs, and a depot 2 km north of it.
TOWN = Coordinate(lat=-23.700, lon=133.880)
DEPOT = Coordinate(lat=-23.682, lon=133.880)
FAR = Coordinate(lat=-25.000, lon=131.000)

REGIONS = Regions(
    depot=(DEPOT, 5_000.0),
    reserve=BoundingBox(-24.0, -23.0, 133.0, 134.0),
    outback=BoundingBox(-26.0, -25.0, 130.0, 132.0),
)

GEOFENCED = """
permit (principal, action == Action::"read", resource)
when { context.regions.contains("depot") };
"""


def _app(source, *, regions=REGIONS, at=None, roles=("driver",)):
    """An app whose only route is guarded by `source`, with the caller at `at`."""
    app = Wreath()

    async def verify(token: str) -> Identity | None:
        return Identity(token, roles=frozenset(roles)) if token == "alice" else None

    @app.get("/vehicles")
    @authorize(action="read", resource="Vehicle::*")
    async def vehicles(request: Any) -> dict:
        return {"ok": True}

    app.configure_auth(
        BearerTokenBackend(verify),
        CedarAuthorizer(
            engine=CedarPolicies(source),
            regions=regions,
            location=lambda request: at,
        ),
    )
    return app


async def _status(app: Wreath) -> int:
    """Drive one authenticated GET and return its status."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/vehicles",
            "headers": [(b"authorization", b"Bearer alice")],
        },
        receive,
        send,
    )
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.asyncio
async def test_a_caller_inside_the_region_is_permitted():
    assert await _status(_app(GEOFENCED, at=TOWN)) == 200


@pytest.mark.asyncio
async def test_the_same_caller_outside_the_region_is_denied():
    assert await _status(_app(GEOFENCED, at=FAR)) == 403


@pytest.mark.asyncio
async def test_a_caller_with_no_location_is_denied():
    assert await _status(_app(GEOFENCED, at=None)) == 403


@pytest.mark.asyncio
async def test_a_policy_can_test_a_region_the_caller_is_only_in_by_box():
    source = """
    permit (principal, action == Action::"read", resource)
    when { context.regions.contains("reserve") };
    """
    assert await _status(_app(source, at=TOWN)) == 200
    assert await _status(_app(source, at=FAR)) == 403


@pytest.mark.asyncio
async def test_an_unless_geofence_still_forbids_when_no_provider_is_configured():
    source = """
    permit (principal, action == Action::"read", resource);
    forbid (principal, action == Action::"read", resource)
    unless { context.regions.contains("depot") };
    """
    assert await _status(_app(source, regions=None, at=TOWN)) == 403


@pytest.mark.asyncio
async def test_an_unless_geofence_permits_a_caller_actually_inside():
    source = """
    permit (principal, action == Action::"read", resource);
    forbid (principal, action == Action::"read", resource)
    unless { context.regions.contains("depot") };
    """
    assert await _status(_app(source, at=TOWN)) == 200
    assert await _status(_app(source, at=FAR)) == 403


def test_the_engine_skips_an_unless_forbid_when_the_key_is_absent():
    engine = CedarPolicies(
        "permit (principal, action, resource);\n"
        "forbid (principal, action, resource)\n"
        'unless { context.regions.contains("depot") };'
    )
    from wreath.authorization import EntityUid

    def ask(context):
        return engine.is_authorized(
            principal=EntityUid("User", "alice"),
            action=EntityUid("Action", "read"),
            resource=EntityUid("Site", "s1"),
            context=context,
            entities=(),
        ).allowed

    assert ask({}) is True  # absent: the forbid is skipped
    assert ask({"regions": frozenset()}) is False  # empty: the forbid stands


class CountingRegions:
    """A `Regions` that records how many times a request resolved a position."""

    def __init__(self) -> None:
        self.calls: list[Coordinate] = []
        self.asked: list[frozenset[str] | None] = []

    def names(self) -> frozenset[str]:
        return REGIONS.names()

    def containing(self, point, names=None):
        self.calls.append(point)
        self.asked.append(names)
        return REGIONS.containing(point, names)


class CountingEngine:
    """Wraps the real engine and counts evaluations, so a test can prove it
    exercised the many-evaluations path before asserting anything about caching."""

    def __init__(self, source: str) -> None:
        self._engine = CedarPolicies(source)
        self.evaluations = 0

    def referenced_flags(self):
        return self._engine.referenced_flags()

    def referenced_regions(self):
        return self._engine.referenced_regions()

    def is_authorized(self, **query):
        self.evaluations += 1
        return self._engine.is_authorized(**query)


MANIFEST_POLICY = """
permit (principal in Role::"driver", action, resource)
when { context.regions.contains("depot") };
"""


def _manifest_app(regions, engine, at):
    """An app with several actions across two resource types, plus the manifest.

    The manifest endpoint asks the authorizer once per (resource type, action),
    which is what drives many evaluations through one request -- a single
    `@authorize` is a single `is_authorized` call, because `CedarPolicies`
    evaluates the whole policy set at once.
    """
    from wreath.authorization import permissions_router

    app = Wreath()

    async def verify(token: str) -> Identity | None:
        return Identity(token, roles=frozenset({"driver"})) if token == "alice" else None

    @app.get("/vehicles/{vehicle_id}")
    @authorize(action="Vehicle::read", resource="Vehicle::*")
    async def read_vehicle(request: Any) -> dict:
        return {"ok": True}

    @app.delete("/vehicles/{vehicle_id}")
    @authorize(action="Vehicle::delete", resource="Vehicle::*")
    async def delete_vehicle(request: Any) -> dict:
        return {"ok": True}

    @app.get("/sites/{site_id}")
    @authorize(action="Site::read", resource="Site::*")
    async def read_site(request: Any) -> dict:
        return {"ok": True}

    @app.delete("/sites/{site_id}")
    @authorize(action="Site::delete", resource="Site::*")
    async def delete_site(request: Any) -> dict:
        return {"ok": True}

    app.configure_auth(
        BearerTokenBackend(verify),
        CedarAuthorizer(engine=engine, regions=regions, location=lambda request: at),
    )
    app.include_router(permissions_router(app))
    return app


@pytest.mark.asyncio
async def test_regions_are_resolved_once_however_many_policies_evaluate():
    from wreath.testing import TestClient

    regions = CountingRegions()
    engine = CountingEngine(MANIFEST_POLICY)
    app = _manifest_app(regions, engine, TOWN)

    async with TestClient(app) as client:
        response = await client.acting_as("alice", roles=["driver"]).get("/permissions/manifest")

    assert response.status == 200
    assert engine.evaluations > 1, (
        "the manifest made one evaluation, so this asserts nothing about caching"
    )
    assert len(regions.calls) == 1, (
        f"resolved the position {len(regions.calls)} times in one request"
    )


def test_a_literal_geofence_names_only_the_regions_it_tests():
    engine = CedarPolicies(GEOFENCED)
    assert engine.referenced_regions() == frozenset({"depot"})


def test_a_policy_naming_no_region_costs_nothing():
    engine = CedarPolicies("permit (principal, action, resource);")
    assert engine.referenced_regions() == frozenset()


def test_a_computed_region_argument_withholds_the_list():
    engine = CedarPolicies(
        "permit (principal, action, resource)\nwhen { context.regions.contains(resource.site) };"
    )
    assert engine.referenced_regions() is None


def test_isempty_withholds_the_list_too():
    engine = CedarPolicies(
        "permit (principal, action, resource)\nwhen { context.regions.isEmpty() };"
    )
    assert engine.referenced_regions() is None


def test_flags_and_regions_do_not_read_each_other():
    engine = CedarPolicies(
        "permit (principal, action, resource)\n"
        'when { context.flags.contains("beta") && '
        'context.regions.contains("depot") };'
    )
    assert engine.referenced_flags() == frozenset({"beta"})
    assert engine.referenced_regions() == frozenset({"depot"})


@pytest.mark.asyncio
async def test_only_the_named_regions_are_measured():
    asked = []

    class RecordingRegions:
        def names(self):
            return REGIONS.names()

        def containing(self, point, names=None):
            asked.append(names)
            return REGIONS.containing(point, names)

    assert await _status(_app(GEOFENCED, regions=RecordingRegions(), at=TOWN)) == 200
    assert asked == [frozenset({"depot"})]


@pytest.mark.asyncio
async def test_a_computed_argument_measures_every_declared_region():
    asked = []

    class RecordingRegions:
        def names(self):
            return REGIONS.names()

        def containing(self, point, names=None):
            asked.append(names)
            return REGIONS.containing(point, names)

    source = (
        'permit (principal, action == Action::"read", resource)\n'
        'when { context.regions.contains("depot") || '
        "context.regions.contains(context.path) };"
    )
    await _status(_app(source, regions=RecordingRegions(), at=TOWN))
    assert asked == [None], "an unknowable argument must resolve every region"


def test_a_policy_naming_an_undeclared_region_fails_at_startup():
    source = 'permit (principal, action, resource)\nwhen { context.regions.contains("warehouse") };'
    with pytest.raises(ValueError, match="warehouse"):
        CedarAuthorizer(engine=CedarPolicies(source), regions=REGIONS)


def test_the_startup_refusal_says_why_it_would_have_denied_forever():
    source = 'permit (principal, action, resource)\nwhen { context.regions.contains("waerhouse") };'
    with pytest.raises(ValueError, match="deny forever"):
        CedarAuthorizer(engine=CedarPolicies(source), regions=REGIONS)


def test_a_correct_region_name_boots():
    CedarAuthorizer(engine=CedarPolicies(GEOFENCED), regions=REGIONS)


def test_no_regions_configured_is_not_refused():
    CedarAuthorizer(engine=CedarPolicies(GEOFENCED), regions=None)


def test_a_non_enumerable_region_provider_warns_where_it_is_written():
    class OpaqueRegions:
        def containing(self, point, names=None):
            return frozenset()

    with pytest.warns(RuntimeWarning, match="misspelled region"):
        CedarAuthorizer(engine=CedarPolicies(GEOFENCED), regions=OpaqueRegions())


def test_a_circle_contains_a_point_inside_its_radius():
    assert Regions(depot=(DEPOT, 5_000.0)).containing(TOWN) == frozenset({"depot"})


def test_a_circle_excludes_a_point_outside_its_radius():
    assert Regions(depot=(DEPOT, 500.0)).containing(TOWN) == frozenset()


def test_a_region_name_must_be_a_non_empty_string():
    with pytest.raises(ValueError, match="non-empty strings"):
        Regions({"": (DEPOT, 1_000.0)})


def test_a_radius_must_be_positive():
    with pytest.raises(ValueError, match="positive finite radius"):
        Regions(depot=(DEPOT, 0.0))


def test_a_radius_must_be_numeric():
    with pytest.raises(ValueError, match="numeric radius"):
        Regions(depot=(DEPOT, "5km"))


def test_a_centre_must_be_a_coordinate():
    with pytest.raises(ValueError, match="Coordinate centre"):
        Regions(depot=((-23.7, 133.8), 1_000.0))


def test_a_region_must_be_a_box_or_a_circle_pair():
    with pytest.raises(ValueError, match="BoundingBox or a"):
        Regions(depot="somewhere")


def test_names_enumerates_every_declared_region():
    assert REGIONS.names() == frozenset({"depot", "reserve", "outback"})


def test_a_boolean_radius_is_refused_rather_than_read_as_one_metre():
    with pytest.raises(ValueError, match="numeric radius"):
        Regions(depot=(DEPOT, True))


def test_an_infinite_radius_is_refused():
    with pytest.raises(ValueError, match="positive finite radius"):
        Regions(depot=(DEPOT, float("inf")))


def test_a_three_element_region_is_refused():
    with pytest.raises(ValueError, match="BoundingBox or a"):
        Regions(depot=(DEPOT, 1_000.0, "extra"))


def test_a_non_string_region_name_is_refused():
    with pytest.raises(ValueError, match="non-empty strings"):
        Regions({7: (DEPOT, 1_000.0)})


def test_a_box_region_is_kept_apart_from_a_circle_one():
    mixed = Regions(
        circle=(DEPOT, 5_000.0),
        box=BoundingBox(-24.0, -23.0, 133.0, 134.0),
    )
    assert mixed.containing(TOWN) == frozenset({"circle", "box"})
    assert mixed.containing(FAR) == frozenset()


def test_containing_resolves_only_the_names_it_is_given():
    assert REGIONS.containing(TOWN, ["reserve"]) == frozenset({"reserve"})
    assert REGIONS.containing(TOWN, []) == frozenset()
