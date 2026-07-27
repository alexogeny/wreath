# Native-augmented consumer type generation plan

Status: ready for test-first implementation

Related material:

- `AGENTS.md`
- `repo-map.md`
- `docs/agents/manifest.json`
- `src/neo/router.py`
- `src/neo/binding.py`
- `src/neo/openapi.py`
- `src/neo/_cli.py`
- `docs/internals/performance.md`
- `benchmarks/README.md`

## Goal

Generate deterministic, dependency-free client contracts from Neo's typed routes, initially targeting TypeScript models, a framework-neutral `fetch` client, and opt-in TanStack React Query hooks. Python remains responsible for inspecting annotations and defining wire semantics. An optional C renderer may accelerate large generated modules only after a retained decomposition proves rendering is a material cost; the pure-Python and native implementations must produce byte-identical output.

## Repository constraints

- Target CPython 3.14 and keep `src/neo` free of mandatory third-party runtime dependencies.
- Reuse `RouteDefinition`, `BindingSpec`, and `inspect_handler()` rather than introducing a second route/schema inspection system.
- Keep framework and server layers separate. Type generation is an offline application-introspection feature and must add no request-time work or Python/native crossings.
- Follow the existing optional-native pattern: a pure-Python implementation is always available, `NEO_PURE=1` remains meaningful, and the facade selects C only when built.
- Do not ask C to interpret arbitrary Python annotations. Python owns semantic analysis; C may render an already-normalized representation in one bounded call per output module.
- Measure before adding or claiming value from C. Importing the application and resolving type hints may dominate total command time.
- Generated React code may import consumer libraries, but Neo itself must not depend on React, TypeScript, Node, TanStack Query, or an external OpenAPI generator.
- Generated output must be deterministic, reviewable, and safe to check into a consumer repository.

## Existing implementation seams

Neo already has the front half of type generation:

- `src/neo/router.py` defines `RouteDefinition` with path, methods, endpoint, tags, summary, dependencies, middleware, and authorization requirements.
- `src/neo/binding.py` defines `BindingSpec`; `inspect_handler()` resolves path, query, header, cookie, form, file, body, dependency, connection, session, and return annotations.
- `src/neo/openapi.py` converts route definitions and binding specs into OpenAPI 3.1.
- `src/neo/_cli.py` owns the dependency-free `neo` command and application target loading conventions.
- `src/neo/_native/_coremodule.c` and `src/neo/_pure/` establish the native/pure parity model.

The current OpenAPI schema conversion is intentionally small. It handles primitive scalars, dataclasses, unions, lists/tuples, dictionaries, and recursive component slots, but it does not yet provide a stable consumer-generation contract. It also identifies components by unqualified `__name__` and has no route `operation_id`. Typegen must resolve those ambiguities rather than encoding them into generated clients.

## Architecture

Use one semantic model for OpenAPI and all consumer targets:

```text
Python annotations + RouteDefinition + BindingSpec
                         |
                         v
                canonical Typegen IR
                  frozen dataclasses
                         |
             +-----------+------------+
             |                        |
             v                        v
          OpenAPI              consumer targets
                                    |
                         +----------+----------+
                         |          |          |
                         v          v          v
                    TS models   fetch client  React Query
                         |
                         v
               pure or native renderer
```

Python builds and validates the canonical intermediate representation. Target planners convert it into normalized output records. A pure renderer always exists. The optional C renderer accepts the complete normalized module in one call and returns UTF-8 bytes.

Do not generate TypeScript directly from arbitrary OpenAPI dictionaries. OpenAPI remains an output of Neo's schema model, not the internal source of truth. This avoids duplicating nullable/optional rules and gives diagnostics access to the original Python symbols.

## Canonical typegen model

Add `src/neo/typegen/model.py` with frozen, slotted records. The exact internal representation can be refined test-first, but it should express at least:

