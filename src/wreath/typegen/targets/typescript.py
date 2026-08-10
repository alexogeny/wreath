"""Plan an `ApiModel` into TypeScript output files.

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

from ..inspect import _pascal
from ..model import ApiModel, Operation, TypeRef
from ..render import select_renderers
from ..typescript_renderer import GENERATOR_HEADER, TYPEGEN_CONTRACT, ts_type

MANIFEST_NAME = "wreath-typegen.json"


def _camel(text: str) -> str:
    parts = [part for part in re.split(r"[_\-]", text) if part]
    if not parts:
        return text
    head = parts[0]
    return head[:1].lower() + head[1:] + "".join(
        part[:1].upper() + part[1:] for part in parts[1:]
    )


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


def permission_flag(action: str) -> str:
    """`"Llama::force_sync"` -> `"canForceSync"`.

    The verb only: a component destructuring `{ canEdit }` off a llama's
    permissions already knows what it is looking at, and `canLlamaEdit` reads
    like a stutter.
    """
    _resource, _separator, verb = action.rpartition("::")
    return "can" + _pascal(verb or action)


def _permissions_module(api: ApiModel) -> str:
    """A typed client for the app's own authorization vocabulary.

    The unions come from the routes, so asking about an action the API does not
    enforce is a compile error rather than a permanent `false` -- which is the
    failure mode of every hand-written copy of the rules.
    """

    lines = [GENERATOR_HEADER, "\n"]
    for entry in api.permissions:
        resource = _pascal(entry.resource_type)
        union = " | ".join(f'"{action}"' for action in entry.actions)
        flags = "\n".join(
            f"  {permission_flag(action)}: boolean;" for action in entry.actions
        )
        lines.append(
            f"export type {resource}Action = {union};\n\n"
            f"export interface {resource}Permissions {{\n{flags}\n}}\n\n"
        )
    names = [_pascal(entry.resource_type) for entry in api.permissions]
    union = " | ".join(f'"{entry.resource_type}"' for entry in api.permissions)
    lines.append(
        f"export type PermissionResource = {union};\n\n"
        "export interface PermissionMap {\n"
        + "".join(f"  {name}: {name}Permissions;\n" for name in names)
        + "}\n\n"
        "const ACTIONS: Record<PermissionResource, readonly string[]> = {\n"
        + "".join(
            f"  {entry.resource_type}: ["
            + ", ".join(f'"{action}"' for action in entry.actions)
            + "],\n"
            for entry in api.permissions
        )
        + "};\n\n"
        "function flags<R extends PermissionResource>(\n"
        "  resource: R,\n"
        "  allowed: readonly string[],\n"
        "): PermissionMap[R] {\n"
        "  const out: Record<string, boolean> = {};\n"
        "  for (const action of ACTIONS[resource]) {\n"
        '    const verb = action.split("::").pop() ?? action;\n'
        '    const name = "can" + verb.replace(/(^|_)([a-z])/g, (_m, _s, c) =>\n'
        "      c.toUpperCase());\n"
        "    out[name] = allowed.includes(action);\n"
        "  }\n"
        "  return out as PermissionMap[R];\n"
        "}\n\n"
        "/** Ask the server what this caller may do to these rows. One call. */\n"
        "export async function fetchPermissions<R extends PermissionResource>(\n"
        "  baseUrl: string,\n"
        "  resource: R,\n"
        "  ids: readonly string[],\n"
        "  init?: RequestInit,\n"
        "): Promise<Record<string, PermissionMap[R]>> {\n"
        "  const response = await fetch(`${baseUrl}/permissions`, {\n"
        '    ...init,\n    method: "POST",\n'
        '    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },\n'
        "    body: JSON.stringify({ type: resource, ids }),\n"
        "  });\n"
        "  if (!response.ok) throw new Error(`permissions: ${response.status}`);\n"
        "  const body = (await response.json()) as {\n"
        "    permissions: Record<string, string[]>;\n"
        "  };\n"
        "  const out: Record<string, PermissionMap[R]> = {};\n"
        "  for (const [id, allowed] of Object.entries(body.permissions)) {\n"
        "    out[id] = flags(resource, allowed);\n"
        "  }\n"
        "  return out;\n"
        "}\n"
    )
    return "".join(lines)


def _permissions_hook_module() -> str:
    """The React half: `usePermissions(resource, ids)` over the same call."""

    return (
        GENERATOR_HEADER
        + '\nimport { useQuery } from "@tanstack/react-query";\n'
        + 'import {\n  fetchPermissions,\n  type PermissionMap,\n'
        + "  type PermissionResource,\n} from \"./permissions\";\n\n"
        + "/**\n"
        + " * What the signed-in caller may do to these rows, from the same Cedar\n"
        + " * policies the server enforces. There is no second copy of the rules.\n"
        + " */\n"
        + "export function usePermissions<R extends PermissionResource>(\n"
        + "  baseUrl: string,\n"
        + "  resource: R,\n"
        + "  ids: readonly string[],\n"
        + ") {\n"
        + "  return useQuery({\n"
        + '    queryKey: ["wreath", "permissions", resource, [...ids].sort()],\n'
        + "    queryFn: () => fetchPermissions(baseUrl, resource, ids),\n"
        + "    enabled: ids.length > 0,\n"
        + "  });\n"
        + "}\n\n"
        + "/** The single-row form: `const { canEdit } = usePermission(url, \"Llama\", id);` */\n"
        + "export function usePermission<R extends PermissionResource>(\n"
        + "  baseUrl: string,\n"
        + "  resource: R,\n"
        + "  id: string,\n"
        + "): Partial<PermissionMap[R]> {\n"
        + "  const { data } = usePermissions(baseUrl, resource, [id]);\n"
        + "  return (data?.[id] ?? {}) as Partial<PermissionMap[R]>;\n"
        + "}\n"
    )


def _series_module(api: ApiModel) -> str:
    """Typed envelopes for the app's calculated views.

    The shared interfaces are generic over the measure names, so a component
    destructures `point.started` and the compiler knows whether it can be
    `null`. That is the whole return on measures being named: a positional
    measure arrives here as `value_0` and the component names it again by
    hand, which is a copy of the declaration that nothing checks.

    `values` stays parallel to `buckets` rather than becoming
    `{bucket, value}` pairs, because the two are the same length by
    construction -- the spine guarantees a dense run -- and a charting library
    wants the two arrays.
    """

    lines = [
        GENERATOR_HEADER,
        "\n/** An ISO-8601 instant. The start of a bucket, or when an event"
        " happened. */\n"
        "export type Instant = string;\n\n"
        "/** One plottable line: a stable identity, its unit, and its values.\n"
        " *\n"
        " * `key` is the grouping value and never a rank, so a line keeps its\n"
        " * identity when its neighbours come and go. `other` marks the folded\n"
        " * remainder, which also carries a null key -- that flag is what tells\n"
        " * it apart from a group whose value genuinely is null.\n"
        " */\n"
        "export interface SeriesData<V = number | null> {\n"
        "  measure: string;\n"
        "  key: string | number | null;\n"
        "  label: string;\n"
        "  unit: string | null;\n"
        "  kind: string;\n"
        "  values: readonly V[];\n"
        "  other: boolean;\n"
        "}\n\n"
        "/** A marker over the same range: its exact instant and its bucket. */\n"
        "export interface SeriesEvent {\n"
        "  at: Instant;\n"
        "  bucket: Instant;\n"
        "  label: string;\n"
        "}\n\n"
        "/** The prior period, with its own bucket run.\n"
        " *\n"
        " * February against March is 28 buckets against 31. Lining them up by\n"
        " * index is the renderer's decision, so both runs are given.\n"
        " */\n"
        "export interface SeriesComparison<S = SeriesData> {\n"
        "  previous: string;\n"
        "  buckets: readonly Instant[];\n"
        "  series: readonly S[];\n"
        "}\n\n"
        "export interface SeriesRange {\n"
        "  start: Instant;\n"
        "  end: Instant;\n"
        "}\n\n"
        "/** A dense run of buckets and one named series per measure per group. */\n"
        "export interface SeriesResult<S = SeriesData> {\n"
        "  range: SeriesRange;\n"
        "  zone: string;\n"
        "  bucket: string;\n"
        "  buckets: readonly Instant[];\n"
        "  series: readonly S[];\n"
        "  comparison: SeriesComparison<S> | null;\n"
        "  events: readonly SeriesEvent[];\n"
        "  /** Where the watermark fell, or null for a view that seals nothing.\n"
        "   * `corrections` names buckets whose settled value has a late\n"
        "   * adjustment folded in -- render them as provisional if you show it. */\n"
        "  sealed: SeriesSealed | null;\n"
        "  /** Which stored grain answered for which part of the range, oldest\n"
        "   * first. Empty for a view with no retention ladder. More than one\n"
        "   * entry means the line is drawn from two grains -- worth showing. */\n"
        "  segments: readonly SeriesSegment[];\n"
        "}\n\n"
        "export interface SeriesSealed {\n"
        "  through: Instant;\n"
        "  settled: readonly Instant[];\n"
        "  corrections: readonly Instant[];\n"
        "}\n\n"
        "export interface SeriesSegment {\n"
        "  start: Instant;\n"
        "  end: Instant;\n"
        "  /** `\"raw\"`, or the bucket name of the tier that served it. */\n"
        "  grain: string;\n"
        "}\n\n"
        "export interface AggregateRow<M extends string = string> {\n"
        "  key: string | number | null;\n"
        "  label: string;\n"
        "  values: Record<M, number | null>;\n"
        "}\n\n"
        "export interface AggregateResult<M extends string = string> {\n"
        "  rows: readonly AggregateRow<M>[];\n"
        "  measures: readonly M[];\n"
        "}\n\n",
    ]
    for shape in api.series:
        lines.append(_series_declaration(shape))
    return "".join(lines)


def _series_declaration(shape: Any) -> str:
    """The concrete types for one declaration."""
    base = _pascal(shape.name)
    names = " | ".join(f'"{measure.name}"' for measure in shape.measures)
    out = [f"/** Measures declared on `{shape.name}`. */\n"]
    out.append(f"export type {base}Measure = {names};\n\n")
    if shape.form == "aggregate":
        out.append(
            f"export type {base}Result = AggregateResult<{base}Measure>;\n\n"
        )
        return "".join(out)

    # A measure that fills has no nulls in its values; one that does not (an
    # average of no rows is undefined) does, and the component is made to say
    # what it draws in the gap.
    arms = []
    for measure in shape.measures:
        cell = "number" if measure.fills else "number | null"
        arms.append(
            f'  | (SeriesData<{cell}> & {{ measure: "{measure.name}" }})'
        )
    out.append(f"export type {base}Series =\n" + "\n".join(arms) + ";\n\n")
    detail = [f"bucket `{shape.bucket}`"]
    if shape.grouped:
        detail.append("grouped, so several lines per measure")
    if shape.compares:
        detail.append(f"compares against the previous {shape.compares}")
    if shape.events:
        detail.append("carries event markers")
    out.append(
        f"/** `{shape.name}`: {'; '.join(detail)}. */\n"
        f"export type {base}Result = SeriesResult<{base}Series>;\n\n"
    )
    return "".join(out)


def _index_module(
    react_query: bool,
    base_url_env: str | None,
    permissions: bool,
    series: bool = False,
) -> str:

    lines = [GENERATOR_HEADER, '\nexport * from "./models";\nexport * from "./client";\n']
    if series:
        lines.append('export * from "./series";\n')
    if permissions:
        lines.append('export * from "./permissions";\n')
    if react_query:
        lines.append('export * from "./react-query";\n')
        if permissions:
            lines.append('export * from "./use-permissions";\n')
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

    document = {
        "generator": "wreath-typegen",
        "contract": TYPEGEN_CONTRACT,
        "renderer": backend,
        "title": api.title,
        "version": api.version,
        "files": sorted(files),
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


#: How many times the generated runtime will re-send after a `Retry-After`.
#: Bounded, and visible in the emitted source, because a client that retries
#: without a ceiling turns one struggling origin into an outage: every caller
#: keeps a connection open and keeps re-arriving for as long as the server
#: keeps saying "later".
RETRY_CEILING = 3


def _behaviours_module(api: ApiModel) -> str:
    """The runtime for the behaviours the tape declared, and nothing else.

    Emitted only when at least one operation declares one. Dependency-free:
    `fetch`, `crypto.randomUUID` and `Map` are all platform, so the generated
    client adds no package to a consumer's lockfile.
    """
    entries = "\n".join(
        f"  {operation.id!r}: {list(operation.behaviours)!r},".replace("'", '"')
        for operation in api.operations
        if operation.behaviours
    )
    return f"""// Generated by wreath typegen. Do not edit.
