from __future__ import annotations

import dataclasses

import pytest

from wreath._replay_plan import (
    CanonicalRequest,
    PlanMode,
    PlanReplayResult,
    _resolve_route,
    _substituted_endpoints,
    replay_endpoint_plan,
)


def test_plan_result_matching_compares_every_owned_response_field() -> None:
    result = PlanReplayResult(
        200,
        ((b"content-type", b"text/plain"),),
        b"body",
        "invoke",
        True,
        False,
    )

    assert result.matches(dataclasses.replace(result))
    assert not result.matches(dataclasses.replace(result, status=201))
    assert not result.matches(dataclasses.replace(result, headers=()))
    assert not result.matches(dataclasses.replace(result, body=b"other"))


@pytest.mark.asyncio
async def test_plan_replay_uses_zero_when_an_app_emits_no_response() -> None:
    class SilentApp:
        async def __call__(self, _scope, _receive, _send) -> None:
            pass

    invoked = await replay_endpoint_plan(SilentApp(), CanonicalRequest("GET", "/"))

    replaceable = SilentApp()
    replaceable._routes = []
    replaced = await replay_endpoint_plan(
        replaceable,
        CanonicalRequest("GET", "/"),
        mode=PlanMode.REPLACE,
        recorded_return="unused",
    )

    assert invoked.status == 0
    assert replaced.status == 0


def test_endpoint_substitution_preserves_routes_without_an_endpoint() -> None:
    class App:
        def __init__(self) -> None:
            self._routes = [object()]
            self._dirty = False

    app = App()
    route = app._routes[0]

    with _substituted_endpoints(app, "recorded", None):
        assert app._routes == [route]

    assert app._routes == [route]
    assert app._dirty


def test_route_resolution_compiles_only_a_dirty_application() -> None:
    class CleanApp:
        _dirty = False
        _all_capability_mask = 7

        def _route_match(self, method, path, mask):
            assert (method, path, mask) == ("GET", "/ready", 7)
            return object()

        def _compile_routes(self) -> None:
            raise AssertionError("a clean route table must be reused")

    class DirtyApp:
        _dirty = True
        _all_capability_mask = 0
        _route_match = None

        def _compile_routes(self) -> None:
            self._dirty = False
            self._route_match = lambda _method, _path, _mask: object()

    canonical = CanonicalRequest("GET", "/ready")
    dirty = DirtyApp()

    assert _resolve_route(CleanApp(), canonical)
    assert _resolve_route(dirty, canonical)
    assert not dirty._dirty


def test_route_resolution_without_a_matcher_is_a_miss() -> None:
    assert not _resolve_route(object(), CanonicalRequest("GET", "/missing"))
