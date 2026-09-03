from __future__ import annotations

import pytest

from wreath.graphql import GraphQL
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text


class Person(Model, table="rgb_people"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text)
    password_hash: Mapped[str] = column(Text, nullable=True)
    api_key: Mapped[str] = column(Text, nullable=True)


class _FakeDatabase:
    name = "rgb"

    async def acquire(self, workload):  # pragma: no cover - never reached
        raise AssertionError("no database in these tests")

    async def release(self, workload, connection):  # pragma: no cover
        pass


def _registry() -> Registry:
    return Registry(_FakeDatabase(), [Person], validate_schema="off")


class TestGraphQLSensitiveFields:
    """R-74: the schema exposes every column, so `password_hash` is queryable."""

    def _schema(self, **kwargs):
        from wreath._graphql.schema import build_schema

        return build_schema(_registry(), [Person], **kwargs)

    def test_sensitive_columns_are_absent_by_default(self):
        fields = self._schema().types["Person"].fields
        assert "email" in fields
        assert "password_hash" not in fields
        assert "api_key" not in fields

    def test_a_named_column_can_be_exposed(self):
        fields = self._schema(expose=("Person.api_key",)).types["Person"].fields
        assert "api_key" in fields
        assert "password_hash" not in fields

    def test_an_unqualified_name_also_exposes(self):
        fields = self._schema(expose=("api_key",)).types["Person"].fields
        assert "api_key" in fields

    def test_the_sdl_does_not_advertise_a_hidden_column(self):
        assert "password_hash" not in self._schema().sdl()


class TestGraphQLTransport:
    """R-75: the endpoint accepts anything that parses as JSON, so a
    cookie-authenticated app is reachable by a simple cross-origin form POST
    that never triggers a preflight."""

    async def test_a_form_content_type_is_refused(self):
        from wreath import Wreath
        from wreath.graphql import GraphQL
        from wreath.testing import TestClient

        api = GraphQL(_registry(), models=[Person])
        app = Wreath()
        app.include_router(api.router(public=True))

        async with TestClient(app) as client:
            response = await client.post(
                "/graphql",
                content=b'{"query": "{ __typename }"}',
                headers={"content-type": "text/plain"},
            )
        assert response.status == 415

    async def test_json_is_accepted(self):
        from wreath import Wreath
        from wreath.graphql import GraphQL
        from wreath.testing import TestClient

        api = GraphQL(_registry(), models=[Person])
        app = Wreath()
        app.include_router(api.router(public=True))

        async with TestClient(app) as client:
            response = await client.post(
                "/graphql",
                content=b'{"query": "{ nope }"}',
                headers={"content-type": "application/json"},
            )
        assert response.status == 200


class TestCrudListRowAuthorization:
    """R-66: `object_authorizer` guards retrieve/update/delete/create and not
    `list`, so rows protected per-row are readable in bulk from `GET /`."""

    def _router(self, **kwargs):
        from wreath.crud import Access, crud_router

        def open_session(request):  # pragma: no cover - replaced per test
            raise AssertionError("unused")

        kwargs.setdefault("authorize", Access.public())
        return crud_router(Person, open_session, **kwargs)

    async def test_list_applies_the_row_authorizer(self):
        from wreath.crud import Access, crud_router

        rows = [
            _Row(id=1, email="a@example.com"),
            _Row(id=2, email="b@example.com"),
        ]

        class _Session:
            async def fetch(self, query):
                return rows

            async def close(self):
                pass

        def only_row_one(request, op, instance):
            return instance.id == 1

        router = crud_router(
            Person,
            lambda request: _Session(),
            operations=("list",),
            object_authorizer=only_row_one,
            authorize=Access.public(),
        )
        handler = router.routes[0].endpoint
        response = await handler(_ListRequest())
        import json

        payload = json.loads(response.body)
        assert [item["id"] for item in payload["items"]] == [1]


class _Row:
    def __init__(self, id, email):
        self.id = id
        self.email = email
        self.password_hash = "x"
        self.api_key = "y"


class _ListRequest:
    query_string = b""
    path_params: dict[str, str] = {}
    identity = None


def test_graphql_requires_an_authorizer_or_an_explicit_public_mount():
    api = GraphQL(_registry(), models=[Person])

    with pytest.raises(ValueError, match=r"authorizer.*public=True"):
        api.router()

    assert api.router(public=True).routes
