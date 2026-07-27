# Native C internationalization plan — Stage 2: delivery and proof

## Status

Proposed implementation and evidence sequence for `native-c-i18n-stage-1.md`. No runtime implementation is included.

Each phase is independently testable. Native acceleration is accepted only from repeated whole-request measurements that clear the measured A/A noise floor. The goal is tight integration with Wreath's native validation, templates, JSON serialization, server request state, typegen, and Flight Recorder—not an isolated translation microbenchmark.

## Implementation phases

### Phase 0 — contract, baselines, and pure compiler

**Exact scope**

Define catalog/message/locale models, restricted syntax, deterministic IDs/image, fallback rules, pure renderer, pinned CLDR plural generator, and baseline workloads. Inventory every framework and validation message intended for localization.

Record current native validation throughput, allocation, output shape, and Python/native crossings before changing error representation.

**Native and Python surfaces**

- Add `src/wreath/i18n.py`, `_pure/i18n.py`, `_i18n_compile.py`, and generated CLDR data tooling.
- Add frozen catalog/type records but no C hot-path behavior.
- Add benchmark scenarios without changing production request handling.

**Dependencies:** none.

**Tests required**

- Deterministic image/hash across processes.
- Catalog signature matching and strict/non-strict missing translations.
- BCP 47 tag validation and fallback-cycle rejection.
- Restricted message syntax, Unicode, nesting, and source/output limits.
- CLDR cardinal and ordinal published vectors.
- Pure rendering and argument errors.
- Default English source covers every selected framework/validation message exactly.

**Benchmarks required**

- Current valid native validation.
- One and many invalid fields.
- Nested dataclass/list errors.
- Current templates and problem JSON.
- Pure/native/Pydantic arms only where Pydantic already exists as a benchmark dependency.
- Retain environment, build hash, raw repetitions, A/A floor, allocations, cycles/instructions/cache misses, throughput, p50/p99/p999, and crossings.

**Completion criteria**

One executable pure specification, one deterministic catalog image, and retained before-data. No public request behavior changes.

**Deliberately deferred**

Native matcher/renderer, validation integration, template opcode, framework localization, browser bundles.

### Phase 1 — native catalog, message VM, and locale negotiation

**Exact scope**

Add `_core/i18n.c`, native Catalog/Message types, compiled image loading, plural/select VM, exact-size standalone rendering, and bounded RFC 4647 negotiation. Extend the guarded `WreathCoreCAPI` for raw server negotiation.

**Native and Python surfaces**

- `setup.py` `_core` source list.
- `_native/i18n.c`, `i18n.h`, `wreathcore.h`, `_coremodule.c`.
- `wreath.i18n` native/pure facade.
- Pure/native differential harness.
- Native request context receives locale ID only after the C API is proven.

**Dependencies:** Phase 0.

**Tests required**

- Byte-for-byte pure/native rendering parity.
- RFC 4647 exact, truncated, wildcard, `q=0`, equal-q order, malformed, and bounded-range corpus.
- Every generated CLDR rule vector.
- Catalog ABI/hash/version rejection.
- Argument mismatch, missing key, fallback, output overflow, and maximum nesting.
- Free-threaded concurrent catalog reads and interpreter-lifetime behavior.
- ASan/UBSan and native lints.

**Benchmarks and effect proof**

- Negotiation microbenchmark only for diagnosis.
- Negotiation inside a complete Wreath-native request.
- Message ID lookup versus string key.
- Static, interpolated, plural, and nested-select messages.
- One exact allocation for standalone bytes.
- Measure instructions, cache misses, allocation count, CPU/request, throughput, and tail latency.

**Completion criteria**

The native server can select `locale_id` from raw header bytes without an ASGI scope, header dictionary, Python call, lock, or request-time catalog parsing. Native selection/rendering matches pure behavior.

Do not select native by default solely from a lookup microbenchmark; complete localized response gain must clear A/A noise.

**Deliberately deferred**

Validation descriptors, templates, framework responses, typegen bundles.

### Phase 2 — native validation and localized direct 422

**Exact scope**

Replace English display strings in compiled validation plans with stable numeric error code/message/context descriptors. Make `validate.c` accumulate compact issues and directly serialize localized 422 problem JSON through native JSON/i18n writers when no custom handler requires Python errors.

