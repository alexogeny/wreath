from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

from wreath.binding import (
    Form,
    ValidationError,
    _body_validator,
    _form_model_fields,
    _FormModelValidationTape,
    _unwrap_form_type,
    inspect_handler,
)
from wreath.orm import Mapped
from wreath.request import FormData


@dataclasses.dataclass
class Booking:
    llama: str
    days: int
    note: str = "none"


class _StubRequest:
    def __init__(self, fields: dict[str, str]) -> None:
        self._form = FormData(dict(fields), {})

    async def form(self) -> FormData:
        return self._form


def test_form_marked_model_becomes_form_model_spec() -> None:
    async def handler(request, data: Annotated[Booking, Form()]): ...

    spec = inspect_handler(handler, "/book")
    assert spec is not None
    assert spec.form_model == ("data", Booking)
    assert spec.body is None
    assert spec.form_params == ()  # not treated as a scalar field


def test_scalar_form_field_still_scalar() -> None:
    async def handler(request, name: Annotated[str, Form()]): ...

    spec = inspect_handler(handler, "/x")
    assert spec is not None
    assert spec.form_model is None
    assert spec.form_params and spec.form_params[0][0] == "name"


def test_form_model_field_specs() -> None:
    by_name = {name: (ptype, required) for name, ptype, required in _form_model_fields(Booking)}
    assert by_name["llama"] == (str, True)
    assert by_name["days"] == (int, True)
    assert by_name["note"][1] is False  # has a default → optional


async def test_form_model_decode_coerces_and_validates() -> None:
    tape = _FormModelValidationTape("data", _form_model_fields(Booking), _body_validator(Booking))
    kwargs: dict[str, object] = {}
    await tape.decode(_StubRequest({"llama": "Kuzco", "days": "3"}), kwargs)
    assert kwargs["data"] == Booking(llama="Kuzco", days=3, note="none")


async def test_form_model_rejects_extra_field() -> None:
    tape = _FormModelValidationTape("data", _form_model_fields(Booking), _body_validator(Booking))
    with pytest.raises(ValidationError):
        await tape.decode(_StubRequest({"llama": "K", "days": "1", "bogus": "x"}), {})


async def test_form_model_rejects_bad_scalar() -> None:
    tape = _FormModelValidationTape("data", _form_model_fields(Booking), _body_validator(Booking))
    with pytest.raises(ValidationError):
        await tape.decode(_StubRequest({"llama": "K", "days": "not-an-int"}), {})


def test_body_and_form_model_conflict() -> None:
    async def handler(request, a: Annotated[Booking, Form()], b: Booking): ...

    with pytest.raises(TypeError):
        inspect_handler(handler, "/x")


# `_unwrap_form_type` peels `Mapped[T]` and `Optional[T]` down to something
# `_convert_scalar` understands. A mutation sweep found every branch of it either
# `unreached` or undistinguished: no form model in any test declared an optional
# field, a `Mapped` field, or a union with two real options. A form field is a
# string on the wire, so failing to peel means the field is refused as
# `unsupported parameter annotation` -- the developer's model looks wrong when it
# is not.


@dataclasses.dataclass
class Peeled:
    """One field per shape `_unwrap_form_type` claims to handle."""

    plain: str
    maybe: int | None
    mapped: Mapped[int]
    both: Mapped[float] | None
    either: int | str


def test_every_wrapper_a_form_field_may_wear_is_peeled_to_its_scalar() -> None:
    assert _form_model_fields(Peeled) == (
        ("plain", str, True),
        ("maybe", int, True),
        ("mapped", int, True),
        ("both", float, True),
        ("either", int | str, True),
    )


def test_a_name_that_is_not_mapped_is_left_alone() -> None:
    assert _unwrap_form_type(list[int]) == list[int]
    assert _unwrap_form_type(str) is str
    assert _unwrap_form_type(Mapped) is Mapped  # no args: the `and args` clause