//
// The server declared these behaviours through its middleware tape; this
// module is the client half. Every operation named below was documented as
// honouring the behaviour listed beside it, so acting on it is not a guess.

/** Behaviours declared per operation id, from the server's own contract. */
export const OPERATION_BEHAVIOURS: Record<string, readonly string[]> = {{
{entries}
}};

/** Re-sends permitted after a `Retry-After`. Bounded on purpose. */
export const RETRY_CEILING = {RETRY_CEILING};

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Cache of the last `ETag` seen per request key, for `If-None-Match`. */
const etags = new Map<string, string>();

function has(operationId: string, behaviour: string): boolean {{
  const declared = OPERATION_BEHAVIOURS[operationId];
  return declared !== undefined && declared.indexOf(behaviour) !== -1;
}}

function newKey(): string {{
  const c: any = (globalThis as any).crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  // No crypto.randomUUID (older runtimes): a time-and-random key is still
  // unique enough to make one client's retries share one key, which is all
  // an idempotency key has to do.
  return `${{Date.now().toString(16)}}-${{Math.random().toString(16).slice(2)}}`;
}}

/** Seconds to wait from a `Retry-After`, or null when it says nothing usable. */
export function retryDelay(response: Response): number | null {{
  const raw = response.headers.get("retry-after");
  if (raw === null) return null;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  const when = Date.parse(raw); // RFC 9110 permits an HTTP-date here.
  if (Number.isNaN(when)) return null;
  return Math.max(0, (when - Date.now()) / 1000);
}}

