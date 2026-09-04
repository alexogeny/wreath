"""The Python target: a typed `ServiceClient` subclass for a sibling service.

Service-to-service calls were stringly typed -- `await client.get(f"/llamas/{id}")`
returning `Any`, with the caller re-parsing what came back. Everything needed to
do better already existed separately: a strict OpenAPI generator, a
compatibility comparator, a binding layer that builds validators from ordinary
Python types, and a `ServiceClient` that binds a base path and an
auto-refreshing bearer token.

This target joins them. What it emits:

* `models.py` -- one dataclass per model the API returns or accepts.
* `client.py` -- a `ServiceClient` subclass with one typed method per
  operation, and a `SPEC_DIGEST` naming the contract it was generated from.

Two rules shape everything here.

**No transport.** The generated client subclasses `ServiceClient` and calls
`self.request(...)`. Pooling, retries, rate limiting, origin pinning and token
refresh are solved in `wreath.http_client`, and a generated file must not
re-solve them -- a bug fixed there must not need regenerating to reach anyone.

**Responses bind through `wreath.binding.validate`**, the same function the
server uses on the way in. An extra field on the wire is refused exactly as it
would be server-side. That strictness is the feature: a provider that starts
sending a field you do not model is a change you want to hear about at the
boundary, not three layers in where the shape has already been passed around.
"""

from __future__ import annotations

import hashlib
import json
import keyword
from collections.abc import Iterable, Iterator
from typing import Any

from ..model import ApiModel, Diagnostic, Model, Operation, Parameter, TypegenError, TypeRef

#: Scalars that need no import in the generated module.
_PRIMITIVES: dict[str, str] = {
    "unknown": "Any",
    "null": "None",
    "boolean": "bool",
    "integer": "int",
    "number": "float",
    "string": "str",
}

#: Named scalars, keyed by `TypeRef.name`, and what each needs imported.
#:
#: These all arrive as `kind="string"` with a `name` -- `DATE_TIME` is
#: `TypeRef("string", "date-time")`. Matching on `kind` alone emitted `str` for
#: every one of them, which type-checks, round-trips, and silently loses the
#: zone on a timestamp. Binding through `wreath.binding.validate` is what makes
#: the annotation load-bearing rather than decorative: `Instant` in the
#: dataclass is what coerces the wire string back to an aware instant, and what
#: refuses a naive one.
_NAMED_SCALARS: dict[str, tuple[str, str | None]] = {
    "date-time": ("Instant", "from wreath.temporal import Instant"),
    "date": ("datetime.date", "import datetime"),
    "uuid": ("UUID", "from uuid import UUID"),
    "decimal": ("Decimal", "from decimal import Decimal"),
    "byte": ("bytes", None),
    "coordinate": ("Coordinate", "from wreath.geospatial import Coordinate"),
}


#: The document the package was generated from, kept beside the client so the
#: contract gate has a baseline. Named here so the CLI and the target agree.
SPEC_FILE = "spec.json"


def _doc_text(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)[1:-1]


def _bytes_source(value: bytes) -> str:
    body = "".join(
        chr(byte)
        if 0x20 <= byte <= 0x7E and byte not in (ord('"'), ord("\\"))
        else f"\\x{byte:02x}"
        for byte in value
    )
    return f'b"{body}"'


def _python_identifier(value: object) -> bool:
    return isinstance(value, str) and value.isidentifier() and not keyword.iskeyword(value)


def _validate_identifiers(api: ApiModel, class_name: object) -> None:
    diagnostics = []
    if not _python_identifier(class_name):
        diagnostics.append(
            Diagnostic(
                f"class_name must be a Python identifier that is not a keyword, "
                f"got {class_name!r}"
            )
        )
    for model in api.models:
        if not _python_identifier(model.name):
            diagnostics.append(
                Diagnostic(
                    f"model name must be a Python identifier that is not a keyword, "
                    f"got {model.name!r}"
                )
            )
        for field in model.fields:
            if not _python_identifier(field.wire_name):
                diagnostics.append(
                    Diagnostic(
                        f"model field name must be a Python identifier that is not a keyword, "
                        f"got {field.wire_name!r}",
                        location=f"model {model.name}",
                    )
                )
    for operation in api.operations:
        generated = _snake(operation.id)
        if not _python_identifier(generated):
            diagnostics.append(
                Diagnostic(
                    f"operation id {operation.id!r} generates unusable Python method "
                    f"{generated!r}; choose a non-keyword identifier",
                    operation_id=operation.id,
                )
            )
    if diagnostics:
        raise TypegenError(tuple(diagnostics))


