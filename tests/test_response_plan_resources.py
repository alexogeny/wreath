from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import pytest

from wreath import binding
from wreath.binding import Field, ResponseValidationError
from wreath.response import _EncodedJSON


@dataclass
class PlanPoint:
    value: int


@pytest.mark.parametrize(
    "annotation, python_calls, native_calls",
    [
        (list[dict[str, int]], 3, 1),
        (PlanPoint, 2, 1),
        (UUID, 1, 0),
        (list[Annotated[int, Field(ge=0)]], 3, 1),
        (list[int | str], 4, 1),
        (Any, 0, 0),
    ],
)
def test_response_annotation_compiles_each_plan_once(
    monkeypatch: pytest.MonkeyPatch,
    annotation: Any,
    python_calls: int,
    native_calls: int,
) -> None:
    python_plans = []
    native_plans = []
    compile_plan = binding._compile_plan
    compile_native = binding._core.compile_validation_plan

    def counted_python(annotation: Any, seen: frozenset[type]) -> tuple[Any, ...]:
        python_plans.append(annotation)
        return compile_plan(annotation, seen)

    def counted_native(plan: Any) -> Any:
        native_plans.append(plan)
        return compile_native(plan)

    def handler(request: Any) -> Any:
        return request

    monkeypatch.setattr(binding, "_compile_plan", counted_python)
    monkeypatch.setattr(binding._core, "compile_validation_plan", counted_native)

    binding.compile_response_validator(handler, annotation)

    assert len(python_plans) == python_calls
    assert len(native_plans) == native_calls


def test_response_check_and_encoder_share_the_compiled_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = []
    run_validation = binding._core.run_validation
    run_validation_json = binding._core.run_validation_json

    def checked(plan: Any, value: Any, loc: Any) -> Any:
        plans.append(plan)
        return run_validation(plan, value, loc)

    def encoded(plan: Any, value: Any, loc: Any) -> Any:
        plans.append(plan)
        return run_validation_json(plan, value, loc)

    def handler(request: Any) -> Any:
        return request

    monkeypatch.setattr(binding._core, "run_validation", checked)
    monkeypatch.setattr(binding._core, "run_validation_json", encoded)
    wrapper = binding.compile_response_validator(handler, list[dict[str, int]])

    result = wrapper([{"n": 1}])
    assert isinstance(result, _EncodedJSON)
    assert result.body == b'[{"n":1}]'
    assert wrapper(({"n": 2},)) == [{"n": 2}]
    assert len(plans) == 3
    assert all(plan is plans[0] for plan in plans)


def test_response_plan_keeps_the_dataclass_annotation_from_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: Any) -> Any:
        return request

    wrapper = binding.compile_response_validator(handler, PlanPoint)
    monkeypatch.setitem(PlanPoint.__annotations__, "value", str)

    assert wrapper({"value": 3, "private": True}) == {"value": 3}
    with pytest.raises(ResponseValidationError) as caught:
        wrapper({"value": "three"})
    assert caught.value.errors == [
        {"loc": ["response", "value"], "msg": "value is not an integer", "type": "int"}
    ]


@pytest.mark.parametrize(
    "annotation, value, expected",
    [
        (UUID, "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000001"),
        (list[Any], [UUID(int=1)], ["00000000-0000-0000-0000-000000000001"]),
        (list[Annotated[int, Field(ge=0)]], (0, 2), [0, 2]),
        (list[int | str], (1, "two"), [1, "two"]),
    ],
)
def test_response_plan_reuse_preserves_conversion_fallbacks(
    annotation: Any, value: Any, expected: Any
) -> None:
    def handler(request: Any) -> Any:
        return request

    wrapper = binding.compile_response_validator(handler, annotation)

    assert wrapper(value) == expected


def test_response_shared_plan_keeps_constraint_errors_after_sequence_projection() -> None:
    def handler(request: Any) -> Any:
        return request

    wrapper = binding.compile_response_validator(handler, list[Annotated[int, Field(ge=0)]])

    with pytest.raises(ResponseValidationError) as caught:
        wrapper((-1,))

    assert caught.value.errors == [
        {"loc": ["response", 0], "msg": "value must be >= 0", "type": "ge"}
    ]
