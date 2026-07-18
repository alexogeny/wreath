"""Plan an :class:`ApiModel` into TypeScript output files.

The planner normalizes the semantic model into the renderer's tuple contract,
computes deterministic client identifiers (camelCase) while preserving wire
names, and assembles the file set. Rendering of the two size-scaling modules
(models, client) goes through the pure/native facade; the small structural files
(index, manifest, react-query) are assembled here.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..model import ApiModel, Operation, TypeRef
from ..render import select_renderers

MANIFEST_NAME = "wreath-typegen.json"


def _camel(text: str) -> str:
    parts = [part for part in re.split(r"[_\-]", text) if part]
    if not parts:
        return text
    head = parts[0]
    return head[:1].lower() + head[1:] + "".join(
        part[:1].upper() + part[1:] for part in parts[1:]
    )


def _pascal(text: str) -> str:
    parts = [part for part in re.split(r"[_\-]", text) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _tuplize(ref: TypeRef) -> tuple[Any, ...]:
    return (ref.kind, ref.name, tuple(_tuplize(arg) for arg in ref.arguments), ref.literals)


def _param_interface_name(operation: Operation) -> str:
    return _pascal(operation.id) + "Parameters"


def _client_params(operation: Operation) -> list[tuple[str, str, str, tuple[Any, ...], bool]]:
    # Cookies are server-managed for browser targets; excluded from the client.
    return [
        (_camel(param.python_name), param.wire_name, param.location, _tuplize(param.type),
         param.required)
        for param in operation.parameters
        if param.location in ("path", "query", "header")
    ]


def _declarations(api: ApiModel) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    declarations: list[tuple[str, tuple[Any, ...]]] = []
    for model in api.models:
        fields = tuple(
            (field.wire_name, _tuplize(field.type), field.required) for field in model.fields
        )
        declarations.append((model.name, fields))
    # Per-operation parameter interfaces, deterministic by operation id order.
    for operation in api.operations:
        params = _client_params(operation)
        if not params:
            continue
        fields = tuple(
            (client_name, type_tuple, required)
            for client_name, _wire, _location, type_tuple, required in params
        )
        declarations.append((_param_interface_name(operation), fields))
    return tuple(declarations)


def _operation_tuples(api: ApiModel) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            operation.id,
            operation.method,
            operation.path,
            tuple(_client_params(operation)),
            _tuplize(operation.request_body) if operation.request_body is not None else None,
            operation.request_body_media_type,
            _tuplize(operation.response_body),
        )
        for operation in api.operations
    )


def _index_module(react_query: bool, base_url_env: str | None) -> str:
    from ..._pure.typegen import GENERATOR_HEADER

    lines = [GENERATOR_HEADER, '\nexport * from "./models";\nexport * from "./client";\n']
    if react_query:
        lines.append('export * from "./react-query";\n')
    if base_url_env:
        lines.append(
            f"\nexport const defaultBaseUrl: string =\n"
            f"  (import.meta as unknown as {{ env?: Record<string, string> }}).env?."
            f"{base_url_env} ?? \"\";\n"
        )
    return "".join(lines)


def _referenced_names(api: ApiModel) -> list[str]:
    names = {model.name for model in api.models}
    for operation in api.operations:
        if any(param.location in ("path", "query", "header") for param in operation.parameters):
            names.add(_param_interface_name(operation))
    return sorted(names)


def _react_query_module(api: ApiModel) -> str:
    from ..._pure.typegen import GENERATOR_HEADER, ts_type

    names = _referenced_names(api)
    import_names = ",\n  ".join(names)
    lines = [
        GENERATOR_HEADER,
        "\nimport { createContext, useContext } from \"react\";\n",
        "import {\n  useQuery,\n  useMutation,\n"
        "  type UseQueryOptions,\n  type UseMutationOptions,\n"
        "} from \"@tanstack/react-query\";\n",
        "import { WreathClient, WreathApiError } from \"./client\";\n",
    ]
    if names:
        lines.append(f"import type {{\n  {import_names},\n}} from \"./models\";\n")
    lines.append(
        "\nconst WreathClientContext = createContext<WreathClient | null>(null);\n"
        "export const WreathClientProvider = WreathClientContext.Provider;\n\n"
        "export function useWreathClient(): WreathClient {\n"
        "  const client = useContext(WreathClientContext);\n"
        "  if (client === null) {\n"
        "    throw new Error(\"WreathClientProvider is missing from the tree\");\n"
        "  }\n"
        "  return client;\n"
        "}\n"
    )
    for operation in api.operations:
        lines.append("\n" + _react_hook(operation, ts_type))
    return "".join(lines)


def _react_hook(operation: Operation, ts_type: Any) -> str:
    has_params = any(
        param.location in ("path", "query", "header") for param in operation.parameters
    )
    param_type = _pascal(operation.id) + "Parameters"
    response = ts_type(_tuplize(operation.response_body))
    body_ref = operation.request_body
    is_query = operation.method in ("GET", "HEAD")
    key = f'["{operation.id}"'
    if has_params:
        key += ", parameters"
    key += "] as const"

    args: list[str] = []
    if has_params:
        args.append(f"parameters: {param_type}")
    call_args = "parameters" if has_params else ""
    if is_query:
        args.append(
            f'options?: Omit<UseQueryOptions<{response}, WreathApiError>, '
            f'"queryKey" | "queryFn">'
        )
        signature = ", ".join(args)
        return (
            f"export function use{_pascal(operation.id)}({signature}) {{\n"
            f"  const client = useWreathClient();\n"
            f"  return useQuery({{\n"
            f"    ...options,\n"
            f"    queryKey: {key},\n"
            f"    queryFn: () => client.{operation.id}({call_args}),\n"
            f"  }});\n"
            f"}}\n"
        )
    # Mutation: variables carry the body (and parameters when present).
    body_type = ts_type(_tuplize(body_ref)) if body_ref is not None else "void"
    var_type = body_type
    call = call_args
    if body_ref is not None:
        call = (call_args + ", variables") if call_args else "variables"
    args.append(
        f'options?: Omit<UseMutationOptions<{response}, WreathApiError, {var_type}>, '
        f'"mutationFn">'
    )
    signature = ", ".join(args)
    variables_param = "variables" if body_ref is not None else "_variables"
    return (
        f"export function use{_pascal(operation.id)}({signature}) {{\n"
        f"  const client = useWreathClient();\n"
        f"  return useMutation({{\n"
        f"    ...options,\n"
        f"    mutationFn: ({variables_param}: {var_type}) => client.{operation.id}({call}),\n"
        f"  }});\n"
        f"}}\n"
    )


def _manifest(files: list[str], api: ApiModel, backend: str) -> str:
    from ..._pure.typegen import TYPEGEN_CONTRACT

    document = {
        "generator": "wreath-typegen",
        "contract": TYPEGEN_CONTRACT,
        "renderer": backend,
        "title": api.title,
        "version": api.version,
        "files": sorted(files),
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_typescript(
    api: ApiModel,
    *,
    react_query: bool = False,
    base_url_env: str | None = None,
    pure: bool = False,
) -> dict[str, str]:
    """Return ``{filename: contents}`` for the TypeScript target."""
    render_models, render_client, backend = select_renderers(pure=pure)
    files: dict[str, str] = {}
    files["models.ts"] = render_models(_declarations(api), 0).decode("utf-8")
    client_payload = (tuple(_referenced_names(api)), _operation_tuples(api))
    files["client.ts"] = render_client(client_payload, 0).decode("utf-8")
    files["index.ts"] = _index_module(react_query, base_url_env)
    if react_query:
        files["react-query.ts"] = _react_query_module(api)
    files[MANIFEST_NAME] = _manifest(list(files.keys()), api, backend)
    return files


__all__ = ["MANIFEST_NAME", "render_typescript"]