def _snake(text: str) -> str:
    """`listLlamas` -> `list_llamas`. Operation ids arrive camelCased."""
    out: list[str] = []
    for index, character in enumerate(text):
        if character.isupper() and index and not text[index - 1].isupper():
            out.append("_")
        out.append(character.lower())
    return "".join(out).replace("-", "_").replace("__", "_")


def _annotation(ref: TypeRef) -> str:
    """A TypeRef as Python source. Mirrors the TypeScript target's mapping."""
    if ref.name is not None and ref.name in _NAMED_SCALARS:
        # Before the primitive table: a named scalar carries `kind="string"`,
        # so checking `kind` first would answer `str` and lose the type.
        return _NAMED_SCALARS[ref.name][0]
    if ref.kind in _PRIMITIVES:
        return _PRIMITIVES[ref.kind]
    if ref.kind == "page":
        inner = _annotation(ref.arguments[0]) if ref.arguments else "Any"
        return f"Page[{inner}]"
    if ref.kind == "reference":
        return ref.name or "Any"
    if ref.kind == "array":
        inner = _annotation(ref.arguments[0]) if ref.arguments else "Any"
        return f"list[{inner}]"
    if ref.kind == "tuple":
        if not ref.arguments:
            return "tuple[()]"
        return "tuple[" + ", ".join(_annotation(a) for a in ref.arguments) + "]"
    if ref.kind == "record":
        value = _annotation(ref.arguments[0]) if ref.arguments else "Any"
        return f"dict[str, {value}]"
    if ref.kind == "union":
        if not ref.arguments:
            return "Any"
        return " | ".join(_annotation(a) for a in ref.arguments)
    if ref.kind == "literal":
        if not ref.literals:
            return "Any"
        return "Literal[" + ", ".join(repr(v) for v in ref.literals) + "]"
    return "Any"


def _is_optional(ref: TypeRef) -> bool:
    """True when the type already admits None, so `| None` must not be added.

    A field annotated `str | None` arrives as a union carrying a null member.
    Appending another `| None` produced `str | None | None`, which is legal
    Python and reads like a generator that does not understand its own input.
    """
    if ref.kind == "null":
        return True
    return ref.kind == "union" and any(a.kind == "null" for a in ref.arguments)


def _optional(ref: TypeRef) -> str:
    annotation = _annotation(ref)
    return annotation if _is_optional(ref) else f"{annotation} | None"


def _walk(ref: TypeRef) -> Iterator[TypeRef]:
    yield ref
    for argument in ref.arguments:
        yield from _walk(argument)


def _model_refs(api: ApiModel) -> Iterator[TypeRef]:
    for model in api.models:
        for field in model.fields:
            yield from _walk(field.type)


def _operation_refs(api: ApiModel) -> Iterator[TypeRef]:
    for operation in api.operations:
        yield from _walk(operation.response_body)
        if operation.request_body is not None:
            yield from _walk(operation.request_body)
        for parameter in operation.parameters:
            yield from _walk(parameter.type)


def _extra_imports(refs: Iterable[TypeRef]) -> list[str]:
    """Import lines the named scalars and `Page` in `refs` need.

    Computed per module rather than for the whole API, because an import a
    module does not use is a ruff failure -- and emitting the union everywhere
    would mean the generated package only lints by luck of every shape being
    present somewhere.
    """
    lines: set[str] = set()
    for ref in refs:
        if ref.name is not None and ref.name in _NAMED_SCALARS:
            statement = _NAMED_SCALARS[ref.name][1]
            if statement is not None:
                lines.add(statement)
        if ref.kind == "page":
            lines.add("from wreath.pagination import Page")
    return sorted(lines)


def _needs_literal(refs: Iterable[TypeRef]) -> bool:
    return any(ref.kind == "literal" for ref in refs)