```python
@dataclass(frozen=True, slots=True)
class TypeRef:
    kind: Literal[
        "unknown",
        "null",
        "boolean",
        "integer",
        "number",
        "string",
        "array",
        "tuple",
        "record",
        "union",
        "literal",
        "reference",
    ]
    name: str | None = None
    arguments: tuple["TypeRef", ...] = ()
    literals: tuple[str | int | float | bool | None, ...] = ()


@dataclass(frozen=True, slots=True)
class Field:
    wire_name: str
    type: TypeRef
    required: bool


@dataclass(frozen=True, slots=True)
class Model:
    name: str
    fields: tuple[Field, ...]


@dataclass(frozen=True, slots=True)
class Parameter:
    python_name: str
    wire_name: str
    location: Literal["path", "query", "header", "cookie"]
    type: TypeRef
    required: bool


@dataclass(frozen=True, slots=True)
class Operation:
    id: str
    method: str
    path: str
    parameters: tuple[Parameter, ...]
    request_body: TypeRef | None
    response_body: TypeRef
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApiModel:
    title: str
    version: str
    models: tuple[Model, ...]
    operations: tuple[Operation, ...]
```

Add `src/neo/typegen/inspect.py` to construct this model from `RouteDefinition` and `BindingSpec`. Keep source references in private diagnostic records so errors can name the route, handler, parameter, and unsupported annotation without putting Python objects into renderer input.

### Schema rules

The model builder must distinguish concepts that TypeScript consumers observe:

- required field versus optional field;
- nullable value versus missing value;
- free-length array versus fixed tuple;
- object with known fields versus string-keyed record;
- literal values and enums versus their scalar base types;
- named recursive reference versus inline anonymous shape;
- unknown schema versus an empty object.

Extend schema support with explicit tests for:

- `Literal`;
- enums;
- nullable unions;
- variadic and fixed tuples;
- nested and recursive dataclasses;
- dataclasses sharing the same `__name__` across modules;
- supported `bytes`, UUID, date, time, and datetime wire encodings, if and only if Neo's request/response codecs define them;
- unsupported and unresolved annotations.

Unsupported annotations should produce an actionable diagnostic and map to `unknown` only when generation is configured to allow unknowns. Strict generation should fail rather than silently emit `{}` or `any`.

### Model identity and naming

Track Python model identity separately from its generated name. A registry should claim a generated name before descending into fields so recursive types terminate.

If two types request the same public name:

1. use an explicit schema name when Neo later exposes one;
2. otherwise derive a deterministic qualified alias;
3. fail if the alias still collides.

Never merge components merely because two classes share `__name__`.

## Route contract changes

Add an optional client-facing operation identifier to `RouteDefinition`, `Router.route()`, `Neo.route()`, and the convenience decorators:

```python
@app.get(
    "/widgets/{widget_id}",
    operation_id="getWidget",
    tags=("widgets",),
)
async def get_widget(...) -> Widget:
    ...
```

Rules:

- Preserve an explicit `operation_id` unchanged after validating it can be mapped to target identifiers.
- Otherwise derive a deterministic ID from method and path, such as `getWidgetsByWidgetId`.
- Do not rely only on `handler.__name__`; included routers and reused handlers can collide.
- Reject duplicate operation IDs during schema compilation with both conflicting routes in the error.
- Emit the same ID into OpenAPI.
- Preserve operation IDs when routers are included with prefixes and inherited metadata.

Do not expand this change into a complete response declaration API. Response status, content type, declared error responses, schema exclusion, and deprecation metadata are useful follow-up route contracts, but the first target can document its current assumptions: successful typed return is the operation result and unexpected non-2xx responses use a generic client error.

## OpenAPI integration

Refactor `src/neo/openapi.py` to consume the canonical model rather than independently walking annotations. Preserve its public `generate_openapi(app, *, title, version)` API and existing documents while adding operation IDs and the newly supported schema forms.

Parity tests must prove that request parameter locations, aliases, required/default state, request bodies, response bodies, recursive references, tags, and summaries still match binding behavior.

`enable_docs()` remains opt-in and unrelated to type generation. The CLI may construct the canonical model without registering documentation routes.

## TypeScript output

Add `src/neo/typegen/targets/typescript.py`. The target planner should turn `ApiModel` into target-neutral normalized declarations and operations before handing them to a renderer.

