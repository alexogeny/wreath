"""Validation-plan execution contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from wreath._native import _core
from wreath.binding import _body_validator, _compile_plan, _PlanUnsupported


@dataclass
class Node:
    value: int
    children: list[Node] = field(default_factory=list)


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
    assert errors == [
        {"loc": ["response"], "msg": "value is not an object", "type": "dict"}
    ]


def test_recursive_dataclass_is_evaluated_without_a_flat_plan() -> None:
    with pytest.raises(_PlanUnsupported):
        _compile_plan(Node, frozenset())

    validator = _body_validator(Node)
    result = validator({"value": 1, "children": [{"value": 2}]}, ("body",))

    assert result == Node(value=1, children=[Node(value=2)])
