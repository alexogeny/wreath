from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Annotated, Any

from wreath._model_fields import dataclass_field_image


@dataclass
class Example:
    annotated: Annotated[int, "wire-name"]
    required: str
    defaulted: int = 3
    factory: list[int] = dataclasses.field(default_factory=list)


def test_dataclass_field_image_preserves_declaration_facts() -> None:
    fields = dataclass_field_image(
        Example,
        {"annotated": Annotated[int, "wire-name"]},
    )

    assert [field.python_name for field in fields] == [
        "annotated",
        "required",
        "defaulted",
        "factory",
    ]
    assert fields[0].metadata == ("wire-name",)
    assert fields[1].annotation == "str"
    assert fields[1].required is True
    assert fields[1].default is None
    assert fields[2].required is False
    assert fields[2].default == 3
    assert fields[3].required is False


def test_dataclass_field_image_uses_an_explicit_fallback() -> None:
    fields = dataclass_field_image(Example, {}, fallback=Any)
    assert all(field.annotation is Any for field in fields)
