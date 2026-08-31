from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import wreath.binding as binding


@dataclasses.dataclass
class Point:
    x: int


@dataclasses.dataclass
class Recursive:
    child: Recursive


def test_projection_identity_compiler_respects_cycles_and_mapping_keys() -> None:
    assert not binding._projection_is_identity(str, frozenset({str}))
    assert not binding._projection_is_identity(Point | str)
    assert binding._projection_is_identity(dict[Point, str])


def test_jsonable_compiler_uses_the_shared_any_converter() -> None:
    assert binding._compile_jsonable(Any) is binding._jsonable_any
    assert binding._compile_jsonable(object) is binding._jsonable_any
    assert binding._compile_jsonable(inspect.Parameter.empty) is binding._jsonable_any


def test_jsonable_compiler_only_compiles_mapping_values_for_typed_dicts() -> None:
    assert binding._compile_jsonable(dict)({"point": Point(1)}) == {"point": Point(1)}
    assert binding._compile_jsonable(dict[str])({"point": Point(1)}) == {"point": Point(1)}
    convert = binding._compile_jsonable(tuple[int, str])
    assert convert.__defaults__[1] is binding._jsonable_any


def test_response_check_selects_native_and_annotation_engines() -> None:
    assert binding._compile_response_check(int).__name__ == "planned_check"
    assert binding._compile_response_check(Recursive).__name__ == "annotation_check"


def test_any_response_annotation_returns_the_original_handler() -> None:
    def handler(request: object) -> object:
        return request

    assert binding.compile_response_validator(handler, Any) is handler


def test_identity_projection_is_not_compiled(monkeypatch: Any) -> None:
    def handler(request: object) -> str:
        return str(request)

    def reject_projection(annotation: Any) -> Any:
        raise AssertionError(annotation)

    monkeypatch.setattr(binding, "_compile_response_input", reject_projection)
    binding.compile_response_validator(handler, str)
