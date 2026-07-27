"""OpenAPI 3.1 document generation from typed route signatures.

The same signature inspection that drives request binding (`wreath.binding`)
produces the schema, so the docs can never drift from the validation:

```python
app = Wreath()
app.enable_docs()          # /openapi.json and /docs
```
Dataclass bodies become component schemas; typed path/query parameters become
parameter objects; a dataclass or scalar return annotation becomes the 200
response schema. Handlers without typed signatures still appear with their
paths and methods.
"""

from __future__ import annotations

import inspect
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

    image = app._application_image
    routes = list(image.routes())
    binding_specs = image.binding_specs()
    resolved_ids, _diagnostics = resolve_operation_ids(routes)
    paths: dict[str, dict[str, Any]] = {}

    for index, definition in enumerate(routes):
        operations = paths.setdefault(definition.path, {})
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


# Self-contained API-docs renderer. No CDN, no external assets: the page is
# rendered from the same `ApiModel` that feeds the spec and the typed clients,
# so the three can never drift. Every user-authored string is escaped; the only
# `Markup` (unescaped) fragments are framework-generated.
#
# TODO: de-dup these tokens with `_devtools/bench_report.py::_STYLE` -- copied
# here to keep this subsystem within its file boundary.
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
