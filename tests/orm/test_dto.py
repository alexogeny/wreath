from __future__ import annotations

import dataclasses
from typing import Any, get_type_hints

import pytest

from wreath.orm import Mapped, Model, column, model_dataclass
from wreath.orm.types import Int64, Jsonb, Text, TsVector


class Llama(Model, table="dto_llamas"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    nickname: Mapped[str | None] = column(Text, nullable=True)
    labels: Mapped[dict] = column(Jsonb, default=dict)


def test_model_dataclass_selects_columns_in_model_order() -> None:
    payload = model_dataclass(
        Llama,
        include={"labels", "name"},
        name="LlamaCreate",
    )

    assert dataclasses.is_dataclass(payload)
    assert [item.name for item in dataclasses.fields(payload)] == ["name", "labels"]
    assert payload(name="Ada").labels == {}
    assert payload(name="Bea").labels is not payload(name="Ada").labels


def test_model_dataclass_preserves_nullable_and_generated_defaults() -> None:
    payload = model_dataclass(Llama, exclude={"labels"}, name="LlamaRead")

    value = payload(name="Ada")

    assert value.id is None
    assert value.nickname is None
    hints = get_type_hints(payload)
    assert hints["id"] == int | None
    assert hints["nickname"] == str | None


def test_model_dataclass_preserves_each_omission_and_annotation_contract() -> None:
    class FreshProjectionKinds(Model, table="fresh_dto_projection_kinds"):
        id: Mapped[int] = column(Int64, primary_key=True)
        required: Mapped[str] = column(Text)
        nullable_declared_str: Mapped[str] = column(Text, nullable=True)
        already_optional: Mapped[str | None] = column(Text, nullable=True)
        anything: Mapped[Any] = column(Jsonb, nullable=True)
        nothing: Mapped[None] = column(Text, nullable=True)
        constant_default: Mapped[str] = column(Text, default="fixed")
        database_default: Mapped[str] = column(Text, server_default="'db'")
        search: Mapped[str] = column(TsVector(sources=("required",)))

    payload = model_dataclass(FreshProjectionKinds, name="FreshProjectionKindsData")
    with pytest.raises(TypeError, match="required"):
        payload()

    value = payload(required="Ada")
    assert value.id is None
    assert value.nullable_declared_str is None
    assert value.already_optional is None
    assert value.anything is None
    assert value.nothing is None
    assert value.constant_default == "fixed"
    assert value.database_default is None
    assert value.search is None
    hints = get_type_hints(payload)
    assert hints["required"] is str
    assert hints["nullable_declared_str"] == str | None
    assert hints["already_optional"] == str | None
    assert hints["anything"] is Any
    assert hints["nothing"] is type(None)
    assert hints["database_default"] == str | None
    assert hints["search"] == str | None


def test_model_dataclass_default_shape_and_name_are_explicit() -> None:
    class FreshLlama(Model, table="fresh_dto_llamas"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        nickname: Mapped[str | None] = column(Text, nullable=True)
        labels: Mapped[dict] = column(Jsonb, default=dict)

    payload = model_dataclass(FreshLlama)
    assert payload.__name__ == "FreshLlamaData"
    assert [item.name for item in dataclasses.fields(payload)] == [
        "id",
        "name",
        "nickname",
        "labels",
    ]


def test_model_dataclass_preserves_plain_column_annotations() -> None:
    class PlainAnnotations(Model, table="plain_annotation_dto"):
        id: int = column(Int64, primary_key=True)
        name: str = column(Text)

    payload = model_dataclass(PlainAnnotations)
    assert get_type_hints(payload) == {"id": int | None, "name": str}


def test_model_dataclass_is_keyword_only_and_cached_on_the_model() -> None:
    first = model_dataclass(Llama, include={"name"}, name="LlamaName")
    second = model_dataclass(Llama, include={"name"}, name="LlamaName")

    assert first is second
    with pytest.raises(TypeError):
        first("Ada")


def test_model_dataclass_can_be_extended_by_an_explicit_dataclass() -> None:
    base = model_dataclass(Llama, include={"name"}, name="LlamaPatchFields")

    @dataclasses.dataclass(kw_only=True)
    class LlamaPatch(base):
        reason: str

    value = LlamaPatch(name="Ada", reason="shearing")

    assert dataclasses.asdict(value) == {"name": "Ada", "reason": "shearing"}


@pytest.mark.parametrize(
    ("kwargs", "error", "match"),
    [
        ({"include": {"missing"}}, ValueError, "no column"),
        ({"exclude": {"id", "name", "nickname", "labels"}}, ValueError, "at least one"),
        ({"include": {"name"}, "exclude": {"id"}}, ValueError, "mutually exclusive"),
        ({"include": {1}}, TypeError, "column names"),
        ({"exclude": None}, TypeError, "not None"),
        ({"name": "not valid"}, ValueError, "Python identifier"),
    ],
)
def test_model_dataclass_refuses_ambiguous_declarations(kwargs, error, match) -> None:
    with pytest.raises(error, match=match):
        model_dataclass(Llama, **kwargs)


def test_model_dataclass_requires_a_concrete_wreath_model() -> None:
    with pytest.raises(TypeError, match="mapped wreath.orm.Model"):
        model_dataclass(123)
    with pytest.raises(TypeError, match="mapped wreath.orm.Model"):
        model_dataclass(object)

    with pytest.raises(TypeError, match="concrete mapped model"):
        model_dataclass(Model)

    with pytest.raises(ValueError, match="Python identifier"):
        model_dataclass(Llama, name=123)
