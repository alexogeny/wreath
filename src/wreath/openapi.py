"""OpenAPI 3.1 document generation from typed route signatures.

The same signature inspection that drives request binding (`wreath.binding`)
produces the schema, so the docs can never drift from the validation:

```python
app = Wreath()
app.enable_docs()          # /openapi.json and /docs
```
Dataclass bodies become component schemas; typed path/query parameters become
parameter objects; and supported return annotations become response schemas at
the route's declared status. Field constraints, additional responses, security,
deprecation and schema visibility share the route metadata used at runtime.
An unsupported annotation is refused instead of silently becoming `{}`.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any

from .negotiation import PROTOBUF as _PROTOBUF
from .protobuf import is_message as _is_message
from .typegen.inspect import _Builder
from .typegen.model import Model, TypeRef

#: The protobuf media type wreath emits and documents. Spelled from
#: `negotiation.PROTOBUF` rather than beside it, so the document and the
#: wire cannot name different types.
_PROTOBUF_MEDIA = _PROTOBUF.media_type

_TYPE_KEYS: dict[str, dict[str, Any]] = {
    "null": {"type": "null"},
    "boolean": {"type": "boolean"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "string": {"type": "string"},
}

_PATH_CONVERTER = re.compile(r"\{([^}:]+):path\}")

#: The vendor extension carrying the behaviours a generated client may act on.
#: A foreign generator ignores an unknown `x-` key, which is the right failure
#: mode: it sees a document it fully understands, minus an optimisation.
BEHAVIOUR_EXTENSION = "x-wreath-behaviours"


def _contract_candidates(app: Any, definition: Any, method: str) -> list[Any]:
    """Every first-class policy and custom hook covering this operation.

    This mirrors `Wreath._compile_routes_locked` exactly, because a document
    that disagrees with the tape about *which* routes a middleware covers is
    worse than a silent one -- a generated client would retry a permanent
    failure. Global middleware wraps every request, so it always applies;
    route middleware is filtered by the same `applies_to` predicate
    `compile_middleware` evaluates, against the same `MiddlewareRoute`.
    """
    return list(app._application_image.contract_candidates(definition, method))


def _collect_contracts(app: Any, definition: Any, method: str) -> list[Any]:
    """Ask each covering policy or custom hook for its declared contract."""
    from .middleware.base import BEHAVIOURS

    contracts = []
    for item in _contract_candidates(app, definition, method):
        # `callable(None)` is False, so the absent case needs no clause of its
        # own -- a component with no `describe`, and one carrying a `describe`
        # that is not a method, are both simply not asked.
        describe = getattr(item, "describe", None)
        if not callable(describe):
            continue
        contract = describe()
        if contract is None:
            continue
        if getattr(item, "global_scope", False) and getattr(item, "applies_to", None):
            raise ValueError(
                f"{type(item).__name__} is global middleware and declares both "
                "describe() and applies_to. Global hooks are compiled into a flat "
                "program with no predicate evaluation, so applies_to is never "
                "consulted at runtime and the contract would document a narrower "
                "scope than the tape enforces. Register it with add_middleware() "
                "for route scope, or drop applies_to."
            )
        if contract.methods is not None and method.upper() not in contract.methods:
            continue
        unknown = set(contract.behaviours) - BEHAVIOURS
        if unknown:
            raise ValueError(
                f"{type(item).__name__}.describe() declares unknown behaviour(s) "
                f"{', '.join(sorted(unknown))}; the vocabulary is closed and is "
                f"{', '.join(sorted(BEHAVIOURS))}"
            )
        contracts.append(contract)
    return contracts


def _header_schema(spec: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if spec.const is not None:
        schema["const"] = spec.const
    return schema


@dataclass(frozen=True, slots=True)
class ResponseSpec:
    """One additional OpenAPI response declared on a route."""

    model: Any = Any
    description: str = "Response"
    media_type: str = "application/json"


@dataclass(frozen=True, slots=True)
class CompatibilityChange:
    """One backwards-incompatible change between two OpenAPI documents."""

    kind: str
    location: str
    detail: str


def _openapi_schema(ref: TypeRef) -> dict[str, Any]:
    """Render one canonical `TypeRef` as an OpenAPI 3.1 schema.

    OpenAPI and the client generators therefore share one interpretation of
    Python annotations -- the difference is only the output syntax.
    """
    simple = _TYPE_KEYS.get(ref.kind)
    if simple is not None:
        schema = dict(simple)
        if ref.kind == "string" and ref.name:
            # A string kind carries its OpenAPI `format` in `name` -- the same
            # field a `reference` uses for its model name, which is free here
            # because a string is never a reference. That is what turns an
            # `Instant` into `date-time` for every consumer at once.
            schema["format"] = ref.name
        return schema
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
    if ref.kind == "coordinate":
        # An object with named components, never a two-element array: GeoJSON
        # orders `[lon, lat]` and people say "lat, lon", so a positional pair is
        # ambiguous exactly where it is most expensive. `format` names it so a
        # reader (and a generator) can tell it from any other lat/lon object.
        return {
            "type": "object",
            "format": "coordinate",
            "properties": {
                "lat": {"type": "number", "minimum": -90, "maximum": 90},
                "lon": {"type": "number", "minimum": -180, "maximum": 180},
            },
            "required": ["lat", "lon"],
        }
    if ref.kind == "page":
        # Inlined rather than a `$ref`, because `Page` is generic: one component
        # schema cannot describe `Page[Llama]` and `Page[Herd]` at once, and
        # minting `PageLlama`/`PageHerd` components would put a generated name
        # in the contract that the Python target then has to un-generate to
        # reach `wreath.pagination.Page`.
        element = _openapi_schema(ref.arguments[0]) if ref.arguments else {}
        return {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": element},
                "total": {"type": "integer"},
                "page": {"type": "integer"},
                "size": {"type": "integer"},
            },
            "required": ["items", "total", "page", "size"],
        }
    raise ValueError(
        f"no OpenAPI schema for TypeKind {ref.kind!r}. A kind added to "
        "`wreath.typegen.model.TypeKind` must be rendered here and in "
        "`wreath.typegen.typescript_renderer.ts_type`; returning a default instead would emit a "
        "silently wrong document and a silently wrong client, and report success."
    )


def _component_schema(model: Model) -> dict[str, Any]:
    properties: dict[str, dict[str, Any]] = {}
    for field in model.fields:
        field_schema = _openapi_schema(field.type)
        if field.description is not None:
            field_schema["description"] = field.description
        if field.examples:
            field_schema["examples"] = list(field.examples)
        for value, key in (
            (field.gt, "exclusiveMinimum"),
            (field.ge, "minimum"),
            (field.lt, "exclusiveMaximum"),
            (field.le, "maximum"),
        ):
            if value is not None:
                field_schema[key] = value
        length_prefix = "Items" if field.type.kind in ("array", "tuple") else "Length"
        if field.min_length is not None:
            field_schema[f"min{length_prefix}"] = field.min_length
        if field.max_length is not None:
            field_schema[f"max{length_prefix}"] = field.max_length
        if field.pattern is not None:
            field_schema["pattern"] = field.pattern
        if field.unique_items:
            field_schema["uniqueItems"] = True
        properties[field.wire_name] = field_schema
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
    builder = _Builder(allow_unknown=False)

    def schema(annotation: Any) -> dict[str, Any]:
        from .response import FileResponse, PreparedResponse, Response, StreamingResponse

        if isinstance(annotation, type):
            if issubclass(
                annotation, (Response, StreamingResponse, FileResponse, PreparedResponse)
            ):
                return {}
        before = len(builder.diagnostics)
        reference = builder.type_ref(annotation)
        if len(builder.diagnostics) != before:
            raise TypeError(builder.diagnostics[-1].message)
        return _openapi_schema(reference)

    image = app._application_image
    routes = list(image.routes())
    binding_specs = image.binding_specs()
    return_annotations = image.return_annotations()
    resolved_ids, _diagnostics = image.operation_ids()
    paths: dict[str, dict[str, Any]] = {}

    for index, definition in enumerate(routes):
        if not definition.include_in_schema:
            continue
        openapi_path = _PATH_CONVERTER.sub(r"{\1}", definition.path)
        operations = paths.setdefault(openapi_path, {})
        spec = binding_specs[index]
        returns = return_annotations[index]
        doc = inspect.getdoc(definition.endpoint)
        for method in definition.methods:
            operation: dict[str, Any] = {
                "operationId": resolved_ids[(index, method)],
            }
            if definition.tags:
                operation["tags"] = list(definition.tags)
            if definition.summary:
                operation["summary"] = definition.summary
            if definition.deprecated:
                operation["deprecated"] = True
            if definition.security:
                missing = [
                    name
                    for name, _scopes in definition.security
                    if name not in app._openapi_security_schemes
                ]
                if missing:
                    raise ValueError(
                        f"route {method} {definition.path} names undeclared OpenAPI "
                        f"security scheme(s): {', '.join(missing)}"
                    )
                operation["security"] = [
                    {name: list(scopes)} for name, scopes in definition.security
                ]
            if doc:
                operation["description"] = doc
            parameters: list[dict[str, Any]] = []
            if spec is not None:
                query_constraints = dict(spec.query_constraints)
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
                        constraint = query_constraints.get(_parameter_name)
                        if constraint is not None:
                            minimum, maximum, _overflow = constraint
                            if minimum is not None:
                                parameter["schema"]["minimum"] = minimum
                            if maximum is not None:
                                parameter["schema"]["maximum"] = maximum
                        parameters.append(parameter)
                if spec.body is not None:
                    body_schema = schema(spec.body[1])
                    body_content: dict[str, Any] = {"application/json": {"schema": body_schema}}
                    # `wreath.binding` reads a protobuf body for any `@message`
                    # annotation, and has since `_decode_protobuf_body` landed.
                    # A document that advertises only JSON understates what the
                    # endpoint accepts, and a generated client believes it.
                    if _is_message(spec.body[1]):
                        body_content[_PROTOBUF_MEDIA] = {"schema": body_schema}
                    operation["requestBody"] = {
                        "required": True,
                        "content": body_content,
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
                # Route registration has already established that a segment
                # starting with "{" is a complete, well-formed placeholder.
                for segment in definition.path.split("/"):
                    if segment.startswith("{"):
                        parameters.append(
                            {
                                "name": segment[1:-1],
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        )
            # The tape describes itself. Collected by asking every middleware
            # that actually covers this operation, so a router-scoped limiter
            # never decorates a route outside that router.
            contracts = _collect_contracts(app, definition, method)
            for contract in contracts:
                for header in contract.request_headers:
                    parameters.append(
                        {
                            "name": header.name,
                            "in": "header",
                            "required": header.required,
                            "schema": _header_schema(header),
                        }
                        | ({"description": header.description} if header.description else {})
                    )
            if parameters:
                operation["parameters"] = parameters
            response: dict[str, Any] = {"description": definition.response_description}
            response_schema = schema(returns)
            if response_schema:
                response["content"] = {definition.response_media_type: {"schema": response_schema}}
                # The same fact on the way out: a route whose return annotation
                # is a `@message` negotiates protobuf, so the document says so.
                # Only when the route has not overridden its media type -- an
                # explicit `response_media_type` is a decision, not a default.
                if _is_message(returns) and definition.response_media_type == "application/json":
                    response["content"][_PROTOBUF_MEDIA] = {"schema": response_schema}
            operation_responses = {str(definition.status_code): response}
            for status, declared in definition.responses:
                response_spec = (
                    declared if isinstance(declared, ResponseSpec) else ResponseSpec(declared)
                )
                additional: dict[str, Any] = {"description": response_spec.description}
                additional_schema = schema(response_spec.model)
                if additional_schema:
                    additional["content"] = {
                        response_spec.media_type: {"schema": additional_schema}
                    }
                operation_responses[str(status)] = additional
            # A middleware's response is additive: the route's own declaration
            # for a status is the more specific source and wins outright.
            for contract in contracts:
                for status, declared in contract.responses:
                    key = str(status)
                    if key in operation_responses:
                        continue
                    response_spec = (
                        declared if isinstance(declared, ResponseSpec) else ResponseSpec(declared)
                    )
                    from_tape: dict[str, Any] = {"description": response_spec.description}
                    tape_schema = schema(response_spec.model)
                    if tape_schema:
                        from_tape["content"] = {response_spec.media_type: {"schema": tape_schema}}
                    operation_responses[key] = from_tape
            for contract in contracts:
                for status, header in contract.response_headers:
                    key = str(definition.status_code if status is None else status)
                    target = operation_responses.get(key)
                    if target is None:
                        continue
                    headers = target.setdefault("headers", {})
                    entry: dict[str, Any] = {"schema": _header_schema(header)}
                    if header.description:
                        entry["description"] = header.description
                    headers.setdefault(header.name, entry)
            behaviours = sorted({name for contract in contracts for name in contract.behaviours})
            if behaviours:
                operation[BEHAVIOUR_EXTENSION] = behaviours
            operation["responses"] = operation_responses
            if method == "QUERY":
                operation["x-wreath-http-method"] = "QUERY"
                operations["x-wreath-query"] = operation
            else:
                operations[method.lower()] = operation

    components = {model.name: _component_schema(model) for model in builder.registry.models()}
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    component_document: dict[str, Any] = {}
    if components:
        component_document["schemas"] = components
    if app._openapi_security_schemes:
        component_document["securitySchemes"] = {
            name: dict(value) for name, value in app._openapi_security_schemes.items()
        }
    if component_document:
        document["components"] = component_document
    return document


def compare_openapi(
    previous: dict[str, Any], current: dict[str, Any]
) -> tuple[CompatibilityChange, ...]:
    """Return backwards-incompatible operation changes in stable order.

    The first lifecycle gate covers removals and request tightening: removed
    operations, newly required parameters, optional parameters made required,
    and removed response statuses. Additions are compatible and omitted.
    """
    changes: list[CompatibilityChange] = []
    current_paths = current.get("paths", {})
    for path, old_path in previous.get("paths", {}).items():
        new_path = current_paths.get(path)
        for method, old_operation in old_path.items():
            method_name = method.upper()
            if new_path is None or method not in new_path:
                changes.append(
                    CompatibilityChange(
                        "operation-removed",
                        f"{method_name} {path}",
                        "operation no longer exists",
                    )
                )
                continue
            new_operation = new_path[method]
            old_parameters = {
                (item["in"], item["name"]): item for item in old_operation.get("parameters", ())
            }
            new_parameters = {
                (item["in"], item["name"]): item for item in new_operation.get("parameters", ())
            }
            for key, new_parameter in new_parameters.items():
                old_parameter = old_parameters.get(key)
                if not new_parameter.get("required", False):
                    continue
                location = f"{method_name} {path} {key[0]}:{key[1]}"
                if old_parameter is None:
                    changes.append(
                        CompatibilityChange(
                            "required-parameter-added",
                            location,
                            "a new required parameter was added",
                        )
                    )
                elif not old_parameter.get("required", False):
                    changes.append(
                        CompatibilityChange(
                            "parameter-became-required",
                            location,
                            "an optional parameter became required",
                        )
                    )
            old_responses = old_operation.get("responses", {})
            new_responses = new_operation.get("responses", {})
            for status in old_responses.keys() - new_responses.keys():
                changes.append(
                    CompatibilityChange(
                        "response-removed",
                        f"{method_name} {path} {status}",
                        "a documented response status was removed",
                    )
                )
            # A behaviour that stops being declared is a silent correctness
            # regression at the consumer: a generated client that stops
            # sending an idempotency key still compiles, still passes its
            # tests, and duplicates a write the first time it retries.
            old_behaviours = set(old_operation.get(BEHAVIOUR_EXTENSION, ()))
            new_behaviours = set(new_operation.get(BEHAVIOUR_EXTENSION, ()))
            for behaviour in sorted(old_behaviours - new_behaviours):
                changes.append(
                    CompatibilityChange(
                        "behaviour-removed",
                        f"{method_name} {path} {behaviour}",
                        "a declared client behaviour was removed",
                    )
                )
    return tuple(changes)


_DOCS_STYLE = """
:root{--paper:#F6F7F9;--raise:#fff;--ink:#0E141B;--muted:#5A6672;--rule:#DDE2E8;
--brass:#8A6416;--good:#0B6E4F;--bad:#A8341A;--accent:#00838F}
@media(prefers-color-scheme:dark){:root{--paper:#0E141B;--raise:#151C24;--ink:#E7ECF1;
--muted:#9AA6B2;--rule:#28313B;--brass:#D3A248;--good:#3FBF8F;--bad:#E4785F;--accent:#4DD0E1}}
:root[data-theme=dark]{--paper:#0E141B;--raise:#151C24;--ink:#E7ECF1;--muted:#9AA6B2;
--rule:#28313B;--brass:#D3A248;--good:#3FBF8F;--bad:#E4785F;--accent:#4DD0E1}
:root[data-theme=light]{--paper:#F6F7F9;--raise:#fff;--ink:#0E141B;--muted:#5A6672;
--rule:#DDE2E8;--brass:#8A6416;--good:#0B6E4F;--bad:#A8341A;--accent:#00838F}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:1.4rem 1.6rem;border-bottom:1px solid var(--rule);background:var(--raise)}
h1{margin:0;font-size:1.3rem}.ver{margin:.2rem 0 0;color:var(--muted);font-size:.85rem}
.ver a{color:var(--accent)}main{max-width:60rem;margin:0 auto;padding:1.2rem 1.6rem}
section.tag{margin:1.4rem 0}section.tag>h2{font-size:1rem;text-transform:uppercase;
letter-spacing:.04em;color:var(--muted);border-bottom:1px solid var(--rule);padding-bottom:.3rem}
details{background:var(--raise);border:1px solid var(--rule);border-radius:6px;margin:.5rem 0}
summary{cursor:pointer;padding:.6rem .8rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:.9rem}.op{padding:0 .8rem .8rem;border-top:1px solid var(--rule)}
.desc{color:var(--muted);white-space:pre-wrap}
.m{display:inline-block;min-width:3.6rem;text-align:center;padding:.1rem .4rem;border-radius:4px;
font-size:.72rem;font-weight:700;color:#fff}.m-get{background:var(--good)}.m-post{background:var(--accent)}
.m-put{background:var(--brass)}.m-patch{background:var(--brass)}.m-delete{background:var(--bad)}
table{border-collapse:collapse;width:100%;margin:.5rem 0;font-size:.85rem}
th,td{text-align:left;padding:.3rem .5rem;border-bottom:1px solid var(--rule)}
th{color:var(--muted);font-weight:600}pre{background:var(--paper);border:1px solid var(--rule);
border-radius:4px;padding:.5rem;overflow:auto;font-size:.8rem}code{font-family:ui-monospace,Menlo,monospace}
.op>h3{margin:.7rem 0 .2rem;font-size:.8rem;text-transform:uppercase;
letter-spacing:.03em;color:var(--muted)}
button.try{margin:.4rem 0;padding:.3rem .7rem;border:1px solid var(--rule);border-radius:4px;
background:var(--accent);color:#fff;cursor:pointer;font-size:.8rem}
.try-out{white-space:pre-wrap;margin:.3rem 0 0}
"""

_DOCS_SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} — API</title>
<style nonce="{{ nonce }}">{{ style }}</style></head>
<body><header><h1>{{ title }}</h1>
<p class="ver">v{{ version }} · <a href="{{ spec_path }}">OpenAPI document</a></p></header>
<main>{{ body }}</main>{{ script }}</body></html>
"""

# Same-origin try-it-out console. Never proxies through the server (no SSRF),
# attaches the double-submit CSRF header from the cookie, sends same-origin creds.
_TRY_SCRIPT = """
function wreathCsrf(){var m=document.cookie.match(/(?:^|; )wreath_csrf=([^;]+)/);
return m?decodeURIComponent(m[1]):''}
document.querySelectorAll('button.try').forEach(function(b){b.addEventListener('click',function(){
var out=b.nextElementSibling;out.textContent='…';var h={};var c=wreathCsrf();
if(c&&!['GET','HEAD','OPTIONS','QUERY'].includes(b.dataset.method)){h['x-csrf-token']=c}
fetch(b.dataset.path,{method:b.dataset.method,credentials:'same-origin',headers:h})
.then(function(r){return r.text().then(function(t){out.textContent=r.status+'\\n'+t})})
.catch(function(e){out.textContent='error: '+e})})});
"""


def _type_label(ref: TypeRef) -> str:
    if ref.kind == "reference" and ref.name:
        return ref.name
    if ref.kind == "array" and ref.arguments:
        return f"{_type_label(ref.arguments[0])}[]"
    if ref.kind == "literal":
        return " | ".join(repr(value) for value in ref.literals)
    return ref.kind


def _schema_json(ref: TypeRef) -> str:
    from ._json import dumps as _json_dumps

    return _json_dumps(_openapi_schema(ref)).decode("utf-8")


def _render_operation(operation: Any, escape: Any, try_it_out: bool) -> str:
    method = operation.method
    head = (
        f'<span class="m m-{escape(method.lower())}">{escape(method)}</span> '
        f"<code>{escape(operation.path)}</code>"
    )
    if operation.summary:
        head += f" — {escape(operation.summary)}"
    out = [f'<details><summary>{head}</summary><div class="op">']
    if operation.description:
        out.append(f'<p class="desc">{escape(operation.description)}</p>')
    if operation.parameters:
        out.append(
            '<table><thead><tr><th scope="col">name</th><th scope="col">in</th>'
            '<th scope="col">required</th><th scope="col">type</th></tr></thead><tbody>'
        )
        for parameter in operation.parameters:
            out.append(
                f"<tr><td><code>{escape(parameter.wire_name)}</code></td>"
                f"<td>{escape(parameter.location)}</td>"
                f"<td>{'yes' if parameter.required else 'no'}</td>"
                f"<td>{escape(_type_label(parameter.type))}</td></tr>"
            )
        out.append("</tbody></table>")
    if operation.request_body is not None:
        out.append(
            f"<h3>Request body</h3><pre>{escape(_schema_json(operation.request_body))}</pre>"
        )
    out.append(f"<h3>Response</h3><pre>{escape(_schema_json(operation.response_body))}</pre>")
    if try_it_out:
        out.append(
            f'<button class="try" data-method="{escape(method)}" '
            f'data-path="{escape(operation.path)}">Try it</button>'
            '<pre class="try-out"></pre>'
        )
    out.append("</div></details>")
    return "".join(out)


def render_operations(model: Any, try_it_out: bool = False) -> str:
    """Render the operation cards of an `ApiModel` as escaped HTML (a str of
    already-safe markup). Grouped by first tag."""
    from html import escape

    groups: dict[str, list[Any]] = {}
    for operation in model.operations:
        tag = operation.tags[0] if operation.tags else "default"
        groups.setdefault(tag, []).append(operation)
    parts: list[str] = []
    for tag in sorted(groups):
        parts.append(f'<section class="tag"><h2>{escape(tag)}</h2>')
        for operation in sorted(groups[tag], key=lambda o: (o.path, o.method)):
            parts.append(_render_operation(operation, escape, try_it_out))
        parts.append("</section>")
    return "".join(parts)


def render_models(model: Any) -> str:
    """Render the component schemas of an `ApiModel` as escaped HTML."""
    from html import escape

    if not model.models:
        return ""
    from ._json import dumps as _json_dumps

    parts = ['<section class="tag"><h2>Schemas</h2>']
    for component in model.models:
        schema = _json_dumps(_component_schema(component)).decode("utf-8")
        parts.append(
            f"<details><summary><code>{escape(component.name)}</code></summary>"
            f'<div class="op"><pre>{escape(schema)}</pre></div></details>'
        )
    parts.append("</section>")
    return "".join(parts)


def render_docs_body(
    app: Any, *, title: str = "Wreath", version: str = "0.1.0", try_it_out: bool = False
) -> str:
    """Build the (nonce-free) inner docs HTML for `app`. Expensive part --
    caches well; the per-request shell wraps it with a CSP nonce."""
    from .typegen.inspect import build_api_model

    model = build_api_model(app, title=title, version=version, allow_unknown=True)
    return render_operations(model, try_it_out) + render_models(model)


def render_docs_shell(
    *,
    title: str,
    version: str,
    spec_path: str,
    nonce: str,
    body: str,
    try_it_out: bool = False,
) -> str:
    """Wrap a cached docs body in the outer shell, injecting the per-response
    CSP `nonce` on the single inline `<style>`/`<script>`."""
    from html import escape

    from .templates import Markup, Template

    if try_it_out:
        script = Markup(f'<script nonce="{escape(nonce, quote=True)}">{_TRY_SCRIPT}</script>')
    else:
        script = Markup("")
    return Template.from_string(_DOCS_SHELL).render(
        title=title,
        version=version,
        spec_path=spec_path,
        nonce=nonce,
        style=Markup(_DOCS_STYLE),
        body=Markup(body),
        script=script,
    )


__all__ = [
    "generate_openapi",
    "render_docs_body",
    "render_docs_shell",
    "render_models",
    "render_operations",
]