Generate this initial layout:

```text
generated/
  models.ts
  client.ts
  index.ts
  react-query.ts       # only when requested
  neo-typegen.json     # owned-file manifest and generator metadata
```

### Models

Representative output:

```typescript
export interface Widget {
  name: string;
  weight: number;
  labels?: string[];
}

export interface GetWidgetParameters {
  widgetId: number;
  expand?: boolean;
}
```

Rendering rules:

- Python `str`, `int`/`float`, `bool`, and `None` map to `string`, `number`, `boolean`, and `null`.
- Unknown schemas map to `unknown`, never `any`.
- Missing and nullable remain distinct: `value?: string` is not interchangeable with `value: string | null`.
- Unsafe property names are quoted as string literals.
- Unions and recursive references are parenthesized consistently.
- Declarations and imports have deterministic ordering.
- Generated files include a stable do-not-edit header and generator version, but no timestamp or machine-specific path.

### Fetch client

Generate a framework-neutral client built on an injectable Fetch API:

```typescript
export interface NeoClientOptions {
  baseUrl: string;
  fetch?: typeof globalThis.fetch;
  headers?: HeadersInit | (() => HeadersInit | Promise<HeadersInit>);
}

export function createNeoClient(options: NeoClientOptions) {
  return {
    async getWidget(
      parameters: GetWidgetParameters,
      init?: RequestInit,
    ): Promise<Widget> {
      // generated transport
    },
  };
}
```

Transport correctness rules:

- Encode each substituted path value with `encodeURIComponent`.
- Omit undefined query values.
- Match Neo's actual repeated/list query encoding rather than choosing a client-only convention.
- Serialize request bodies with the content type represented by the operation.
- Merge generated, configured, and per-request headers predictably without embedding credentials.
- Allow configured headers to be asynchronous so consumers can refresh tokens.
- Use an injected `fetch` implementation when supplied, otherwise `globalThis.fetch`.
- Preserve response status and headers in a generated `NeoApiError` for non-2xx responses.
- Attempt error-body decoding according to response content type without hiding decode failures.
- Keep React concepts out of `client.ts`.

Cookie parameters need an explicit portability rule. Browser code cannot freely set a `Cookie` header, so the first browser target should use `credentials` and document cookie parameters as server-managed rather than pretending they can always be serialized. Node-specific cookie-jar behavior is out of scope.

## React Query output

Add `src/neo/typegen/targets/react_query.py`, consuming the same operations and transport client. Generated source may import TanStack Query v5, but Neo does not install or import it.

Representative output:

```typescript
export const widgetKeys = {
  all: ["widgets"] as const,
  detail: (parameters: GetWidgetParameters) =>
    ["widgets", "detail", parameters] as const,
};

export function useGetWidget(
  parameters: GetWidgetParameters,
  options?: Omit<
    UseQueryOptions<Widget, NeoApiError>,
    "queryKey" | "queryFn"
  >,
) {
  const client = useNeoClient();
  return useQuery({
    ...options,
    queryKey: widgetKeys.detail(parameters),
    queryFn: () => client.getWidget(parameters),
  });
}
```

Rules:

- `GET` and `HEAD` operations generate queries.
- Mutating methods generate mutations.
- Query keys are deterministic and contain parameters but never configured authentication state.
- A small generated provider/context supplies the transport client.
- User options cannot replace the generated query key or function accidentally.
- Do not infer cache invalidation from path spelling. Automatic invalidation waits for explicit resource metadata.
- React generation is opt-in so TypeScript-only consumers receive no React imports.

## CLI contract

Extend `src/neo/_cli.py` with a `typegen` command using the existing module/attribute target conventions:

```console
neo typegen package.app:app \
  --target typescript \
  --output frontend/src/api/generated

neo typegen package.app:create_app \
  --factory \
  --target typescript \
  --react-query \
  --output frontend/src/api/generated
```

Initial options:

```text
--target typescript
--react-query
--output PATH
--base-url-env VITE_API_URL
--check
--strict / --allow-unknown
--pure
--factory
```

Behavior:

