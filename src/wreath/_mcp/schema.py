"""Derive a tool's `inputSchema` from the handler signature, once.

This is the whole reason `wreath.mcp` is worth having in-tree. A tool's schema
is the same question OpenAPI answers for a route — *what does this callable
accept?* — and Wreath already answers it in one place: `binding.inspect_handler`
reads the signature, `typegen.inspect._Builder` canonicalises each annotation,
and `openapi._openapi_schema` renders it. Calling those three is not a shortcut;
it is the contract. `tests/test_mcp_schema.py` asserts that a tool's schema and
the same handler's OpenAPI schema agree, so the two cannot drift.

The only difference between the two renderings is where a dataclass lives. An
OpenAPI document puts it in `components/schemas`; a JSON Schema document that
has to travel alone puts it in `$defs`, so the renderer emits that reference
prefix directly.

Derivation happens when the tool is registered, never per call.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from ..binding import BindingSpec, inspect_handler
from ..openapi import _component_schema, _openapi_schema
from ..typegen.inspect import _Builder

_OPENAPI_REF_BASE = "#/components/schemas/"
_JSON_SCHEMA_REF_BASE = "#/$defs/"

#: Binding sources a tool cannot have, and what to say about each. An MCP call
#: carries one JSON object of arguments and nothing else -- no headers, no
#: cookies, no multipart body -- so a parameter bound from one of those is a
#: declaration that can never be satisfied. Refusing at registration is the
#: point: the alternative is a tool that lists an argument the caller has no way
#: to supply, discovered by a model at runtime.
_UNSUPPORTED_SOURCES = (
    ("header_params", "a header"),
    ("cookie_params", "a cookie"),
    ("form_params", "a form field"),
    ("file_params", "an uploaded file"),
)


class ToolSignatureError(TypeError):
    """A handler's signature cannot be expressed as an MCP tool."""


def _rebase_refs(node: Any) -> Any:
    """Point every `$ref` at `$defs` instead of an OpenAPI component section."""
    if isinstance(node, dict):
        return {
            key: (
                value.replace(_OPENAPI_REF_BASE, _JSON_SCHEMA_REF_BASE, 1)
                if key == "$ref" and isinstance(value, str)
                else _rebase_refs(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_rebase_refs(item) for item in node]
    return node


def _refuse(name: str, reason: str, route: str | None) -> ToolSignatureError:
    """The one refusal message, written for whoever has to act on it.

    A hand-declared tool is refused where it is declared, and the tool's name is
    enough to find it. A route-derived one is not: `expose_routes` names a tag or
    a predicate, and the offending handler may be three files away, so the route
    is named too and the remedy is different -- exclude the route, or declare a
    tool that calls into the same code with an argument object it *can* be given.
    """
    subject = f"tool {name!r}" if route is None else f"route {route} as tool {name!r}"
    remedy = (
        ""
        if route is None
        else " Narrow the selector so this route is not chosen, or declare a "
        "tool of your own that takes those values as arguments and calls the "
        "same code."
    )
    return ToolSignatureError(
        f"cannot expose {subject}: {reason}. An MCP `tools/call` carries a "
        "single JSON object of arguments, so every bound parameter must come "
        "from it -- a plain annotated parameter, or one dataclass or ORM model "
        f"marked `Annotated[T, Body()]`.{remedy}"
    )


def _check_supported(name: str, spec: BindingSpec, route: str | None) -> None:
    for attribute, description in _UNSUPPORTED_SOURCES:
        bindings = getattr(spec, attribute)
        if bindings:
            parameter = bindings[0][0]
            raise _refuse(name, f"parameter {parameter!r} binds from {description}", route)
    if spec.form_model is not None:
        raise _refuse(name, f"parameter {spec.form_model[0]!r} binds a whole multipart form", route)
    if spec.path_params:
        raise _refuse(
            name,
            f"parameter {spec.path_params[0][0]!r} binds from a path placeholder",
            route,
        )
    for attribute, description in (
        ("depends", "a `Depends(...)` dependency"),
        ("connections", "a database connection"),
        ("sessions", "an ORM session"),
    ):
        bindings = getattr(spec, attribute)
        if bindings:
            raise _refuse(
                name,
                f"parameter {bindings[0][0]!r} injects {description}, which "
                "the MCP surface does not resolve",
                route,
            )


def derive_input_schema(
    handler: Callable[..., Any], name: str, *, route: str | None = None
) -> tuple[dict[str, Any], BindingSpec | None]:
    """Return `(input_schema, binding_spec)` for one tool handler.

    The schema is a JSON Schema object with one property per bound parameter,
    `additionalProperties: false`, and any dataclass or ORM model the signature
    mentions inlined under `$defs`. The spec comes back too because dispatch
    needs it to validate arguments, and reading a signature twice is exactly the
    request-time introspection this codebase compiles away.

    Raises:
        ToolSignatureError: The signature binds from a source an MCP call has no
            way to fill.
    """
    # The route's own path, when there is one, so that a placeholder is
    # classified as a path parameter and refused *by name* rather than quietly
    # becoming an ordinary argument. `""` for a declared tool, which has no path.
    spec = inspect_handler(handler, route or "")
    if spec is None:
        # A request-only handler takes no arguments at all. That is a legitimate
        # tool -- `ping`-shaped tools exist -- and its schema is an empty object.
        return {"type": "object", "properties": {}, "additionalProperties": False}, None

    _check_supported(name, spec, route)

    # One builder for the whole tool, so a dataclass mentioned by two parameters
    # is registered once and both `$ref`s resolve to the same definition.
    builder = _Builder(allow_unknown=True)

    def render(annotation: Any) -> dict[str, Any]:
        return _openapi_schema(builder.type_ref(annotation), _JSON_SCHEMA_REF_BASE)

    properties: dict[str, Any] = {}
    required: list[str] = []
    for _parameter, wire_name, annotation, default in spec.query_params:
        rendered = render(annotation)
        if default is inspect.Parameter.empty:
            required.append(wire_name)
        elif default is not None:
            rendered["default"] = (
                _rebase_refs(default) if isinstance(default, (dict, list)) else default
            )
        properties[wire_name] = rendered
    if spec.body is not None:
        parameter, annotation = spec.body
        properties[parameter] = render(annotation)
        required.append(parameter)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    definitions = {
        model.name: _component_schema(model, _JSON_SCHEMA_REF_BASE, _rebase_refs)
        for model in builder.registry.models()
    }
    if definitions:
        schema["$defs"] = definitions
    return schema, spec