Preserve recursive-dataclass pure fallback and custom `ValidationError` handlers.

**Native and Python surfaces**

- `binding._compile_plan`, `_error`, `ValidationError`, `compile_binder`, missing query/header/cookie/form/file checks.
- `_native/validate.c`, `i18n.c`, `json.c` and shared writer APIs.
- Endpoint-plan metadata indicating direct response versus custom-handler materialization.
- Pure validation output.
- Typegen's future stable validation contract scaffold.

**Dependencies:** Phases 0–1.

**Tests required**

- Every existing validation annotation/constraint and error path.
- Default English `msg` compatibility where public.
- Stable `code`, `type`, `loc`, typed `ctx`, and localized `msg`.
- One/many/nested issues and configured truncation.
- Range/pattern/context values.
- Default and non-default locale.
- Direct native response equals materialized Python response semantically and byte-for-byte where canonical JSON order is defined.
- Custom exception handlers receive `ValidationError.errors` in the documented shape.
- Recursive dataclass fallback.
- Cancellation/error cleanup and no issue-path leaks.

**Benchmarks and effect proof**

Run valid and invalid bodies separately:

- valid scalar/dataclass/list body;
- one scalar issue;
- many independent fields;
- nested path issues;
- range/pattern context;
- default and non-default locale;
- direct C 422 and custom-handler materialization.

Compare:

```text
pre-i18n native validation/default English
integrated native validation/default locale
integrated native validation/non-default locale
pure validator
benchmark-only Pydantic equivalent
```

Measure allocations, bytes allocated, crossings, cycles, throughput, and p99/p999. The direct localized path must add no Python/native crossing and must preserve a demonstrated native-validation advantage.

**Completion criteria**

No post-validation Python translation traversal exists on the optimized path. Validator classification, issue storage, locale message selection, and final 422 serialization remain in C. Valid requests do not regress outside noise.

**Deliberately deferred**

Template integration and all framework messages beyond validation.

### Phase 3 — native template message opcode

**Exact scope**

Add `{% trans "key" ... %}` compilation and `OP_MESSAGE` execution. Append directly into the existing native template output builder without an intermediate translation object.

**Native and Python surfaces**

- `_pure/templates.py` parser/compiler/renderer.
- `templates.py` catalog association.
- `_native/templates.c` and internal i18n writer.
- Template fixtures and docs.

**Dependencies:** Phase 1; Phase 2 writer API where reusable.

**Tests required**

- Compile-time literal key and exact argument signature validation.
- Variables loaded through existing template path semantics.
- Pure/native byte parity.
- HTML escaping of translator text and arguments.
- Message opcode inside loops, conditionals, and includes.
- Missing/fallback locale, plural/select, nesting, and output-limit errors.
- Translator text cannot create `Markup` or raw tags.

**Benchmarks and effect proof**

- Small localized template.
- Loop/table template with repeated message handle.
- Native direct append versus Python preformatting and interpolation.
- Allocation proof that no intermediate bytes exists in native template mode.
- Whole-request throughput/latency for default and non-default locale.
- Ordinary templates without `OP_MESSAGE` remain below noise against baseline.

**Completion criteria**

A localized HTML response executes one compiled template/message pipeline in C and preserves safe escaping.

**Deliberately deferred**

Rich translated HTML, client components, date/number formatters.

### Phase 4 — framework messages, cache semantics, and route policy

**Exact scope**

Compile locale policy/message IDs into app/router/route/endpoint plans. Localize framework-owned static problems, auth/authz defaults, CSRF, 404/405, and configured limits. Emit correct `Content-Language` and variance/private-cache behavior.

**Native and Python surfaces**

- `app.py`, `request.py`, `response.py`, exceptions, auth and CSRF seams.
- `_native/server_request.c`, `server.h`, core C API.
- Existing web-policy `append_vary`.
- Prepared/static response cache indexed by locale ID.
- Route compilation metadata.

**Dependencies:** Phases 1–3 as applicable.

**Tests required**

