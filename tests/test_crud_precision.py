from __future__ import annotations

import json

import pytest

from wreath.auth import Identity
from wreath.authorization import CedarAuthorizer, CedarPolicies, PrecisionLadder, coarsen
from wreath.crud import crud_router
from wreath.geospatial import Coordinate

STATION = Coordinate(lat=-23.6980, lon=133.8807)

LADDER = PrecisionLadder(
    ("Station::locate_exact", None),
    ("Station::locate_fine", 1_000),
    ("Station::locate_coarse", 10_000),
)

POLICY = """
permit (principal in Role::"ranger", action == Action::"Station::locate_exact",
        resource == Station::"*");
permit (principal in Role::"partner", action == Action::"Station::locate_fine",
        resource == Station::"*");
permit (principal in Role::"volunteer", action == Action::"Station::locate_coarse",
        resource == Station::"*");
"""


def _model():
    """A model whose `location` is a real geospatial `Point` column.

    PostgreSQL's `point` stores x=lon, y=lat -- the inverse of `Coordinate`'s
    keyword order, and deliberately so, because PostGIS, GeoJSON and `point`
    all put longitude first while only humans say "lat, lon". The ORM codec
    owns that transposition; nothing in this slice re-does it, which is why
    `serialize` reads a `Coordinate` and never a pair.
    """
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Point, Text

    class Station(Model, table="precision_stations"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        location: Mapped[Coordinate] = column(Point, nullable=True)

    return Station


def _Row(id, name, location):
    """One row of the model above."""
    return _model()(id=id, name=name, location=location)


class _Null:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    async def get(self, model, pk):
        return self.rows.get(pk)

    async def fetch(self, query):
        return list(self.rows.values())

    def begin(self):
        return _Null()

    async def close(self):
        pass


class _State:
    """The slice of `request.state` the precision cache uses."""

    def __init__(self):
        self._values = {}

    def get(self, name, default=None):
        return self._values.get(name, default)

    def __setattr__(self, name, value):
        if name == "_values":
            object.__setattr__(self, name, value)
        else:
            self._values[name] = value


class _App:
    def __init__(self, authorizer):
        self._authorizer = authorizer


class _Req:
    def __init__(self, app, identity, path_params=None):
        self.app = app
        self.identity = identity
        self.path_params = path_params or {}
        self.query_string = b""
        self.state = _State()
        self.method = "GET"
        self.path = "/station/1"


def _routes(router):
    return {(r.methods[0], r.path): r.endpoint for r in router.routes}


def _setup(roles):
    Station = _model()
    station = _Row(1, "Waterhole", STATION)
    router = crud_router(
        Station,
        lambda request: _FakeSession({1: station}),
        precision={"location": LADDER},
    )
    app = _App(CedarAuthorizer(engine=CedarPolicies(POLICY)))
    identity = Identity("alice", roles=frozenset(roles))
    return _routes(router)[("GET", "/station/{id}")], _Req(app, identity, {"id": "1"})


async def test_a_ranger_sees_the_exact_position():
    retrieve, request = _setup(["ranger"])
    data = json.loads((await retrieve(request)).body)
    assert data["location"] == {"lat": STATION.lat, "lon": STATION.lon}


async def test_a_partner_sees_a_one_kilometre_cell():
    retrieve, request = _setup(["partner"])
    data = json.loads((await retrieve(request)).body)
    expected = coarsen(STATION, 1_000)
    assert data["location"] == {"lat": expected.lat, "lon": expected.lon}
    assert data["location"] != {"lat": STATION.lat, "lon": STATION.lon}


async def test_a_volunteer_sees_a_ten_kilometre_cell():
    retrieve, request = _setup(["volunteer"])
    data = json.loads((await retrieve(request)).body)
    expected = coarsen(STATION, 10_000)
    assert data["location"] == {"lat": expected.lat, "lon": expected.lon}


async def test_the_public_sees_no_location_key_at_all():
    retrieve, request = _setup([])
    data = json.loads((await retrieve(request)).body)
    assert "location" not in data
    assert data["name"] == "Waterhole"


async def test_the_degraded_value_is_stable_across_repeated_requests():
    seen = set()
    for _ in range(25):
        retrieve, request = _setup(["volunteer"])
        data = json.loads((await retrieve(request)).body)
        seen.add((data["location"]["lat"], data["location"]["lon"]))
    assert len(seen) == 1, f"the degraded position moved between requests: {seen}"


async def test_a_coarser_caller_cannot_recover_the_finer_answer():
    volunteer, vreq = _setup(["volunteer"])
    ranger, rreq = _setup(["ranger"])
    coarse = json.loads((await volunteer(vreq)).body)["location"]
    exact = json.loads((await ranger(rreq)).body)["location"]
    assert coarse != exact


async def test_a_list_response_degrades_every_row():
    Station = _model()
    rows = {
        1: _Row(1, "A", STATION),
        2: _Row(2, "B", STATION),
    }
    router = crud_router(
        Station,
        lambda request: _FakeSession(rows),
        precision={"location": LADDER},
    )
    app = _App(CedarAuthorizer(engine=CedarPolicies(POLICY)))
    request = _Req(app, Identity("alice", roles=frozenset(["volunteer"])))
    list_ = _routes(router)[("GET", "/station")]
    data = json.loads((await list_(request)).body)
    assert len(data["items"]) == 2
    for item in data["items"]:
        assert item["location"] == {
            "lat": coarsen(STATION, 10_000).lat,
            "lon": coarsen(STATION, 10_000).lon,
        }


async def test_no_authorizer_withholds_rather_than_publishes():
    Station = _model()
    station = _Row(1, "Waterhole", STATION)
    router = crud_router(
        Station,
        lambda request: _FakeSession({1: station}),
        precision={"location": LADDER},
    )
    request = _Req(_App(None), Identity("alice", roles=frozenset(["ranger"])), {"id": "1"})
    retrieve = _routes(router)[("GET", "/station/{id}")]
    data = json.loads((await retrieve(request)).body)
    assert "location" not in data


async def test_the_ladder_is_asked_once_per_request_not_once_per_row():
    Station = _model()
    rows = {i: _Row(i, f"S{i}", STATION) for i in range(1, 11)}

    class CountingAuthorizer(CedarAuthorizer):
        asks = 0

        async def authorize(self, request, requirement):
            type(self).asks += 1
            return await super().authorize(request, requirement)

    CountingAuthorizer.asks = 0
    router = crud_router(
        Station,
        lambda request: _FakeSession(rows),
        precision={"location": LADDER},
    )
    app = _App(CountingAuthorizer(engine=CedarPolicies(POLICY)))
    request = _Req(app, Identity("alice", roles=frozenset(["volunteer"])))
    list_ = _routes(router)[("GET", "/station")]
    data = json.loads((await list_(request)).body)

    assert len(data["items"]) == 10
    # Three rungs, asked once for the request: the volunteer is denied exact and
    # fine before being permitted coarse. Ten rows must not multiply that.
    assert CountingAuthorizer.asks == 3, f"asked {CountingAuthorizer.asks} times for 10 rows"


async def test_resolving_one_ladder_twice_in_a_request_asks_once():
    from wreath._auth.geofence import resolve_precision

    class CountingAuthorizer(CedarAuthorizer):
        asks = 0

        async def authorize(self, request, requirement):
            type(self).asks += 1
            return await super().authorize(request, requirement)

    CountingAuthorizer.asks = 0
    authorizer = CountingAuthorizer(engine=CedarPolicies(POLICY))
    request = _Req(_App(authorizer), Identity("alice", roles=frozenset(["ranger"])))

    first = await resolve_precision(request, authorizer, LADDER, 'Station::"*"')
    after = CountingAuthorizer.asks
    second = await resolve_precision(request, authorizer, LADDER, 'Station::"*"')

    assert first is None and second is None  # exact, on the first rung
    assert CountingAuthorizer.asks == after, "the second resolution re-asked Cedar"


async def test_a_withheld_answer_is_cached_too_rather_than_re_resolved():
    from wreath._auth.geofence import WITHHELD, resolve_precision

    class CountingAuthorizer(CedarAuthorizer):
        asks = 0

        async def authorize(self, request, requirement):
            type(self).asks += 1
            return await super().authorize(request, requirement)

    CountingAuthorizer.asks = 0
    authorizer = CountingAuthorizer(engine=CedarPolicies(POLICY))
    request = _Req(_App(authorizer), Identity("alice", roles=frozenset()))

    assert await resolve_precision(request, authorizer, LADDER, 'Station::"*"') is WITHHELD
    after = CountingAuthorizer.asks
    assert await resolve_precision(request, authorizer, LADDER, 'Station::"*"') is WITHHELD
    assert CountingAuthorizer.asks == after


async def test_a_row_with_no_location_stays_null_rather_than_being_coarsened():
    Station = _model()
    router = crud_router(
        Station,
        lambda request: _FakeSession({1: _Row(1, "Nowhere", None)}),
        precision={"location": LADDER},
    )
    app = _App(CedarAuthorizer(engine=CedarPolicies(POLICY)))
    request = _Req(app, Identity("alice", roles=frozenset(["volunteer"])), {"id": "1"})
    retrieve = _routes(router)[("GET", "/station/{id}")]
    data = json.loads((await retrieve(request)).body)
    assert data["location"] is None


async def test_a_router_with_no_ladder_does_no_authorization_work():
    Station = _model()

    class CountingAuthorizer(CedarAuthorizer):
        asks = 0

        async def authorize(self, request, requirement):
            type(self).asks += 1
            return await super().authorize(request, requirement)

    CountingAuthorizer.asks = 0
    router = crud_router(Station, lambda request: _FakeSession({1: _Row(1, "A", STATION)}))
    app = _App(CountingAuthorizer(engine=CedarPolicies(POLICY)))
    request = _Req(app, Identity("alice", roles=frozenset(["ranger"])), {"id": "1"})
    data = json.loads((await _routes(router)[("GET", "/station/{id}")](request)).body)

    assert CountingAuthorizer.asks == 0
    assert data["location"] == {"lat": STATION.lat, "lon": STATION.lon}


def test_a_ladder_on_an_unserialized_column_is_refused():
    Station = _model()
    with pytest.raises(ValueError, match="does not.*serialize|not serialize"):
        crud_router(
            Station,
            lambda request: _FakeSession(),
            precision={"nonexistent": LADDER},
        )


def test_a_ladder_on_a_sensitive_column_is_refused_because_it_is_withheld():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Secretive(Model, table="precision_secretive"):
        id: Mapped[int] = column(Int64, primary_key=True)
        api_token: Mapped[str] = column(Text, nullable=True)

    with pytest.raises(ValueError, match="not serialize"):
        crud_router(
            Secretive,
            lambda request: _FakeSession(),
            precision={"api_token": LADDER},
        )
