# Native C internationalization plan — Stage 1: architecture and contracts

## Status

Proposed implementation plan. No API or compatibility promise exists until an implementation issue and ADR approve the public surface.

This plan uses Wreath's current name. It assumes typegen is established and the Native Flight Recorder is being developed separately. Stage 2 defines implementation phases, proof requirements, files, risks, and non-goals.

## Goal

Add dependency-free `wreath.i18n` support that compiles locale catalogs, language negotiation, message signatures, plural/select rules, validation-error messages, template message operations, and client type information at startup, then executes the request-time path in native C.

The integrated path should be:

```text
native request headers / identity locale
    -> uint16 locale_id
    -> native JSON decode and validation plan
    -> stable validation code + typed context
    -> uint32 message_id
    -> compiled plural/select message tape
    -> native template / problem JSON / response output
```

Do not build Python translation middleware around a fast C validator. Locale selection, validation issue production, message selection, and response rendering should remain C-owned wherever the existing request stage is already native.

## Repository constraints and reusable mechanisms

Wreath already uses the right architecture:

- `binding._compile_plan()` converts annotations into normalized immutable tuples once.
- `_native/validate.c` executes a whole body plan in one C call and currently emits `{loc, msg, type}` errors matching the pure implementation.
- `_pure/templates.compile_tape()` parses templates once into a flat opcode tape.
- `_native/templates.c` executes that tape and emits byte-identical UTF-8.
- typegen builds one frozen canonical `ApiModel` and renders OpenAPI and TypeScript consumers from it.
- routing, auth clauses, serializers, and other hot paths compile Python declarations into native-friendly constants.

I18n must extend these plans and tapes rather than introduce dynamic request-time dictionaries, gettext lookups, Python callbacks, or a second metadata graph.

### Native extension boundary

Put the native catalog, locale matcher, plural VM, and message renderer in `wreath._native._core`:

```text
src/wreath/_native/i18n.c
src/wreath/_native/i18n.h
```

Add `i18n.c` to `_core` in `setup.py`. `validate.c` and `templates.c` can then call it directly with no capsule lookup or Python/native crossing.

Extend the existing `WreathCoreCAPI` capsule in `wreathcore.h` so `_server` can negotiate locale from raw request-header bytes and store only an integer ID. Add a C API version/size guard before extending the struct; sibling extensions must reject an incompatible API rather than read beyond an older layout.

Do not create a separate `_i18n` extension unless build/link measurements prove `_core` ownership is harmful. Separation would make native validation/template integration more expensive.

### Pure reference

Add `src/wreath/_pure/i18n.py` as the semantic oracle. Native and pure implementations must match:

- selected locale and fallback reason;
- message bytes and Unicode;
- plural/select category;
- missing argument/key behavior;
- validation codes, context, and default English message;
- limits;
- response headers and cache behavior.

Representation may differ; behavior may not.

### Dependencies

Do not add mandatory ICU, gettext, Babel, Fluent, or MessageFormat runtimes. Python compiles catalogs; C executes a small Wreath-owned tape.

Pin a Unicode CLDR release for plural rules and generated vectors. Commit compact generated rule data plus source/version/license metadata. Date, currency, timezone, and collation data are not part of v1.

## Public model

### Catalog construction

```python
from wreath.i18n import Catalog, LocalePolicy

catalog = Catalog.compile(
    default_locale="en",
    locales={
        "en": {
            "cart.items": {
                "args": {"count": "integer"},
                "message": "{count, plural, one {# item} other {# items}}",
                "visibility": "shared",
            },
            "validation.integer": "value is not an integer",
            "auth.forbidden": "You do not have permission.",
        },
        "mi-NZ": {
            "cart.items": {
                "args": {"count": "integer"},
                "message": "{count, plural, other {# tūemi}}",
                "visibility": "shared",
            },
            "validation.integer": "ehara te uara i te tau tōpū",
            "auth.forbidden": "Kāore ō whakaaetanga.",
        },
    },
    policy=LocalePolicy(
        identity_claim="locale",
        cookie="locale",
        accept_language=True,
    ),
)

app.configure_i18n(catalog)
```

