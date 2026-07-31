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

from .typegen.inspect import _Builder, _return_annotation, resolve_operation_ids
from .typegen.model import Model, TypeRef

_TYPE_KEYS: dict[str, dict[str, Any]] = {
    "null": {"type": "null"},
    "boolean": {"type": "boolean"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "string": {"type": "string"},
}

_PATH_CONVERTER = re.compile(r"\{([^}:]+):path\}")


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
    raise ValueError(
        f"no OpenAPI schema for TypeKind {ref.kind!r}. A kind added to "
        "`wreath.typegen.model.TypeKind` must be rendered here and in "
        "`wreath._pure.typegen.ts_type`; returning a default instead would emit a "
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
        if annotation is Any or annotation is inspect.Parameter.empty:
            return {}
        from .response import FileResponse, PreparedResponse, Response, StreamingResponse

        if isinstance(annotation, type) and issubclass(
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
    resolved_ids, _diagnostics = resolve_operation_ids(routes)
    paths: dict[str, dict[str, Any]] = {}

    for index, definition in enumerate(routes):
        if not definition.include_in_schema:
            continue
        openapi_path = _PATH_CONVERTER.sub(r"{\1}", definition.path)
        operations = paths.setdefault(openapi_path, {})
        spec = binding_specs[index]
        returns = _return_annotation(definition.endpoint)
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
            doc = inspect.getdoc(definition.endpoint)
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
                        if location == "query":
                            constraint = query_constraints.get(_parameter_name)
                            if constraint is not None:
                                minimum, maximum, _overflow = constraint
                                if minimum is not None:
                                    parameter["schema"]["minimum"] = minimum
                                if maximum is not None:
                                    parameter["schema"]["maximum"] = maximum
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
            response: dict[str, Any] = {"description": definition.response_description}
            response_schema = schema(returns)
            if response_schema:
                response["content"] = {
                    definition.response_media_type: {"schema": response_schema}
                }
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
            operation["responses"] = operation_responses
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
                (item["in"], item["name"]): item
                for item in old_operation.get("parameters", ())
            }
            new_parameters = {
                (item["in"], item["name"]): item
                for item in new_operation.get("parameters", ())
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
    return tuple(changes)


# Self-contained API-docs renderer. No CDN, no external assets: the page is
# rendered from the same `ApiModel` that feeds the spec and the typed clients,
# so the three can never drift. Every user-authored string is escaped; the only
# `Markup` (unescaped) fragments are framework-generated.
#
# These tokens look like `_devtools/bench_report.py::_STYLE` and are not it. The
# two stylesheets share **no** line, and of the eight custom-property names they
# have in common, seven carry different values (`--paper` #0E141B vs #0D1116,
# `--ink` #E7ECF1 vs #E6EAF0, and so on); each also declares tokens the other
# does not. They are two palettes that were forked, not one copied twice, so
# extracting a shared block would mean reconciling seven colours and changing
# how one of the two pages renders -- a visual decision, not a de-duplication.
# A stale "TODO: de-dup these" lived here and prompted exactly that merge; it is
# recorded as measured instead.
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
if(c&&b.dataset.method!=='GET'){h['x-csrf-token']=c}
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
    out = [f"<details><summary>{head}</summary><div class=\"op\">"]
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