- Locale precedence for early, public, protected, and authenticated paths.
- Identity claim validation/replacement and invalid claim fallback.
- Cookie/header/default behavior.
- `Content-Language`, `Vary: Accept-Language`, private cache behavior for user selectors, and no duplicate Vary tokens.
- Localized 404/405/auth/CSRF/limit responses.
- Custom application messages and logs remain unchanged.
- Misses still skip authentication/binding.
- Global finalizers unwind exactly once.
- Generic ASGI and native server parity.

**Benchmarks and effect proof**

- Localized 404, auth denial, CSRF denial, and static problem response.
- I18n disabled, default locale, and non-default locale.
- `wreath-request-trace` proves no new Python crossing on native paths.
- Disabled/default operation is below noise relative to pre-i18n baseline.

**Completion criteria**

Framework-owned client messages are localized without middleware or request-time policy introspection. Cache headers reflect the actual locale selector.

**Deliberately deferred**

Runtime catalog swaps and arbitrary Python locale callbacks on the fast path.

### Phase 5 — typegen and browser catalogs

**Exact scope**

Extend `ApiModel`, TypeScript output, manifest, and consumer fixtures with locale/message/signature/catalog-hash metadata, typed `t()`, visibility filtering, stable validation code/context types, and optional per-locale bundles.

**Native and Python surfaces**

- `typegen/model.py`, `inspect.py`, TypeScript target/renderers.
- Generated manifest and consumer package.
- Catalog export tooling.
- Optional versioned OpenAPI extension only if it serves consumers.

**Dependencies:** Phase 0 contracts and stable Phase 2 error model.

**Tests required**

- Valid generated client compiles.
- Unknown message key fails TypeScript.
- Missing, extra, and wrong-type arguments fail.
- Server-only messages are excluded.
- Locale union, bundles, and catalog hash match the server image.
- Validation error code/context types compile.
- Server pure/native and client renderer share plural/select golden vectors.
- Existing non-i18n typegen output remains stable unless deliberately versioned.

**Effect proof**

Measure generation time, manifest/catalog size, bundle splitting, and consumer compilation. Do not describe type generation as request-throughput optimization.

**Completion criteria**

One catalog declaration provides typed server and browser contracts without duplicating message signatures or parsing English validation messages.

**Deliberately deferred**

Translation management service, editor UI, remote catalogs.

### Phase 6 — advanced formatter decision gate

**Exact scope**

Measure demand and separately decide localized numbers, dates, times, currencies, units, list formatting, and full MessageFormat 2 compatibility.

**Dependencies:** stable v1 catalog/tape.

**Proof required before implementation**

- Concrete application requirements and expected locales.
- Exact CLDR data/RSS/startup impact.
- Timezone, currency, rounding, and numbering-system semantics.
- Pure reference and cross-language golden corpus.
- Native benefit versus Python/client formatting.
- Audit displays preserve immutable UTC instants and permanent sortable UTC/offset presentation.

**Completion criteria**

Only explicitly supported formatters ship. V1 is never relabelled full CLDR/MF2 without conformance evidence.

**Deliberately deferred**

Collation/search, transliteration, timezone database ownership, arbitrary formatter extensions.

## Benchmark matrix

### Workloads

- locale negotiation: exact, regional fallback, wildcard, malformed, maximum ranges;
- static message;
- integer/string arguments;
- cardinal/ordinal plural;
- nested select/plural;
- small and maximum-bounded output;
- valid native validation;
- one/many/nested invalid issues;
- localized problem JSON;
- small and loop/table templates;
- 404, auth denial, CSRF denial;
- generic ASGI and Wreath native server;
- default locale, non-default locale, and disabled i18n.

### Required measurements

Retain repeated interleaved trials and A/A noise with environment metadata. Measure:

- cycles, instructions, branches, cache misses;
- allocations and bytes allocated;
- Python/native crossings;
- throughput and CPU/request;
- p50/p99/p999;
- catalog image/RSS and startup compile time;
- semantic output accuracy;
- fallback/malformed/missing/limit counters;
- typegen time and generated bundle size.

Critical whole-request comparisons:

```text
existing native validation/default English
vs integrated native validation/default locale
vs integrated native validation/non-default locale
vs Python-materialized/custom-handler path
```

