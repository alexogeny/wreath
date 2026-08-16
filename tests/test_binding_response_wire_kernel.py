"""Whole-boundary response validation and JSON emission."""

from __future__ import annotations

from typing import Annotated, Any

import pytest

from wreath.binding import Field, ResponseValidationError, compile_response_validator
from wreath.response import _EncodedJSON


def _compiled(annotation: Any, value: Any) -> Any:
    def handler(_request: object) -> Any:
        return value

    return compile_response_validator(handler, annotation)(None)


def test_typed_list_reaches_wire_bytes_without_python_container_rebuilds() -> None:
    result = _compiled(list[int], [1, 2, 3])
    assert result.__class__ is _EncodedJSON
    assert result.body == b"[1,2,3]"


def test_nested_wire_preserving_plan_reaches_the_same_kernel() -> None:
    result = _compiled(dict[str, list[Annotated[int, Field(ge=0)]]], {"n": [1, 2]})
    assert result.__class__ is _EncodedJSON
    assert result.body == b'{"n":[1,2]}'


def test_sequence_projection_remains_the_fallback_for_tuple_input() -> None:
    result = _compiled(list[int], (1, 2, 3))
    assert result == [1, 2, 3]
    assert result.__class__ is list


def test_transforming_float_plan_keeps_the_canonical_conversion_path() -> None:
    result = _compiled(list[float], [1, 2.5])
    assert result == [1.0, 2.5]
    assert result.__class__ is list


def test_scalar_string_keeps_text_response_coercion() -> None:
    result = _compiled(str, "ok")
    assert result == "ok"
    assert result.__class__ is str


def test_native_list_refusal_keeps_the_response_error_contract() -> None:
    with pytest.raises(ResponseValidationError) as caught:
        _compiled(list[int], [1, "two", 3])
    assert caught.value.errors == [
        {"loc": ["response", 1], "msg": "value is not an integer", "type": "int"}
    ]
