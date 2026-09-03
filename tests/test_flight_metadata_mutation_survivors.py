from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from wreath._flight_metadata import build_metadata_image
from wreath.auth import authenticated


class _Widget:
    pass


def _app(
    *,
    routes: tuple[Any, ...] = (),
    specs: tuple[Any, ...] = (),
    requirements: tuple[Any, ...] = (),
    **attributes: Any,
) -> Any:
    image = SimpleNamespace(
        routes=lambda: routes,
        binding_specs=lambda: specs,
        requirements=lambda: requirements,
        operation_id=lambda definition, method: f"{method}:{definition.path}",
    )
    return SimpleNamespace(_application_image=image, **attributes)


def test_none_optional_collections_are_normalized_to_empty() -> None:
    image = build_metadata_image(
        _app(
            _databases=None,
            _orm_registries=None,
            _http_clients=None,
            _ws_routes=None,
        )
    )

    assert image.databases == ()
    assert image.models == ()
    assert image.clients == ()
    assert image.routes == ()


def test_registered_orm_models_reach_the_metadata_table() -> None:
    registry = SimpleNamespace(specs=(SimpleNamespace(model_type=_Widget),))

    image = build_metadata_image(_app(_orm_registries={"main": registry}))

    assert [entry.name for entry in image.models] == ["_Widget"]


def test_route_and_binding_dependencies_are_combined() -> None:
    def route_dependency() -> None:
        return None

    def binding_dependency() -> None:
        return None

    definition = SimpleNamespace(
        dependencies=(SimpleNamespace(fn=route_dependency),),
        middleware=(),
        methods=("GET",),
        path="/items",
        tags=(),
    )
    spec = SimpleNamespace(
        depends=((None, SimpleNamespace(fn=binding_dependency)),),
        path_params=(),
        query_params=(),
        header_params=(),
        cookie_params=(),
        form_params=(),
        file_params=(),
        body=None,
        returns=None,
        query_constraints=(),
    )

    image = build_metadata_image(
        _app(
            routes=(definition,),
            specs=(spec,),
            requirements=(SimpleNamespace(),),
        )
    )

    names = {entry.name for entry in image.dependencies}
    assert {name.rsplit(".", 1)[-1] for name in names} == {
        "binding_dependency",
        "route_dependency",
    }
    assert len(image.routes[0].dependency_ids) == 2


def test_websocket_auth_is_interned_only_for_protected_handlers() -> None:
    async def open_handler() -> None:
        return None

    @authenticated()
    async def protected_handler() -> None:
        return None

    image = build_metadata_image(
        _app(_ws_routes=(("/open", open_handler), ("/private", protected_handler)))
    )

    assert [(entry.entry_id, entry.name) for entry in image.auth_policies] == [(1, "auth")]
    routes = {route.path: route for route in image.routes}
    assert routes["/open"].auth_policy_id == 0
    assert routes["/private"].auth_policy_id == 1