Accept Python mappings and dependency-free JSON files. YAML is not core. PO/XLIFF import/export may become build-time tooling but must compile into the same model.

### Locale precedence

Compile one explicit policy:

1. route/application explicit locale;
2. configured authenticated identity claim;
3. configured locale cookie;
4. `Accept-Language`;
5. application default.

Early errors before authentication use header/cookie/default. Authentication may replace locale with one native resolution when a configured claim exists. Arbitrary Python callbacks are a labelled fallback, not the optimized path.

### Message API

```python
catalog.format("cart.items", locale="mi-NZ", count=3)

cart_items = catalog.message("cart.items")
cart_items.format(locale_id, count=3)
cart_items.format_bytes(locale_id, count=3)
```

`Message` stores numeric message ID and argument signature. Template, validation, framework, and endpoint plans store IDs rather than keys.

Application key lookup may hash a string per call. Pre-resolved handles avoid it on hot paths.

### Request access

```python
request.locale          # canonical tag, lazily materialized
request.locale_id       # valid for this catalog image
request.messages        # lightweight request-bound catalog view
```

The native request context stores `locale_id` and catalog generation. It does not allocate a locale string unless Python asks. Do not use generic `request.state` for locale ownership.

## Catalog source and compiled image

### Canonical records

```python
@dataclass(frozen=True, slots=True)
class MessageArgument:
    name: str
    type: Literal["string", "integer", "number", "boolean"]

@dataclass(frozen=True, slots=True)
class MessageDefinition:
    key: str
    arguments: tuple[MessageArgument, ...]
    visibility: Literal["server", "client", "shared"]

@dataclass(frozen=True, slots=True)
class LocaleDefinition:
    tag: str
    fallback: str | None
```

Date/time/currency/collation/unit/timezone formatting requires separate compiled formatter semantics and is omitted initially.

### Deterministic IDs

Assign:

- `uint16 locale_id` from sorted canonical tags;
- `uint32 message_id` from sorted keys;
- `uint16 argument_signature_id` from canonical typed tuples;
- `uint16 plural_rule_id` from deduplicated generated CLDR rules.

The image carries version and SHA-256. It must be byte-identical across processes for identical source. Never use `repr()`, addresses, or randomized hashes.

Reject at compilation:

- invalid/duplicate BCP 47 tag;
- fallback cycle or absent default;
- missing key or incompatible signature in strict mode;
- absent `other` case;
- undeclared argument or duplicate selector;
- unsupported formatter or excess nesting;
- catalog/output arithmetic overflow.

### Native image

```c
typedef struct {
    uint32_t abi_version;
    uint32_t image_version;
    uint32_t message_count;
    uint32_t signature_count;
    uint32_t tape_bytes;
    uint32_t text_bytes;
    uint16_t locale_count;
    uint16_t default_locale_id;
    uint16_t max_arguments;
    uint16_t max_select_depth;
    uint32_t max_output_bytes;
} WreathCatalogHeader;

typedef struct {
    uint32_t message_id;
    uint32_t tape_offset;
    uint32_t tape_length;
    uint16_t signature_id;
    uint16_t flags;
} WreathMessageEntry;
```

Per-locale entries are direct-indexed; a sentinel selects a pre-flattened fallback. Runtime never traverses locale strings. Text lives in measured immutable UTF-8 blobs; tapes reference offsets and lengths.

## Message language and native VM

Use a restricted Wreath message language informed by MessageFormat 2 and CLDR, without claiming full MF2 conformance.

V1 features:

- UTF-8 literal text;
- typed variables;
- exact-value and named `select`;
- CLDR cardinal and ordinal plural;
- plural `#`;
- bounded nested selection.

```text
TEXT offset length
ARG_STRING slot
ARG_INTEGER slot
ARG_NUMBER slot
ARG_BOOLEAN slot
SELECT slot case_table fallback
PLURAL_CARDINAL slot rule_id case_table fallback
PLURAL_ORDINAL slot rule_id case_table fallback
NUMBER_SIGN slot
JUMP target
END
```

