from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any

import pytest

from wreath._native import _core
from wreath.binding import (
    Field,
    ValidationError,
    _body_validator,
    _compile_plan,
    _PlanUnsupported,
    validate,
)


@dataclass
class Node:
    value: int
    children: list[Node] = field(default_factory=list)


@dataclass
class ConstrainedLine:
    sku: Annotated[
        str,
        Field(alias="stockCode", min_length=3, max_length=12, pattern=r"^[a-z0-9-]+$"),
    ]
    quantity: Annotated[int, Field(gt=0, ge=1, lt=101, le=100)]


@dataclass
class ConstrainedPayload:
    lines: Annotated[list[ConstrainedLine], Field(min_length=1, max_length=3)]


@dataclass(kw_only=True)
class KeywordPayload:
    value: int
    label: str = "default"


def test_any_mapping_validation_preserves_an_unchanged_value() -> None:
    payload = {"id": 7, "nested": [1, "two"]}
    plan = _compile_plan(dict[str, Any], frozenset())

    result, errors = _core.run_validation(plan, payload, ("response",))

    assert errors == []
    assert result is payload


def test_any_mapping_contract_encodes_in_the_validation_entry() -> None:
    payload = {"id": 7, "nested": [1, "two"]}
    plan = _compile_plan(dict[str, Any], frozenset())

    body, errors = _core.run_validation_json(plan, payload, ("response",))

    assert errors == []
    assert body == b'{"id":7,"nested":[1,"two"]}'

    body, errors = _core.run_validation_json(plan, ["not", "an", "object"], ("response",))
    assert body is None
    assert errors == [{"loc": ["response"], "msg": "value is not an object", "type": "dict"}]


def test_recursive_dataclass_is_evaluated_without_a_flat_plan() -> None:
    with pytest.raises(_PlanUnsupported):
        _compile_plan(Node, frozenset())

    validator = _body_validator(Node)
    result = validator({"value": 1, "children": [{"value": 2}]}, ("body",))

    assert result == Node(value=1, children=[Node(value=2)])


def test_keyword_only_dataclass_keeps_its_defaulted_constructor_path() -> None:
    plan = _compile_plan(KeywordPayload, frozenset())

    result, errors = _core.run_validation(plan, {"value": 7}, ("body",))

    assert errors == []
    assert result == KeywordPayload(value=7)


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (Annotated[int, Field(gt=0, ge=1, lt=11, le=10)], 5),
        (Annotated[int, Field(gt=0)], 0),
        (Annotated[Any, Field(gt=0)], "not-comparable"),
        (Annotated[list[int], Field(min_length=1, max_length=2)], []),
        (Annotated[Any, Field(min_length=1)], 7),
        (Annotated[str, Field(pattern=r"^[a-z]+$")], "BAD"),
        (Annotated[Any, Field(pattern=r"^[a-z]+$")], 7),
        (
            ConstrainedPayload,
            {"lines": [{"stockCode": "alpha-1", "quantity": 2}]},
        ),
        (
            ConstrainedPayload,
            {"lines": [{"stockCode": "X", "quantity": 101}]},
        ),
    ],
)
def test_field_plan_matches_the_reference_validator(annotation: Any, value: Any) -> None:
    plan = _compile_plan(annotation, frozenset())
    actual, actual_errors = _core.run_validation(plan, value, ("body",))
    try:
        expected = validate(annotation, value, ("body",))
    except ValidationError as error:
        assert actual_errors == error.errors
    else:
        assert actual_errors == []
        assert actual == expected
        assert type(actual) is type(expected)