def _model_source(model: Model) -> str:
    """One dataclass. Required fields first, so the generated source is valid."""
    lines = ["@dataclass(frozen=True, slots=True)", f"class {model.name}:"]
    required = [f for f in model.fields if f.required]
    optional = [f for f in model.fields if not f.required]
    if not required and not optional:
        lines.append("    pass")
        return "\n".join(lines)
    for field in required:
        lines.append(f"    {field.wire_name}: {_annotation(field.type)}")
    for field in optional:
        lines.append(f"    {field.wire_name}: {_optional(field.type)} = None")
    return "\n".join(lines)


def _models_module(api: ApiModel) -> str:
    body = "\n\n\n".join(_model_source(model) for model in api.models)
    literal = ", Literal" if _needs_literal(_model_refs(api)) else ""
    extra = _extra_imports(_model_refs(api))
    imports = "\n".join(extra) + "\n" if extra else ""
    title = _doc_text(api.title)
    version = _doc_text(api.version)
    return f'''"""Generated by wreath typegen. Do not edit.

Response and request shapes for {title} {version}.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any{literal}

{imports}
{body}
'''


def _signature(operation: Operation, api: ApiModel) -> tuple[str, list[Parameter]]:
    """The method signature, with required parameters ahead of optional ones.

    `idempotency_key` is added here, in the keyword-only section, rather than
    spliced into the rendered line afterwards. Splicing was a defect: an
    operation with any keyword parameter already had a `*`, the splice declined,
    and the emitted call still referenced `idempotency_key` -- generated code
    that raises `NameError` on its first use. Building the signature in one
    place makes that unrepresentable.
    """
    ordered = [p for p in operation.parameters if p.required]
    ordered += [p for p in operation.parameters if not p.required]
    parts = ["self"]
    positional = [p for p in ordered if p.location == "path"]
    keyword = [p for p in ordered if p.location != "path"]
    for parameter in positional:
        parts.append(f"{parameter.python_name}: {_annotation(parameter.type)}")
    if operation.request_body is not None:
        parts.append(f"body: {_annotation(operation.request_body)}")
    wants_key = "idempotency-key" in operation.behaviours
    if keyword or wants_key:
        parts.append("*")
        for parameter in keyword:
            annotation = _annotation(parameter.type)
            if parameter.required:
                parts.append(f"{parameter.python_name}: {annotation}")
            else:
                parts.append(f"{parameter.python_name}: {_optional(parameter.type)} = None")
        if wants_key:
            parts.append("idempotency_key: str | None = None")
    return ", ".join(parts), keyword


def _method_source(operation: Operation, api: ApiModel) -> str:
    signature, keyword = _signature(operation, api)
    name = _snake(operation.id)
    returns = _annotation(operation.response_body)
    path_expression = operation.path
    for parameter in operation.parameters:
        if parameter.location == "path":
            path_expression = path_expression.replace(
                "{" + parameter.wire_name + "}", "{" + parameter.python_name + "}"
            )
    query = [p for p in keyword if p.location == "query"]
    header = [p for p in keyword if p.location == "header"]

    lines = [f"    async def {name}({signature}) -> {returns}:"]
    summary = operation.summary or f"`{operation.method} {operation.path}`."
    lines.append(f'        """{_doc_text(summary)}')
    lines.append("")
    route = _doc_text(f"{operation.method} {operation.path}")
    lines.append(f"        Generated from `{route}`.")
    if operation.behaviours:
        lines.append(f"        The server declared: {', '.join(operation.behaviours)}.")
    lines.append('        """')
    lines.append(f"        path = f{json.dumps(path_expression, ensure_ascii=True)}")
    if query:
        lines.append("        query: list[tuple[str, str]] = []")
        for parameter in query:
            lines.append(f"        if {parameter.python_name} is not None:")
            lines.append(
                f"            query.append(({json.dumps(parameter.wire_name)}, "
                f"str({parameter.python_name})))"
            )
        lines.append("        if query:")
        lines.append('            path = f"{path}?{_urlencode(query)}"')
    if header:
        lines.append("        headers: list[tuple[bytes, bytes]] = []")
        for parameter in header:
            lines.append(f"        if {parameter.python_name} is not None:")
            header_name = _bytes_source(parameter.wire_name.lower().encode("latin-1"))
            lines.append(
                f"            headers.append(({header_name}, "
                f'str({parameter.python_name}).encode("latin-1")))'
            )
    body_argument = ""
    if operation.request_body is not None:
        lines.append("        payload = _dump(body)")
        body_argument = ", body=payload"
    header_argument = ", headers=tuple(headers)" if header else ""
    # The server declared it honours a key on this operation, so a retry is the
    # same request rather than a second one. Generated because the contract said
    # so, not because the method looked unsafe. The parameter itself is declared
    # in `_signature`.
    idempotency = (
        ", idempotency_key=idempotency_key or _new_key()"
        if "idempotency-key" in operation.behaviours
        else ""
    )
    lines.append(
        f'        response = await self.request("{operation.method}", path'
        f"{header_argument}{body_argument}{idempotency})"
    )
    lines.append(f"        return _bind({returns}, response)")
    return "\n".join(lines)