Python parses and resolves jumps once. C never parses source syntax.

### Rendering

Standalone formatting:

1. Validate compiled signature against argument slots.
2. Execute plural/select decisions.
3. Calculate exact UTF-8 size with overflow/limit checks.
4. Allocate one final `bytes` object.
5. Execute into its final buffer.

Template/problem/validation output gets an internal writer API so messages append into the owning native builder without an intermediate Python object.

Do not use locale-sensitive `snprintf`. V1 integer `#` is deterministic ASCII. Localized number formatting is a later plan.

### Safety

- No Python expression/callable or request-time formatter registration.
- Bounded arguments, tape operations, nesting, locales, catalog bytes, and output.
- Every select/plural has a resolved fallback.
- Pure/native runtime type errors match.
- Translation text is display text, never trusted HTML, SQL, headers, logs, or format strings.

## Native language negotiation

Implement bounded RFC 4647 lookup and strict BCP 47 handling.

### Startup

- Canonicalize configured tags.
- Compile an open-addressed table or compact trie.
- Precompute exact/truncated/wildcard/default outcomes.
- Compile identity/cookie/header policy into application/route metadata.

### Request time

- Scan raw native headers without a header dictionary.
- Enforce header-byte and range limits.
- Parse q-values as integers `0..1000`.
- Preserve source order for equal q-values.
- Respect `q=0` and wildcard.
- Return locale ID, source, and fallback flags.

C API shape:

```c
int (*i18n_negotiate)(
    const WreathCatalogView *catalog,
    const uint8_t *value,
    Py_ssize_t value_len,
    uint16_t *locale_id,
    uint16_t *flags
);
```

The native server calls this at header completion. Generic ASGI uses a Python-callable facade over the same native/pure algorithm.

Add `Vary: Accept-Language` only when that header can change representation. Add `Content-Language` for localized output. Reuse existing `append_vary` behavior. Identity/cookie locale needs explicit private/shared-cache rules; do not blindly emit `Vary: Cookie`.

## Tight integration with native validation

This is a primary requirement, not a later adapter.

### Current issue

`_native/validate.c` builds Python dictionaries with English `msg` strings while walking its plan. Translating afterward would add a Python traversal, duplicate classification, allocate discarded English, and damage invalid-request performance.

### Revised plan descriptors

Validation plans carry numeric error descriptors:

```text
(error_code_id, message_id, context_signature_id)
```

Examples:

```text
VALIDATION_NULL        -> validation.null
VALIDATION_FLOAT       -> validation.number
VALIDATION_INT         -> validation.integer
VALIDATION_BOOL        -> validation.boolean
VALIDATION_STR         -> validation.string
VALIDATION_LIST        -> validation.list
VALIDATION_DICT        -> validation.object
VALIDATION_MISSING     -> validation.required
VALIDATION_UNION       -> validation.union
VALIDATION_RANGE_MIN   -> validation.minimum
VALIDATION_RANGE_MAX   -> validation.maximum
VALIDATION_PATTERN     -> validation.pattern
```

Developer-only unsupported annotations remain stable English errors.

### Native issue storage

```c
typedef struct {
    uint32_t code_id;
    uint32_t message_id;
    uint32_t path_offset;
    uint16_t path_length;
    uint16_t context_id;
    int64_t value_a;
    int64_t value_b;
} WreathValidationIssue;
```

The validator accumulates compact issues. At response time one native pass joins path, stable code/type, typed context, localized message tape, and final problem JSON.

Do not materialize Python error dictionaries unless application code catches `ValidationError` or a configured custom handler requires them.

### Public error shape

```json
{
  "loc": ["body", "count"],
  "type": "int",
  "code": "validation.integer",
  "ctx": {},
  "msg": "ehara te uara i te tau tōpū"
}
```

- `code`, `type`, `loc`, and `ctx` are stable machine data.
- `msg` is localized presentation.
- Default locale preserves current public English exactly.
- Pure validation matches.
- Typegen models code/context rather than encouraging `msg` parsing.

