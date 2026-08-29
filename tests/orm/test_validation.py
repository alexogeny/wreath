from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from wreath.binding import ValidationError, compile_binder
from wreath.orm import FromORM, Mapped, Model, Session, column
from wreath.orm.model import TRANSIENT
from wreath.orm.types import Bool, Int64, Json, Text, Timestamp, Uuid
from wreath.orm.validation import compile_model_validator
from wreath.testing import TestClient

from .test_binding import build_app


class Widget(Model, table="validate_widgets"):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text)
    quantity: Mapped[int] = column(Int64)
    enabled: Mapped[bool] = column(Bool, default=True)
    note: Mapped[str] = column(Text, nullable=True)
    stamped: Mapped[object] = column(Timestamp, server_default="now()")


@pytest.fixture
def validator() -> Any:
    return compile_model_validator(Widget)


def test_a_body_becomes_a_model_directly(validator: Any) -> None:
    widget = validator({"label": "bolt", "quantity": 5}, ("body",))
    assert isinstance(widget, Widget)
    assert widget.label == "bolt" and widget.quantity == 5
    assert widget._orm_state == TRANSIENT


def test_the_validated_value_is_the_one_stored(validator: Any) -> None:
    # The point of the seam: what survives validation is what lands in the
    # cell, rather than being re-proven on assignment.
    widget = validator({"label": "bolt", "quantity": 5}, ("body",))
    assert widget._orm_is_loaded(1) and widget._orm_is_loaded(2)
    assert not widget._orm_has_changes()


def test_defaults_apply_and_server_defaults_stay_unloaded(validator: Any) -> None:
    widget = validator({"label": "bolt", "quantity": 5}, ("body",))
    assert widget.enabled is True
    # The database fills it; INSERT ... RETURNING reads it back.
    assert not widget._orm_is_loaded(5)


def test_a_generated_primary_key_may_be_omitted(validator: Any) -> None:
    widget = validator({"label": "bolt", "quantity": 5}, ("body",))
    assert not widget._orm_is_loaded(0)


