"""The schema an elicitation asks for, from the layer that already had one.

`elicitation/create` hands the client a JSON Schema and gets back whatever the
person typed. That is a schema and a validator, and this codebase has exactly
one of each: `derive_input_schema` reads a signature the way it reads a tool's,
and `bind_arguments` checks the answer the way it checks a `tools/call`'s
arguments. Writing a second pair here would have been half a day's work and a
permanent source of drift between what a client is asked for and what the server
will actually accept.

The adaptation is one step wide. A tool's structured argument arrives *nested*,
under the parameter that carries `Annotated[T, Body()]`; MCP's elicitation
schema is flat and primitive-only, because a client renders it as a form. So the
dataclass's fields are read through `binding._dataclass_spec` -- the same
resolution the body validator uses, so the annotations are the same objects --
and laid out as the parameters of one synthetic signature, which is then handed
to the ordinary derivation. What comes back is a flat object schema whose
properties are exactly the dataclass's fields, and a `BindingSpec` that
validates them.

A field MCP cannot carry is refused **when the form is first used**, with the
field named. The specification allows strings, numbers, booleans and enums of
those; a nested object would render as a form field nobody can fill in.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

from ..binding import BindingSpec, _dataclass_spec
from .schema import derive_input_schema

#: Derived once per form class. Keyed on the class, so a form declared at module
#: scope costs one derivation for the process and a form declared inside a
#: function costs one per declaration -- which is the same trade a tool makes.
_FORMS: dict[type, tuple[dict[str, Any], BindingSpec | None]] = {}

#: What a rendered property may be. MCP's elicitation chapter restricts the
#: requested schema to primitives so that a client can render it as a form
#: without implementing a schema compiler; anything else is refused here rather
#: than sent to a client that will decline it or, worse, guess.
_PRIMITIVES = frozenset(("string", "number", "integer", "boolean", "null"))


def form_schema(form: type) -> tuple[dict[str, Any], BindingSpec | None]:
    """`(requestedSchema, spec)` for one elicitation form.

    Raises:
        TypeError: `form` is not a dataclass, or declares a field MCP's
            elicitation schema cannot carry.
    """
    cached = _FORMS.get(form)
    if cached is not None:
        return cached
    if not (isinstance(form, type) and dataclasses.is_dataclass(form)):
        raise TypeError(
            f"an elicitation form must be a dataclass; got {form!r}. The fields "
            "are what the client renders as a form and what the answer is "
            "validated against, so there has to be a declaration to read."
        )
    parameters = [
        inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    annotations: dict[str, Any] = {}
    for name, annotation, required in _dataclass_spec(form):
        annotations[name] = annotation
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                # A field with a `default_factory` has no value to put here, so
                # it gets None: the derivation only writes a `default` into the
                # schema for a non-None one, and an argument the client omits is
                # left out of the kwargs entirely, which is what lets the
                # dataclass apply its own factory.
                default=(
                    inspect.Parameter.empty
                    if required
                    else getattr(form, name, None)
                ),
                annotation=annotation,
            )
        )
    handler = _synthetic(form, parameters, annotations)
    schema, spec = derive_input_schema(handler, form.__name__)
    _check_primitive(form, schema)
    derived = (schema, spec)
    _FORMS[form] = derived
    return derived


def _synthetic(
    form: type, parameters: list[inspect.Parameter], annotations: dict[str, Any]
) -> Any:
    """A callable whose signature *is* the form, for the derivation to read.

    Nothing calls it. It exists because the derivation's input is a signature
    and the declaration's is a dataclass, and one adapter is cheaper than a
    second schema renderer. The annotations are the already-resolved objects
    `binding` computed, so no name has to be looked up again in any module.
    """

    def declaration(request: Any, **fields: Any) -> None:  # pragma: no cover - never run
        raise RuntimeError("an elicitation form declaration is never invoked")

    declaration.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute]
        parameters
    )
    declaration.__annotations__ = annotations
    declaration.__name__ = form.__name__
    declaration.__qualname__ = getattr(form, "__qualname__", form.__name__)
    declaration.__module__ = getattr(form, "__module__", __name__)
    return declaration


def _check_primitive(form: type, schema: dict[str, Any]) -> None:
    for name, rendered in (schema.get("properties") or {}).items():
        if not _is_primitive(rendered):
            raise TypeError(
                f"elicitation form {form.__name__!r} declares {name!r} as "
                "something MCP cannot ask for. The specification restricts an "
                "elicitation's schema to strings, numbers, booleans and enums "
                "of those, because a client renders it as a form for a person "
                "to fill in. Flatten it, or ask for it as a tool argument "
                "instead -- a tool's schema has no such restriction."
            )


def _is_primitive(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    if isinstance(schema.get("enum"), list):
        return all(
            value is None or isinstance(value, (str, int, float, bool))
            for value in schema["enum"]
        )
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind in _PRIMITIVES
    if isinstance(kind, list):
        return all(entry in _PRIMITIVES for entry in kind)
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            return all(_is_primitive(branch) for branch in branches)
    return False


__all__ = ["form_schema"]