/**
 * Send one request, honouring whatever the server declared for this operation.
 *
 * `idempotency-key` -- a key is generated once and reused across this call's
 * retries, so a retry is the *same* request rather than a second one.
 * `retry-after`     -- a 429 or 503 is re-sent after the header says, at most
 *                      RETRY_CEILING times.
 * `etag`            -- the last ETag for this key is sent as `If-None-Match`,
 *                      and a 304 is returned as-is for the caller to treat as
 *                      a hit.
 */
export async function send(
  operationId: string,
  input: string,
  init: RequestInit = {{}},
): Promise<Response> {{
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers ?? {{}});
  const key = `${{method}} ${{input}}`;

  if (has(operationId, "idempotency-key") && UNSAFE.has(method)) {{
    if (!headers.has("idempotency-key")) headers.set("idempotency-key", newKey());
  }}
  if (has(operationId, "etag")) {{
    const seen = etags.get(key);
    if (seen !== undefined && !headers.has("if-none-match")) {{
      headers.set("if-none-match", seen);
    }}
  }}

  let attempt = 0;
  for (;;) {{
    const response = await fetch(input, {{ ...init, method, headers }});
    if (has(operationId, "etag")) {{
      const tag = response.headers.get("etag");
      if (tag !== null) etags.set(key, tag);
    }}
    const retryable = response.status === 429 || response.status === 503;
    if (!has(operationId, "retry-after") || !retryable || attempt >= RETRY_CEILING) {{
      return response;
    }}
    const delay = retryDelay(response);
    if (delay === null) return response;
    attempt += 1;
    await new Promise((resolve) => setTimeout(resolve, delay * 1000));
  }}
}}
"""


def render_typescript(
    api: ApiModel,
    *,
    react_query: bool = False,
    base_url_env: str | None = None,
    pure: bool = False,
) -> dict[str, str]:
    """Return `{filename: contents}` for the TypeScript target."""
    render_models, render_client, backend = select_renderers(pure=pure)
    files: dict[str, str] = {}
    files["models.ts"] = render_models(_declarations(api), 0).decode("utf-8")
    client_payload = (tuple(_referenced_names(api)), _operation_tuples(api))
    files["client.ts"] = render_client(client_payload, 0).decode("utf-8")
    files["index.ts"] = _index_module(
        react_query, base_url_env, bool(api.permissions), bool(api.series)
    )
    if react_query:
        files["react-query.ts"] = _react_query_module(api)
    if api.series:
        # Only when the application declares calculated views: an app with no
        # charts should not ship a module about them.
        files["series.ts"] = _series_module(api)
    if any(operation.behaviours for operation in api.operations):
        # Only when the tape declared something a client can act on. An app
        # with no such middleware should not ship a retry runtime.
        files["behaviours.ts"] = _behaviours_module(api)
    if api.permissions:
        # Only when the application actually declares policies: an app with no
        # authorization should not ship a module about it.
        files["permissions.ts"] = _permissions_module(api)
        if react_query:
            files["use-permissions.ts"] = _permissions_hook_module()
    files[MANIFEST_NAME] = _manifest(list(files.keys()), api, backend)
    return files


__all__ = ["MANIFEST_NAME", "permission_flag", "render_typescript"]