- Import the application without running ASGI lifespan handlers.
- Reuse or extract the current target splitting/import logic rather than adding a second import syntax.
- Require `--factory` before invoking a target callable; do not guess whether an object is an app factory.
- Render all files in memory before mutating the output directory.
- Write through temporary files and replace generated files only after the complete generation succeeds.
- Own and remove only paths listed in `neo-typegen.json`; never delete arbitrary consumer files.
- Reject output paths escaping the selected output directory.
- `--check` performs no writes and exits nonzero when generated bytes differ or owned files are missing/stale.
- `--pure` forces the parity implementation even when the native module is installed.
- Diagnostics include the route, operation, model/field or parameter, and unsupported source annotation.

## Pure renderer

Add `src/neo/_pure/typegen.py` as the reference renderer. It should accept normalized immutable tuples rather than the semantic dataclasses themselves, making the C boundary explicit and keeping parity fixtures compact.

Suggested facade contract:

```python
def render_typescript_module(
    module_name: str,
    declarations: tuple[tuple[object, ...], ...],
    operations: tuple[tuple[object, ...], ...],
    flags: int,
) -> bytes:
    ...
```

The exact tuple schema should be private, versioned internally, validated before crossing into C, and documented adjacent to both implementations. Avoid a dictionary-heavy protocol whose hash iteration or optional keys can introduce nondeterminism.

Keep output assembly linear. Use a list/bytearray builder with one final join, not repeated string concatenation proportional to output size.

## Optional C renderer

Add `src/neo/_native/typegen.c` only after the pure implementation and baseline decomposition exist. Register it through:

```text
src/neo/_native/_coremodule.c
src/neo/_native/neocore.h
setup.py
```

The facade should select it consistently with existing accelerators:

```python
_render_typescript = (
    render_typescript_pure
    if _core is None
    else _core.render_typescript
)
```

Native boundary rules:

- One Python-to-C call per generated output module, not per route, declaration, or field.
- Accept only the normalized, prevalidated tuple model.
- Return one `bytes` object containing UTF-8 source.
- Pre-size or geometrically grow an owned output buffer; never use additive fixed-step growth.
- Check every CPython API result and preserve active exceptions.
- Keep all user-defined Python callbacks and annotation inspection out of C.
- Escape TypeScript strings and identifiers identically to the pure renderer.
- Preserve deterministic ordering supplied by Python; C does not sort semantic objects.
- Add no server or request-path dependency on the renderer.
- Keep `neo-native-lint`, error, memory, boundary, and GIL checks clean. Any waiver must be narrow and justified in place.

If decomposition shows rendering below noise or insignificant in total command time, retain the pure renderer and record the native path as not justified. The project should not carry C solely to label an offline feature native.

## Correctness work

Add focused Python tests and a realistic consumer fixture:

```text
tests/typegen/
  app.py
  expected/
    models.ts
    client.ts
    react-query.ts
    index.ts
  consumer/
    package.json
    lockfile
    tsconfig.json
    src/usage.tsx
```

Test-first requirements:

- [ ] Add red tests for operation IDs, duplicate detection, router-prefix preservation, and deterministic fallback IDs.
- [ ] Add red tests for required, optional, nullable, literal, enum, list, tuple, record, recursive, and colliding model shapes.
- [ ] Prove canonical model parameters match `BindingSpec` aliases, locations, defaults, and request-body selection.
- [ ] Preserve existing OpenAPI output while moving it to the canonical model; update expectations only for intentional additions such as `operationId`.
- [ ] Snapshot generated models, client, React Query hooks, index, and manifest.
- [ ] Run generation twice and prove byte-identical output and no second-run diff.
- [ ] Prove `--check` succeeds for current output and fails for stale, missing, and unexpected owned files without writing.
- [ ] Compile the consumer fixture under TypeScript strict mode.
- [ ] Compile React hooks against the supported TanStack Query major version.
- [ ] Use a mocked Fetch implementation to prove path escaping, query omission/repetition, header merging, body serialization, success decoding, and structured non-2xx errors.
- [ ] Prove no generated code embeds tokens, application filesystem paths, timestamps, or Python module locations.
- [ ] Prove pure/native byte parity for every golden and fuzzed normalized module.
- [ ] Fuzz string escaping, identifier normalization, nested type rendering, and malformed native input.
- [ ] Prove `NEO_PURE=1` and `--pure` select the reference renderer.
- [ ] Prove applications without the native extension generate the same files.