def _client_module(api: ApiModel, digest: str, class_name: str) -> str:
    methods = "\n\n".join(_method_source(op, api) for op in api.operations)
    names = sorted({model.name for model in api.models})
    imports = f"from .models import {', '.join(names)}\n" if names else ""
    for statement in _extra_imports(_operation_refs(api)):
        imports = f"{statement}\n{imports}"
    title = _doc_text(api.title)
    version = _doc_text(api.version)
    return f'''"""Generated by wreath typegen. Do not edit.

A typed client for {title} {version}.

`SPEC_DIGEST` names the contract this was generated from. It is **not**
verified at runtime -- a client that refused to start because the provider
added an optional field would be an outage generator, and OpenAPI says such a
change is compatible. Verification belongs in CI, where a breaking change
should fail a build rather than a request.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, is_dataclass
from typing import Any, Literal
from urllib.parse import urlencode as _urlencode_pairs

from wreath.binding import validate as _validate
from wreath.service_client import ServiceClient

{imports}
#: sha256 over the canonical OpenAPI document this client was generated from.
SPEC_DIGEST = "{digest}"


def _urlencode(pairs: list[tuple[str, str]]) -> str:
    return _urlencode_pairs(pairs)


def _new_key() -> str:
    return str(uuid.uuid4())


def _dump(value: Any) -> bytes:
    """Serialise a request body without inventing a second encoder."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if is_dataclass(value) and not isinstance(value, type):
        return json.dumps(asdict(value)).encode("utf-8")
    return json.dumps(value).encode("utf-8")


def _bind(annotation: Any, response: Any) -> Any:
    """Decode and validate through the *server's* validator, not a second one.

    `wreath.binding.validate` is what the provider runs on the way in, so an
    extra field is refused here exactly as it would be there.
    """
    body = getattr(response, "body", b"")
    if not body:
        return None
    decoded = json.loads(body)
    return _validate(annotation, decoded)


class {class_name}(ServiceClient):
    """Typed calls against {title}.

    Transport, pooling, retries and token refresh come from `ServiceClient`;
    nothing here re-implements them.
    """

{methods if methods else "    pass"}
'''


def spec_digest(document: dict[str, Any]) -> str:
    """A stable digest over the document, independent of key order."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_python(
    api: ApiModel,
    *,
    document: dict[str, Any] | None = None,
    class_name: str = "GeneratedServiceClient",
) -> dict[str, str]:
    """Return `{filename: contents}` for the Python target.

    `spec.json` is the document this was generated from, retained so
    `wreath typegen --check-contract` has a *previous* to compare against.
    `SPEC_DIGEST` alone cannot serve: a digest answers "changed or not", and
    telling a breaking change from a compatible one needs the document itself.
    """
    _validate_identifiers(api, class_name)
    digest = spec_digest(document) if document is not None else ""
    files: dict[str, str] = {}
    if document is not None:
        files[SPEC_FILE] = json.dumps(document, indent=2, sort_keys=True) + "\n"
    files["models.py"] = _models_module(api)
    files["client.py"] = _client_module(api, digest, class_name)
    files["__init__.py"] = (
        '"""Generated by wreath typegen. Do not edit."""\n\n'
        f"from .client import SPEC_DIGEST, {class_name}\n"
        f'\n__all__ = ["SPEC_DIGEST", "{class_name}"]\n'
    )
    return files


__all__ = ["render_python", "spec_digest"]