Never use message-lookup timing alone as an end-to-end claim. Follow `src/wreath/_devtools/measure.py`: establish noise, interleave arms, ablate whole-request components, and do not use cProfile to decide.

## Correctness and security rules

- Locale is presentation data, not authorization, tenancy, routing, or partitioning input.
- Identity locale claims are validated against compiled locales.
- Arguments are typed and never interpreted as templates, HTML, headers, SQL, or format strings.
- Translator text is HTML-escaped and JSON-encoded at the owning serializer.
- Catalog limits prevent memory/output amplification.
- Default English remains stable unless a versioned public change is approved.
- Logs, audit event types, trace names, metric schemas, and exception classes stay untranslated.
- Audit timestamps remain UTC instants; locale formatting is presentation only, and audit views retain sortable permanent UTC/offset display.
- Cache headers represent the selector that changed content.
- Locale is not a metric label by default.
- Native, pure, and client renderers share golden vectors.
- Native optimization requires repeated whole-request evidence above noise.
- Any request-boundary growth is measured and justified.
- `wreath-native-lint`, sanitizers, fuzzing, Ruff, ty, pytest, request-boundary checks, and strict docs are run as applicable.

## Expected files

### Add

```text
src/wreath/i18n.py
src/wreath/_pure/i18n.py
src/wreath/_i18n_compile.py
src/wreath/_i18n_cldr.py
src/wreath/_native/i18n.c
src/wreath/_native/i18n.h

tests/i18n/
benchmarks/bench_i18n.py
tools/generate_cldr_plural_rules.py
```

### Change

```text
setup.py
src/wreath/_native/wreathcore.h
src/wreath/_native/_coremodule.c
src/wreath/_native/validate.c
src/wreath/_native/templates.c
src/wreath/_native/json.c
src/wreath/_native/server_request.c
src/wreath/_native/server.h
src/wreath/binding.py
src/wreath/templates.py
src/wreath/_pure/templates.py
src/wreath/request.py
src/wreath/response.py
src/wreath/app.py
src/wreath/exceptions.py
src/wreath/middleware/csrf.py
src/wreath/typegen/model.py
src/wreath/typegen/inspect.py
src/wreath/typegen/targets/typescript.py
tests/typegen/
benchmarks/README.md
repo-map.md
docs/agents/manifest.json
mkdocs.yml
docs/llms.txt
```

When `wreath.i18n` becomes public, follow `docs/cookbook/agents/documenting-a-module.md`: reference page, guide, recipes, nav, and agent-map updates.

## Risks and stop conditions

- Lookup alone may be too cheap to justify C. Value must come from native validation, templates, and direct response serialization.
- Validation representation can break clients parsing English `msg`. Preserve `msg`, add stable `code/ctx`, document transition, and test exact default English.
- Full CLDR/MF2 is much larger than v1. Keep the tape bounded.
- Locale caching is easy to get wrong. Fall back to private/no-store when selector variance cannot be represented safely.
- A Python translation pass would erase invalid-request advantages; reject it as the default.
- Large catalogs may harm startup, RSS, and cache locality. Measure layout and deduplication.
- If whole-request gains do not clear noise, retain the pure API/compiled metadata but do not select C merely because a microbenchmark is faster.
- Extending `WreathCoreCAPI` without version/size guards risks sibling-extension memory errors; guard before adding fields.

## Explicit non-goals

- Mandatory ICU/gettext/Babel or another runtime dependency.
- Translating logs, telemetry schema, exception classes, SQL, or protocol errors.
- Trusted translator-supplied HTML.
- Arbitrary Python formatter functions on the native request path.
- Locale as authorization/tenancy/routing/database-partition input.
- Full date/number/currency/collation support in v1.
- Claiming complete MessageFormat 2 conformance.
- Runtime translation administration, database catalogs, or remote fetching.
- Replacing typegen with a separate i18n generator.

## Standards references

- [RFC 4647: Matching of Language Tags](https://datatracker.ietf.org/doc/html/rfc4647)
- [Unicode CLDR plural rules](https://cldr.unicode.org/index/cldr-spec/plural-rules)
- [Unicode CLDR 47 and stable MessageFormat 2.0](http://blog.unicode.org/2025/03/unicode-cldr-47-release-messageformat-2.html)