The consumer compiler is a development/CI tool, not a Neo runtime dependency. Pin its package manager lockfile and TanStack/TypeScript versions so generated-code validation is reproducible.

## Benchmark design

Add `benchmarks/bench_typegen.py` with synthetic Neo applications large enough to separate inspection from rendering:

| Shape | Routes | Models | Purpose |
| --- | ---: | ---: | --- |
| small | 10 | 10 | Typical command floor |
| medium | 100 | 100 | Shared model and operation naming cost |
| large | 1,000 | 500 | Renderer throughput and memory |
| stress | 10,000 | 2,000 or shared graph | Native value and scaling defects |

Include nested, shared, nullable, union, recursive, and colliding-name models. Keep the generated semantic content identical across pure/native trials.

Measure separately:

1. application import;
2. route and type-hint inspection;
3. canonical model construction and validation;
4. target planning/normalization;
5. pure rendering;
6. native rendering;
7. filesystem write;
8. total CLI-equivalent generation.

Record:

- warmup and repeated raw trials;
- median, p95, and measured A/A noise floor;
- route/model/field counts and generated byte counts;
- routes, models, and output MiB per second;
- peak RSS;
- pure/native SHA-256 values;
- Python version, platform, compiler flags, native module path, and `NEO_PURE` state;
- errors and unsupported-schema diagnostics.

Benchmark gates:

- Do not implement C before retaining the pure decomposition baseline.
- Do not claim a native win unless repeated renderer deltas clear the A/A noise floor.
- Report renderer-only and total-command deltas. A fast renderer is not a fast command if import and type inspection dominate.
- Rendering and memory should scale approximately linearly with output size; investigate superlinear growth before acceptance.
- Pure and native hashes must match before comparing timings.
- Keep raw artifacts under `benchmark-results-typegen/` and document the exact reproduction command in `benchmarks/README.md`.

## Implementation work

### Canonical contract and OpenAPI

- [ ] Add the frozen typegen model and model builder.
- [ ] Add route `operation_id` metadata and collision validation.
- [ ] Resolve model identity/name collisions and recursive references.
- [ ] Expand schema coverage required by the first TypeScript target.
- [ ] Refactor OpenAPI generation onto the canonical model without changing binding semantics.

### TypeScript proof point

- [ ] Add pure TypeScript model rendering.
- [ ] Add the injectable Fetch client and structured error type.
- [ ] Add deterministic generated-file ownership and atomic writes.
- [ ] Add `neo typegen`, `--check`, `--factory`, strict/unknown behavior, and `--pure`.
- [ ] Add golden, mocked-fetch, CLI, determinism, and strict consumer compilation tests.

### Measurement and native gate

- [ ] Add the decomposed typegen benchmark and retain repeated pure baselines.
- [ ] Determine whether rendering is material for medium, large, and stress applications.
- [ ] If justified, add the one-call C renderer and pure/native parity tests.
- [ ] Retain repeated after-results and report renderer and total-command evidence separately.
- [ ] If not justified, document the result and keep the pure path rather than carrying unproven C.

### React proof point

- [ ] Add opt-in TanStack React Query v5 planning and rendering.
- [ ] Add generated client provider, deterministic query keys, queries, and mutations.
- [ ] Compile a realistic `.tsx` usage fixture.
- [ ] Keep cache invalidation explicit and out of scope until routes expose resource metadata.

### Documentation and agent routing

- [ ] Add `docs/guides/typegen.md` with CLI, checked-in output, CI `--check`, browser authentication, and non-durability of generated assumptions.
- [ ] Update OpenAPI and application reference material for operation IDs and shared schema behavior.
- [ ] Update `repo-map.md` with typegen source, tests, benchmarks, and consumer fixtures.
- [ ] Update `docs/agents/manifest.json` so typegen changes route through binding, OpenAPI, CLI, native, and performance guidance.
- [ ] Update `benchmarks/README.md` with decomposition, native validity gates, and artifact locations.

