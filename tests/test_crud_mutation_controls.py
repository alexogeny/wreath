from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import pytest

from wreath._auth.requirements import requirement_for
from wreath.authorization import EntityUid
from wreath.crud import Access
from wreath.crud import crud_router as _crud_router
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Text


def crud_router(*args, **kwargs):
    kwargs.setdefault("authorize", Access.public())
    return _crud_router(*args, **kwargs)


class Record(Model, table="crud_mutation_records"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    note: Mapped[str] = column(Text, nullable=True)


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Session:
    def __init__(self, rows: dict[int, Record] | None = None) -> None:
        self.rows = dict(rows or {})
        self.added: list[Record] = []
        self.closed = False

    async def get(self, _model: type, key: object) -> Record | None:
        return self.rows.get(key) if isinstance(key, int) else None

    async def fetch(self, _query: object) -> list[Record]:
        return list(self.rows.values())

    def add(self, instance: Record) -> None:
        object.__setattr__(instance, "id", 100)
        self.rows[instance.id] = instance
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def close(self) -> None:
        self.closed = True


class _Request:
    def __init__(
        self,
        *,
        path_params: dict[str, str] | None = None,
        body: object = None,
    ) -> None:
        self.path_params = path_params or {}
        self.query_string = b""
        self._body = body

    async def json(self) -> object:
        return self._body


def _routes(router: Any) -> dict[tuple[str, str], Any]:
    return {(route.methods[0], route.path): route.endpoint for route in router.routes}


def test_composite_primary_key_names_the_model_columns_and_correct_form() -> None:
    class Membership(Model, table="crud_mutation_memberships"):
        organization_id: Mapped[int] = column(Int64, primary_key=True)
        member_id: Mapped[int] = column(Int64, primary_key=True)

    with pytest.raises(
        ValueError,
        match=(
            r"crud_router\(Membership\) found 2 primary-key columns "
            r"\(organization_id, member_id\); expected exactly one"
        ),
    ):
        crud_router(Membership, lambda _request: _Session())


@pytest.mark.asyncio
async def test_fields_allow_list_also_constrains_writable_input() -> None:
    session = _Session({1: Record(id=1, name="before", note="private")})
    routes = _routes(crud_router(Record, lambda _request: session, fields=("id", "name")))

    response = await routes[("PATCH", "/record/{id}")](
        _Request(path_params={"id": "1"}, body={"name": "after", "note": "leak"})
    )

    assert response.status == 200
    assert session.rows[1].name == "after"
    assert session.rows[1].note == "private"
    assert json.loads(response.body) == {"id": 1, "name": "after"}


def test_prefix_and_explicit_tags_are_preserved_on_every_route() -> None:
    router = crud_router(
        Record,
        lambda _request: _Session(),
        prefix="/inventory",
        tags=("stock", "internal"),
    )

    assert {route.path for route in router.routes} == {
        "/inventory",
        "/inventory/{id}",
    }
    assert {route.tags for route in router.routes} == {("stock", "internal")}


def test_default_tag_is_the_lowercase_model_name() -> None:
    router = crud_router(Record, lambda _request: _Session(), operations=("list",))

    assert [(route.path, route.tags) for route in router.routes] == [("/record", ("record",))]


def test_fields_refuses_an_expose_escape_hatch_at_declaration() -> None:
    with pytest.raises(ValueError, match="pass either `fields`.*or `expose`"):
        crud_router(
            Record,
            lambda _request: _Session(),
            fields=("name",),
            expose=("note",),
        )


def test_fields_refuses_each_unknown_model_column_at_declaration() -> None:
    with pytest.raises(
        ValueError,
        match=r"Record has no column\(s\) missing, typo; `fields` names",
    ):
        crud_router(
            Record,
            lambda _request: _Session(),
            fields=("name", "missing", "typo"),
        )


def test_operation_selection_distinguishes_list_from_retrieve() -> None:
    listed = crud_router(Record, lambda _request: _Session(), operations=("list",))
    retrieved = crud_router(Record, lambda _request: _Session(), operations=("retrieve",))

    assert [(route.methods, route.path) for route in listed.routes] == [(("GET",), "/record")]
    assert [(route.methods, route.path) for route in retrieved.routes] == [
        (("GET",), "/record/{id}")
    ]


@pytest.mark.asyncio
async def test_retrieve_deny_short_circuits_before_opening_a_session() -> None:
    opened = False

    def open_session(_request: object) -> _Session:
        nonlocal opened
        opened = True
        return _Session()

    retrieve = _routes(
        crud_router(
            Record,
            open_session,
            operations=("retrieve",),
            authorize={"retrieve": Access.deny()},
        )
    )[
        ("GET", "/record/{id}")
    ]

    response = await retrieve(_Request(path_params={"id": "1"}))

    assert response.status == 403
    assert opened is False


@pytest.mark.asyncio
async def test_retrieve_distinguishes_missing_and_object_denied_rows() -> None:
    session = _Session({1: Record(id=1, name="present")})
    retrieve = _routes(
        crud_router(
            Record,
            lambda _request: session,
            object_authorizer=lambda _request, _op, _row: False,
        )
    )[("GET", "/record/{id}")]

    missing = await retrieve(_Request(path_params={"id": "2"}))
    denied = await retrieve(_Request(path_params={"id": "1"}))

    assert missing.status == 404
    assert denied.status == 403


@pytest.mark.asyncio
async def test_list_removes_only_rows_refused_by_the_object_authorizer() -> None:
    session = _Session(
        {
            1: Record(id=1, name="visible", note=None),
            2: Record(id=2, name="hidden", note=None),
        }
    )
    listing = _routes(
        crud_router(
            Record,
            lambda _request: session,
            operations=("list",),
            object_authorizer=lambda _request, _op, row: row.id == 1,
        )
    )[("GET", "/record")]

    response = await listing(_Request())

    assert [item["name"] for item in json.loads(response.body)["items"]] == ["visible"]


@pytest.mark.asyncio
async def test_update_deny_short_circuits_before_parsing_json() -> None:
    class ExplodingRequest(_Request):
        async def json(self) -> object:
            raise AssertionError("deny must precede body parsing")

    update = _routes(crud_router(Record, lambda _request: _Session(), authorize=Access.deny()))[
        ("PATCH", "/record/{id}")
    ]

    response = await update(ExplodingRequest(path_params={"id": "1"}))

    assert response.status == 403


@pytest.mark.asyncio
async def test_update_refuses_non_object_json_before_opening_a_session() -> None:
    opened = False

    def open_session(_request: object) -> _Session:
        nonlocal opened
        opened = True
        return _Session()

    update = _routes(crud_router(Record, open_session))[("PATCH", "/record/{id}")]

    response = await update(_Request(path_params={"id": "1"}, body=["not", "object"]))

    assert response.status == 400
    assert json.loads(response.body) == {"error": "request body must be a JSON object"}
    assert opened is False


@pytest.mark.asyncio
async def test_update_missing_row_returns_not_found_and_closes_session() -> None:
    session = _Session()
    update = _routes(crud_router(Record, lambda _request: session))[("PATCH", "/record/{id}")]

    response = await update(_Request(path_params={"id": "9"}, body={"name": "unused"}))

    assert response.status == 404
    assert session.closed is True


def test_authenticated_and_cedar_rules_attach_their_distinct_metadata() -> None:
    routes = _routes(
        crud_router(
            Record,
            lambda _request: _Session(),
            operations=("list", "retrieve"),
            authorize={
                "list": Access.authenticated(),
                "retrieve": Access.cedar(action="record:read", resource='Record::"{id}"'),
            },
        )
    )

    listing = requirement_for(routes[("GET", "/record")])
    retrieve = requirement_for(routes[("GET", "/record/{id}")])
    assert listing.authenticated is True
    assert listing.policies == ()
    assert len(retrieve.policies) == 1
    policy = retrieve.policies[0]
    assert policy.action == "record:read"
    assert callable(policy.resource)
    resource = cast("Callable[[Any], object]", policy.resource)
    assert resource(_Request(path_params={"id": "7"})) == 'Record::"7"'


def test_cedar_callable_resource_is_preserved_without_template_wrapping() -> None:
    def resource(request: _Request) -> str:
        return f'Record::"{request.path_params["id"]}"'

    retrieve = _routes(
        crud_router(
            Record,
            lambda _request: _Session(),
            operations=("retrieve",),
            authorize=Access.cedar(action="record:read", resource=resource),
        )
    )[("GET", "/record/{id}")]

    assert requirement_for(retrieve).policies[0].resource is resource


def test_cedar_static_entity_resource_stays_an_entity() -> None:
    resource = EntityUid("Record", "fixed")
    retrieve = _routes(
        crud_router(
            Record,
            lambda _request: _Session(),
            operations=("retrieve",),
            authorize=Access.cedar(action="record:read", resource=resource),
        )
    )[("GET", "/record/{id}")]

    assert requirement_for(retrieve).policies[0].resource is resource


def test_cedar_static_string_resource_is_not_formatted_per_request() -> None:
    resource = 'Record::"fixed"'
    retrieve = _routes(
        crud_router(
            Record,
            lambda _request: _Session(),
            operations=("retrieve",),
            authorize=Access.cedar(action="record:read", resource=resource),
        )
    )[("GET", "/record/{id}")]

    assert requirement_for(retrieve).policies[0].resource == resource