### Direct 422 fast path

Add an internal native validation response operation using `_native/json.c` builder techniques and i18n append calls. Preserve custom exception handlers: if one consumes `ValidationError`, materialize the Python exception/list.

Compile this choice into the endpoint plan; do not inspect handler registries per request.

Benchmark valid and invalid bodies separately: scalar, many errors, nested paths, contextual constraints, default/non-default locale, and custom-handler materialization. The localized path must not erase Wreath's native-validation advantage over Pydantic benchmark arms.

## Template integration

Add a message opcode:

```html
{% trans "cart.items" count=cart.count %}
```

```text
OP_MESSAGE message_id argument_path_table escape_mode
```

Compilation resolves the key and validates arguments. Runtime performs value lookup, typed loading, locale indexing, and native append.

Translation and inserted arguments are HTML-escaped. Trusted translated HTML is excluded from v1. A future rich-text format needs safe nodes, not `Markup` around translator-controlled text.

The C template engine calls i18n directly and appends into its builder without an intermediate bytes object.

## Framework response integration

Localize only client-facing Wreath messages:

- validation;
- framework `ProblemResponse` title/detail;
- 404/405 and limits;
- default auth/authz messages;
- CSRF failures;
- safe native server errors where appropriate.

Do not localize logs, trace/metric names, exception classes, developer configuration, SQL/protocol errors, or custom application messages unless catalog-backed.

Prebuild static framework responses once per locale and compile message IDs into app/route/endpoint metadata. Request time never looks up English source text.

## Typegen integration

Extend the canonical model:

```python
@dataclass(frozen=True, slots=True)
class MessageArgument:
    name: str
    type: Literal["string", "integer", "number", "boolean"]

@dataclass(frozen=True, slots=True)
class Message:
    key: str
    arguments: tuple[MessageArgument, ...]
    visibility: Literal["client", "shared"]

@dataclass(frozen=True, slots=True)
class ApiModel:
    ...
    locales: tuple[str, ...] = ()
    messages: tuple[Message, ...] = ()
    catalog_hash: str | None = None
```

Server-only messages stay out of browser output.

Generate TypeScript:

```typescript
export type Locale = "en" | "mi-NZ";

export interface MessageArgs {
  "auth.forbidden": Record<never, never>;
  "cart.items": { count: number };
}

export type MessageKey = keyof MessageArgs;

export function t<K extends MessageKey>(
  key: K,
  args: MessageArgs[K],
): string;
```

Optionally emit locale bundles and typed lazy loaders with catalog hash/version. Browser output may use a small generated JS interpreter or generated functions; it need not consume the C binary image. Server/client golden vectors must match.

Generate stable validation error code/context types. Clients may localize from `code + ctx` or display server `msg`; they never parse English.

## Flight Recorder integration

Keep both subsystems independently importable. Use numeric observation only:

- catalog image hash in static metadata;
- optional selected locale ID on completion records;
- counters for header/default/identity/cookie selection, fallback, malformed preference, missing translation, and output limits;
- locale is not a metric label by default;
- translated bodies and user arguments are not recorded by default.

The recorder explains locale by metadata join. I18n never calls Python or exporter hooks.

## Configuration and limits

```python
@dataclass(frozen=True, slots=True)
class I18nConfig:
    default_locale: str
    strict_catalogs: bool = True
    accept_language: bool = True
    identity_claim: str | None = None
    cookie: str | None = None
    max_header_bytes: int = 4096
    max_language_ranges: int = 16
    max_locales: int = 256
    max_messages: int = 65_536
    max_arguments: int = 16
    max_select_depth: int = 4
    max_output_bytes: int = 1_048_576
    max_validation_errors: int = 100
```

Compile application, included-router, and route policy during `Wreath._compile_routes()`. Route plans store IDs, not dictionaries.

Runtime catalog replacement is outside v1. A later atomic generation swap must keep old images alive until all requests complete and coordinate typegen hashes.
