"""OpenAPI 3.1 document generation from typed route signatures.

The same signature inspection that drives request binding (``wreath.binding``)
produces the schema, so the docs can never drift from the validation::

    app = Wreath()
    app.enable_docs()          # /openapi.json and /docs

Dataclass bodies become component schemas; typed path/query parameters become
parameter objects; a dataclass or scalar return annotation becomes the 200
response schema. Handlers without typed signatures still appear with their
paths and methods.
"""

from __future__ import annotations

import inspect
from typing import Any

from .binding import inspect_handler
from .typegen.inspect import _Builder, _return_annotation, resolve_operation_ids
from .typegen.model import Model, TypeRef

_TYPE_KEYS: dict[str, dict[str, Any]] = {
    "null": {"type": "null"},
    "boolean": {"type": "boolean"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "string": {"type": "string"},
}


def _openapi_schema(ref: TypeRef) -> dict[str, Any]:
    """Render one canonical :class:`TypeRef` as an OpenAPI 3.1 schema.

    OpenAPI and the client generators therefore share one interpretation of
    Python annotations -- the difference is only the output syntax.
    """
    simple = _TYPE_KEYS.get(ref.kind)
    if simple is not None:
        return dict(simple)
    if ref.kind == "unknown":
        return {}
    if ref.kind == "reference":
        return {"$ref": f"#/components/schemas/{ref.name}"}
    if ref.kind == "array":
        return {"type": "array", "items": _openapi_schema(ref.arguments[0])}
    if ref.kind == "tuple":
        return {
            "type": "array",
            "prefixItems": [_openapi_schema(arg) for arg in ref.arguments],
            "minItems": len(ref.arguments),
            "maxItems": len(ref.arguments),
        }
    if ref.kind == "record":
        return {"type": "object", "additionalProperties": _openapi_schema(ref.arguments[0])}
    if ref.kind == "union":
        return {"anyOf": [_openapi_schema(arg) for arg in ref.arguments]}
    if ref.kind == "literal":
        return {"enum": list(ref.literals)}
    return {}


def _component_schema(model: Model) -> dict[str, Any]:
    properties = {field.wire_name: _openapi_schema(field.type) for field in model.fields}
    required = [field.wire_name for field in model.fields if field.required]
    schema: dict[str, Any] = {
        "type": "object",
        "title": model.name,
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def generate_openapi(
    app: Any,
    *,
    title: str = "Wreath",
    version: str = "0.1.0",
) -> dict[str, Any]:
    # OpenAPI tolerates unresolved annotations as an empty schema, so the shared
    # builder runs with allow_unknown behaviour (diagnostics are not raised).
    builder = _Builder(allow_unknown=True)

    def schema(annotation: Any) -> dict[str, Any]:
        return _openapi_schema(builder.type_ref(annotation))

    resolved_ids, _diagnostics = resolve_operation_ids(list(app._routes))
    paths: dict[str, dict[str, Any]] = {}

    for index, definition in enumerate(app._routes):
        operations = paths.setdefault(definition.path, {})
        spec = inspect_handler(definition.endpoint, definition.path)
        returns = _return_annotation(definition.endpoint)
        for method in definition.methods:
            operation: dict[str, Any] = {
                "operationId": resolved_ids[(index, method)],
            }
            if definition.tags:
                operation["tags"] = list(definition.tags)
            if definition.summary:
                operation["summary"] = definition.summary
            doc = inspect.getdoc(definition.endpoint)
            if doc:
                operation["description"] = doc
            parameters: list[dict[str, Any]] = []
            if spec is not None:
                for _parameter_name, alias, annotation in spec.path_params:
                    parameters.append(
                        {
                            "name": alias,
                            "in": "path",
                            "required": True,
                            "schema": schema(annotation),
                        }
                    )
                for location, bindings in (
                    ("query", spec.query_params),
                    ("header", spec.header_params),
                    ("cookie", spec.cookie_params),
                ):
                    for _parameter_name, alias, annotation, default in bindings:
                        parameter = {
                            "name": alias,
                            "in": location,
                            "required": default is inspect.Parameter.empty,
                            "schema": schema(annotation),
                        }
                        if default is not inspect.Parameter.empty and default is not None:
                            parameter["schema"]["default"] = default
                        parameters.append(parameter)
                if spec.body is not None:
                    operation["requestBody"] = {
                        "required": True,
                        "content": {
                            "application/json": {"schema": schema(spec.body[1])}
                        },
                    }
                elif spec.form_params or spec.file_params:
                    properties = {
                        alias: schema(annotation)
                        for _name, alias, annotation, _default in spec.form_params
                    }
                    properties.update(
                        {
                            alias: {"type": "string", "format": "binary"}
                            for _name, alias, _annotation, _default in spec.file_params
                        }
                    )
                    required = [
                        alias
                        for _name, alias, _annotation, default in (
                            spec.form_params + spec.file_params
                        )
                        if default is inspect.Parameter.empty
                    ]
                    form_schema: dict[str, Any] = {"type": "object", "properties": properties}
                    if required:
                        form_schema["required"] = required
                    operation["requestBody"] = {
                        "required": bool(required),
                        "content": {"multipart/form-data": {"schema": form_schema}},
                    }
            else:
                # Untyped handler: still document path placeholders as strings.
                for segment in definition.path.split("/"):
                    if segment.startswith("{") and segment.endswith("}"):
                        parameters.append(
                            {
                                "name": segment[1:-1],
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        )
            if parameters:
                operation["parameters"] = parameters
            response: dict[str, Any] = {"description": "Successful response"}
            response_schema = schema(returns)
            if response_schema:
                response["content"] = {"application/json": {"schema": response_schema}}
            operation["responses"] = {"200": response}
            operations[method.lower()] = operation

    components = {model.name: _component_schema(model) for model in builder.registry.models()}
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    if components:
        document["components"] = {"schemas": components}
    return document


_DOCS_PAGE = """<!DOCTYPE html>
<html>
<head>
  <title>{title} — API docs</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"/>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({{url: "{spec_url}", dom_id: "#swagger-ui"}});
  </script>
</body>
</html>
"""


def docs_page(title: str, spec_url: str) -> str:
    return _DOCS_PAGE.format(title=title, spec_url=spec_url)


__all__ = ["docs_page", "generate_openapi"]
