"""Pure/native parity for the body validator.

The native `run_validation` executes a plan compiled by `wreath.binding`; it must
produce byte-identical results and error lists to the pure `validate`, which
stays the reference. Recursive dataclasses fall back to pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import pytest

from wreath._native import _core
from wreath.binding import (
    ValidationError,
    _body_validator,
    _compile_plan,
    _PlanUnsupported,
    validate,
)

native_only = pytest.mark.skipif(_core is None, reason="native core not built")


@dataclass
class Address:
    street: str
    city: str
    zip: str | None = None


@dataclass
class Product:
    name: str
    price: float
    quantity: int
    active: bool
    kind: Literal["basic", "premium"]
    tags: list[str] = field(default_factory=list)
    address: Address | None = None
    meta: dict[str, int] = field(default_factory=dict)
    pair: tuple[int, int] | None = None


@dataclass
class Node:
    value: int
    children: list[Node] = field(default_factory=list)


def _pure(annotation: Any, value: Any, loc: tuple[Any, ...] = ("body",)) -> tuple[str, Any]:
    try:
        return ("ok", repr(validate(annotation, value, loc)))
    except ValidationError as error:
        return ("err", error.errors)


def _native(annotation: Any, value: Any, loc: tuple[Any, ...] = ("body",)) -> tuple[str, Any]:
    try:
        plan = _compile_plan(annotation, frozenset())
    except _PlanUnsupported:
        validator = _body_validator(annotation)
        try:
            return ("ok", repr(validator(value, loc)))
        except ValidationError as error:
            return ("err", error.errors)
    result, errors = _core.run_validation(plan, value, loc)
    if errors:
        return ("err", errors)
    return ("ok", repr(result))


CASES: list[tuple[Any, Any]] = [
    (
        Product,
        {
            "name": "a",
            "price": 1.5,
            "quantity": 2,
            "active": True,
            "kind": "basic",
            "tags": ["x", "y"],
            "meta": {"k": 3},
            "pair": [1, 2],
        },
    ),
    (
        Product,
        {
            "name": "a",
            "price": 2,
            "quantity": 1,
            "active": False,
            "kind": "premium",
            "address": {"street": "s", "city": "c", "zip": "12"},
        },
    ),
    (Product, {"name": "a", "price": "nope", "quantity": 1, "active": True, "kind": "basic"}),
    (Product, {"price": 1.0}),
    (
        Product,
        {
            "name": "a",
            "price": 1.0,
            "quantity": 1,
            "active": True,
            "kind": "basic",
            "extra": 9,
            "more": 1,
            "another": 2,
        },
    ),
    (
        Product,
        {"name": "a", "price": 1.0, "quantity": 1, "active": True, "kind": "basic", "tags": [1, 2]},
    ),
    (
        Product,
        {
            "name": "a",
            "price": 1.0,
            "quantity": 1,
            "active": True,
            "kind": "basic",
            "address": {"zip": None},
        },
    ),
    (Product, "not-an-object"),
    (list[int], [1, 2, 3]),
    (list[int], [1, "x", 3]),
    (dict[str, int], {"a": 1, "b": "x"}),
    (dict[str, Any], {"id": 7, "nested": [1, "two"]}),
    (int, True),
    (bool, 1),
    (float, 5),
    (float, 5.0),
    (str | None, None),
    (str | int, "hello"),
    (str | int, 5),
    (str | int, 1.5),
]


@native_only
@pytest.mark.parametrize("annotation,value", CASES)
def test_pure_native_parity(annotation: Any, value: Any) -> None:
    assert _pure(annotation, value) == _native(annotation, value)


@native_only
def test_native_any_mapping_validation_does_not_copy_an_unchanged_value() -> None:
    payload = {"id": 7, "nested": [1, "two"]}
    plan = _compile_plan(dict[str, Any], frozenset())

    result, errors = _core.run_validation(plan, payload, ("response",))

    assert errors == []
    assert result is payload


@native_only
def test_native_any_mapping_contract_encodes_in_the_validation_entry() -> None:
    payload = {"id": 7, "nested": [1, "two"]}
    plan = _compile_plan(dict[str, Any], frozenset())

    body, errors = _core.run_validation_json(plan, payload, ("response",))

    assert errors == []
    assert body == b'{"id":7,"nested":[1,"two"]}'

    body, errors = _core.run_validation_json(plan, ["not", "an", "object"], ("response",))
    assert body is None
    assert errors == [{"loc": ["response"], "msg": "value is not an object", "type": "dict"}]


def test_recursive_dataclass_falls_back_to_pure() -> None:
    # A self-referential model cannot be flattened into a plan.
    with pytest.raises(_PlanUnsupported):
        _compile_plan(Node, frozenset())


@native_only
def test_recursive_body_still_validates_via_pure() -> None:
    from wreath.binding import _body_validator

    validator = _body_validator(Node)
    result = validator({"value": 1, "children": [{"value": 2}]}, ("body",))
    assert result == Node(value=1, children=[Node(value=2)])


def test_body_validator_selects_pure_when_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the native core absent (WREATH_PURE), the pure validator is used.
    import wreath.binding as binding

    monkeypatch.setattr(binding, "_core", None)
    validator = binding._body_validator(Address)
    result = validator({"street": "s", "city": "c"}, ("body",))
    assert isinstance(result, Address)