def test_a_missing_required_field_is_reported(validator: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        validator({"label": "bolt"}, ("body",))
    assert caught.value.errors == [
        {"loc": ["body", "quantity"], "msg": "field is required", "type": "missing"}
    ]


def test_a_type_error_is_reported_per_field_with_its_location(validator: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        validator({"label": 5, "quantity": "x"}, ("body",))
    assert [item["loc"] for item in caught.value.errors] == [
        ["body", "label"],
        ["body", "quantity"],
    ]
    assert "expected str" in caught.value.errors[0]["msg"]


def test_every_field_is_reported_not_just_the_first(validator: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        validator({}, ("body",))
    assert {item["loc"][1] for item in caught.value.errors} == {"label", "quantity"}


def test_null_is_rejected_for_a_non_nullable_column(validator: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        validator({"label": None, "quantity": 5}, ("body",))
    assert caught.value.errors[0]["type"] == "null"


def test_null_is_accepted_for_a_nullable_column(validator: Any) -> None:
    widget = validator({"label": "b", "quantity": 5, "note": None}, ("body",))
    assert widget.note is None
    assert widget._orm_is_null(4)


def test_unexpected_fields_are_rejected(validator: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        validator({"label": "b", "quantity": 5, "nope": 1}, ("body",))
    assert caught.value.errors == [
        {"loc": ["body", "nope"], "msg": "unexpected field", "type": "extra"}
    ]


def test_a_non_object_body_is_rejected(validator: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        validator([1, 2], ("body",))
    assert caught.value.errors[0]["type"] == "dict"


def test_out_of_range_integers_are_rejected(validator: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        validator({"label": "b", "quantity": 2**63}, ("body",))
    assert caught.value.errors[0]["loc"] == ["body", "quantity"]


def test_the_column_type_stays_the_only_source_of_the_rules() -> None:
    # Whatever a column accepts on assignment, the body validator accepts, and
    # vice versa: there is one implementation, not two that can drift.
    class Typed(Model, table="validate_typed"):
        id: Mapped[int] = column(Int64, primary_key=True)
        key: Mapped[object] = column(Uuid)
        when: Mapped[object] = column(Timestamp)
        doc: Mapped[object] = column(Json)

    validate_body = compile_model_validator(Typed)
    key = uuid.uuid4()
    when = datetime.datetime(2024, 7, 15, 12)
    row = validate_body({"key": key, "when": when, "doc": {"a": 1}}, ("body",))
    assert row.key == key and row.when == when and row.doc == {"a": 1}

    for payload, field in (
        ({"key": "not-a-uuid", "when": when, "doc": {}}, "key"),
        ({"key": key, "when": "not-a-datetime", "doc": {}}, "when"),
    ):
        with pytest.raises(ValidationError) as caught:
            validate_body(payload, ("body",))
        assert caught.value.errors[0]["loc"] == ["body", field]
        # The same value is refused by direct assignment, for the same reason.
        with pytest.raises((TypeError, ValueError)):
            setattr(Typed._orm_new(), field, payload[field])


def test_an_unmapped_class_cannot_validate_a_body() -> None:
    with pytest.raises(TypeError, match="not a mapped model"):
        compile_model_validator(Model)


@pytest.mark.asyncio
async def test_a_model_body_binds_validated_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    app.orm(database="main", models=[Widget], validate_schema="off")

    @app.post("/widgets")
    async def create(
        request: Any,
        widget: Widget,
        session: Annotated[Session, FromORM("main", workload="write")],
    ) -> Any:
        database.connection.script("INSERT", [[7, None, None]])
        session.add(widget)
        await session.flush()
        return {"id": widget.id, "label": widget.label}

    async with TestClient(app) as client:
        response = await client.post("/widgets", json={"label": "bolt", "quantity": 5})
    assert response.json() == {"id": 7, "label": "bolt"}
    insert = [(sql, args) for sql, args in database.connection.calls if sql.startswith("INSERT")]
    assert insert == [
        (
            'INSERT INTO "public"."validate_widgets" ("label", "quantity", "enabled") '
            'VALUES ($1, $2, $3) RETURNING "id", "note", "stamped"',
            ("bolt", 5, True),
        )
    ]


@pytest.mark.asyncio
async def test_an_invalid_model_body_is_a_422_before_any_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    app.orm(database="main", models=[Widget], validate_schema="off")

    @app.post("/widgets")
    async def create(
        request: Any,
        widget: Widget,
        session: Annotated[Session, FromORM("main", workload="write")],
    ) -> Any:
        raise AssertionError("the handler must not run for an invalid body")

    async with TestClient(app) as client:
        response = await client.post("/widgets", json={"label": 5, "quantity": "x"})
    assert response.status == 422
    assert [item["loc"] for item in response.json()["errors"]] == [
        ["body", "label"],
        ["body", "quantity"],
    ]
    # Nothing reached the database, and no connection was ever leased.
    assert database.connection.calls == []
    assert database.acquired == 0


@dataclass
class Payload:
    """Module scope on purpose.

    This module postpones annotations, so a handler annotated with a class
    declared inside the test function has nothing to resolve against when the
    binder reads its type hints, and the route fails to compile.
    """

    name: str


@pytest.mark.asyncio
async def test_a_dataclass_body_still_binds_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)

    @app.post("/plain")
    async def plain(request: Any, payload: Payload) -> Any:
        return {"name": payload.name}

    async with TestClient(app) as client:
        assert (await client.post("/plain", json={"name": "x"})).json() == {"name": "x"}
        assert (await client.post("/plain", json={"name": 5})).status == 422


@pytest.mark.asyncio
async def test_two_body_parameters_are_still_rejected() -> None:
    async def handler(request: Any, first: Widget, second: Widget) -> Any:
        return {}

    with pytest.raises(TypeError, match="two body parameters"):
        compile_binder(handler, "/x")
