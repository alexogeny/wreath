from __future__ import annotations

import dataclasses
from typing import Annotated, Any

import pytest

from wreath.binding import Form, inspect_handler


def _handler_with_a_local_annotation() -> Any:
    """A handler annotated with a name visible only inside this function."""

    class Local:
        pass

    async def handler(request: Any, item: Local) -> Any:
        return {}

    return handler


# The model classes are module-level on purpose: the *handler's* annotation must
# resolve, so that the only thing that cannot is a field inside the model. A
# locally-defined model would trip the handler-level refusal first and this file
# would be testing that path twice.
@dataclasses.dataclass
class BodyWithAnUnresolvableField:
    thing: NeverDefined  # noqa: F821


@dataclasses.dataclass
class FormWithAnUnresolvableField:
    thing: AlsoNeverDefined  # noqa: F821


def _handler_with_a_local_body_model() -> Any:
    """A body dataclass whose *field* annotation cannot be resolved."""

    async def handler(request: Any, body: BodyWithAnUnresolvableField) -> Any:
        return {}

    return handler


def _handler_with_a_local_form_model() -> Any:
    """The same, reached through the form-model path rather than the body."""

    async def handler(request: Any, body: Annotated[FormWithAnUnresolvableField, Form()]) -> Any:
        return {}

    return handler


def test_a_handler_annotation_that_cannot_resolve_is_refused_by_name() -> None:
    with pytest.raises(TypeError) as caught:
        inspect_handler(_handler_with_a_local_annotation(), "/x")

    message = str(caught.value)
    assert "handler" in message
    assert "'item'" in message, "the parameter is not named"
    assert "Local" in message, "the unresolvable name is not named"
    assert "module" in message, "the message does not say why it could not resolve"


def test_a_body_model_field_is_refused_at_compile_time_not_on_the_first_request() -> None:
    with pytest.raises(TypeError) as caught:
        inspect_handler(_handler_with_a_local_body_model(), "/x")

    message = str(caught.value)
    assert "body model" in message
    assert "'thing'" in message
    assert "NeverDefined" in message


def test_a_form_model_field_is_refused_the_same_way() -> None:
    with pytest.raises(TypeError) as caught:
        inspect_handler(_handler_with_a_local_form_model(), "/x")

    assert "form model" in str(caught.value)
    assert "NeverDefined" in str(caught.value)


# Each of these passed before the change too. They are here because the failure
# mode of a refusal is refusing too much, and none of the tests above would
# notice.


def test_a_resolvable_handler_still_compiles() -> None:
    async def handler(request: Any, limit: int = 5) -> Any:
        return {}

    spec = inspect_handler(handler, "/x")
    assert spec is not None
    assert [name for name, _alias, _annotation, _default in spec.query_params] == ["limit"]


def test_a_request_only_handler_is_still_none() -> None:
    async def handler(request: Any) -> Any:
        return {}

    assert inspect_handler(handler, "/x") is None


def test_an_uninspectable_object_is_still_none() -> None:

    class NotCallable:
        pass

    assert inspect_handler(NotCallable(), "/x") is None