## Correctness rules

- Request validation, OpenAPI, and generated clients derive from one canonical interpretation of annotations.
- Generated identifiers may change spelling, but wire names never change.
- Required, optional, and nullable are not interchangeable.
- Unknown types never silently become TypeScript `any`.
- Every generated operation ID and model name is unique and deterministic.
- Path, query, header, and body serialization match Neo's codecs.
- Browser cookie restrictions are represented honestly; generated code does not fake forbidden cookie headers.
- Authentication is injected at runtime and never included in generated files or query keys.
- Generation does not run application lifespan handlers unless a future explicit option defines that behavior.
- A failed generation leaves the previous complete generated tree intact.
- The manifest authorizes deletion only inside the selected output directory.
- Pure and native renderers are byte-for-byte equivalent.
- Typegen adds no request-time initialization, allocation, or Python/native crossings.

## Likely files touched

```text
src/neo/typegen/__init__.py
src/neo/typegen/model.py
src/neo/typegen/inspect.py
src/neo/typegen/targets/__init__.py
src/neo/typegen/targets/typescript.py
src/neo/typegen/targets/react_query.py
src/neo/_pure/typegen.py
src/neo/_native/typegen.c
src/neo/_native/_coremodule.c
src/neo/_native/neocore.h
src/neo/openapi.py
src/neo/router.py
src/neo/app.py
src/neo/_cli.py
setup.py
tests/test_typegen.py
tests/test_typegen_cli.py
tests/test_openapi.py
tests/typegen/
benchmarks/bench_typegen.py
benchmarks/README.md
docs/guides/typegen.md
docs/reference/application.md
docs/agents/manifest.json
repo-map.md
```

The native files are conditional on the benchmark gate; they are not required to deliver useful TypeScript generation.

## Out of scope

- Running TypeScript or React in Neo's Python runtime.
- Depending on Node, React, TanStack Query, or an OpenAPI generator at Neo runtime.
- Generating Angular, Vue, Svelte, Axios, Zod, or GraphQL targets before the shared TypeScript/fetch contract is stable.
- Runtime response validation solely for client generation.
- Inferring mutation cache invalidation from URL structure.
- Embedding authentication secrets or implementing a browser token store.
- Running application startup/shutdown during ordinary generation.
- C-based annotation inspection or per-field Python/C crossings.
- Treating generated clients as a compatibility guarantee without explicit API versioning policy.

## Acceptance checks

- `neo typegen package.app:app --target typescript --output ...` generates deterministic models, Fetch client, index, and ownership manifest.
- `--react-query` adds compiling TanStack Query v5 hooks without adding a Neo runtime dependency.
- `--check` detects stale generated output without modifying files.
- Explicit and fallback operation IDs are stable, unique, and present in OpenAPI and generated clients.
- OpenAPI and typegen use the same canonical schema interpretation as request binding.
- Strict generation rejects unsupported annotations with route- and field-specific diagnostics; allowed unknowns become `unknown`, never `any`.
- Generated path, query, header, and body handling passes mocked transport tests against Neo's wire conventions.
- Generated TypeScript and React fixtures compile in strict mode from a pinned consumer toolchain.
- Repeated generation produces byte-identical output and leaves no diff when inputs are unchanged.
- Failed generation cannot partially replace a previously valid output tree or delete unowned files.
- The pure generator works without native extensions and under `NEO_PURE=1`.
- If the C renderer is added, pure/native output hashes match across golden, randomized, and large-application cases.
- Retained benchmark results separate import, inspection, model construction, planning, rendering, writing, and total command cost.
- Any native performance claim clears the measured A/A noise floor and reports total-command impact.
- `neo-request-trace --check` remains unchanged because typegen adds no request-path boundary crossings.
- Focused tests, consumer compilation, default/full Python tests, Ruff, ty, native linters, and strict documentation build pass.
