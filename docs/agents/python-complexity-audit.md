# Adversarial Python complexity audit

**Date:** 2026-07-17  
**Scope:** `src/wreath/**/*.py`, prioritizing large Python-owned surfaces without a C implementation of the same orchestration  
**Method:** static adversarial review; every loop, recursive expansion, repeated slice, linear lookup, and cross-product was treated as quadratic until bounded or disproved  
**Status:** resolved with focused regressions; full default test suite passes as of 2026-07-19

## Resolution

- **QPY-001:** dependency compilation memoizes shared callables and uses O(1) active-path membership; covered by `tests/test_binding_complexity.py`.
- **QPY-002:** registry model names and `ModelSpec` relationship names are compiled into lookup indexes; covered by `tests/orm/test_complexity.py`.
- **QPY-003:** router inclusion stores immutable snapshot edges and flattens each final route once; covered by `tests/test_router_complexity.py`.
- **QPY-004:** default normalization peels parentheses with indices and slices once; covered by `tests/orm/test_complexity.py`.
- **QPY-005:** introspected foreign keys are indexed by paired local and remote positions, preserving linear validation and checking the referenced column; covered by `tests/orm/test_introspection.py`.

The findings below retain the original red-test reasoning and predicted failure modes as historical context.

## Confidence scale

- **0.95–1.00:** the asymptotic behavior follows directly from the source; measurement only establishes its practical crossover.
- **0.80–0.94:** the source establishes a product or repeated scan, but realistic data shape may bound one dimension.
- **Below 0.80:** hypothesis only; omitted from the findings and listed under cleared/watch items instead.

A “red test” below means a test that expresses the desired non-quadratic invariant and should fail on the current implementation. Prefer deterministic probe counts over wall-clock thresholds. Where a probe would require production instrumentation, the suggested microbenchmark uses repeated medians and a doubling ratio.

## Findings

### QPY-001 — Dependency compilation is quadratic for a chain and exponential for a shared DAG

**Confidence:** 0.99  
**Severity:** high for generated or plugin-built dependency graphs; startup/route-compilation only  
**Code:** `src/wreath/binding.py:434-476`, especially `fn in seen`, `(*seen, fn)`, and the uncached recursive call at line 446

`_compile_dependency()` recursively recompiles every occurrence of a dependency. It has no compile-time memo keyed by callable. A depth-`D` chain also copies and scans a lengthening `seen` tuple at each level, giving `1 + ... + D = O(D²)` ancestor work. A diamond DAG is worse: if each level names the same child twice, the compiler expands both paths and creates `2^D` resolver subtrees even though there are only `D` distinct callables. The request-time cache does not help compilation.

**Red tests:** monkeypatch `wreath.binding.inspect.signature` with a wrapper that increments a counter. Build callable objects whose synthetic signatures have two parameters pointing to the same next-level callable, then invoke `_compile_dependency(root, ())`.

```python
# tests/test_binding_complexity.py
# The helper can use inspect.Signature/inspect.Parameter and callable objects.
assert signature_calls(shared_binary_dag(depth=12)) <= 2 * 12
```

The DAG assertion currently sees approximately `2**13 - 1` calls and fails decisively. Signature calls alone cannot expose the chain's tuple-copy/tuple-membership term because there is only one signature call per level. Cover that term with a warmed repeated-median microbenchmark for depths 512, 1,024, and 2,048 and require the doubling ratio to stay below 2.6. A robust fix needs both memoized compiled resolvers and O(1) active-path cycle membership (while retaining an ordered path for the error).

---

### QPY-002 — String relationship resolution scans the complete ORM registry per relationship

**Confidence:** 0.98  
**Severity:** medium; ORM registry startup/compilation  
**Code:** `src/wreath/orm/registry.py:100-107`, `src/wreath/orm/registry.py:224-242`

For every string-target relationship, `_resolve_target()` comprehends over every `(model, spec)` in `self._specs` and compares `model.__name__`. With `M` models and `R` string relationships this is `O(MR)`; the common generated-schema shape `R = Θ(M)` is quadratic. The full list is built even when the first match is unique. This surface is Python-owned; native model storage does not replace registry compilation.

There is a second, smaller repeated-scan path at `src/wreath/orm/schema.py:102-106`: `_check_back_populates()` calls `target.relationship(name)`, which linearly scans the target’s relationships. Dense relationship models can therefore add another quadratic term.

**Red microbenchmark:** generate isolated model classes with one string-target relationship each, then time `Registry(database, models)` at 128, 256, and 512 models. Warm up, randomize size order, use at least 15 samples per size, and subtract a class-generation baseline measured without relationships.

```python
ratios = [median[256] / median[128], median[512] / median[256]]
assert max(ratios) < 2.6
```

Current source predicts ratios tending toward 4 once registry resolution dominates. A deterministic alternative is a private probe incremented for each `_specs` item visited and an assertion that doubling models and relationships does not quadruple probes. The likely repair is a precomputed `name -> unique spec | ambiguous` index plus `ModelSpec.by_relationship_name`.

---

### QPY-003 — Nested router composition eagerly recopies a triangular number of routes

**Confidence:** 0.98  
**Severity:** medium for generated modular APIs; declaration/startup path  
**Code:** `src/wreath/router.py:151-186`

`Router.include_router()` eagerly walks every child route, creates a replacement `RouteDefinition`, and concatenates metadata tuples. Building a nesting chain by repeatedly adding one route and including the previous router performs `1 + 2 + ... + N = O(N²)` route replacements and retains successively copied route lists. Prefix and middleware metadata are flattened at every level rather than once when attached to the application.

This is not the native route matcher: it is Python-only route composition before compilation. A single flat include is linear; the defect is exposed by legal nested composition.

**Red deterministic test:** monkeypatch `wreath.router.replace` with a counting wrapper and construct a chain of routers. Each new router registers one route and includes the previous router.

```python
calls_100 = replacement_calls(build_nested_router(100))
calls_200 = replacement_calls(build_nested_router(200))
assert calls_200 <= 2.2 * calls_100
```

Current behavior is approximately triangular, so the ratio approaches 4. Also record total routes to ensure the benchmark compares equivalent final APIs and is not accidentally generating an exponentially duplicated tree. A lazy include tree flattened once, or composition metadata carried separately until app compilation, would make this linear in final routes plus include edges.

---

### QPY-004 — PostgreSQL default normalization repeatedly slices a shrinking string

**Confidence:** 1.00  
**Severity:** low in normal schemas, but an unambiguous quadratic primitive on database-controlled introspection text  
**Code:** `src/wreath/orm/introspection.py:295-300`

`_normalize_default()` removes one outer parenthesis pair per iteration with `text[1:-1].strip()`. Python string slicing copies the remaining string. An input with `N` nested pairs copies `2N + (2N-2) + ...`, or `O(N²)` characters. The initial `split()`/join and final `lower()` are linear and do not change that result.

**Red microbenchmark:** benchmark `_normalize_default("(" * n + "0" + ")" * n)` at 4 KiB, 8 KiB, and 16 KiB nesting, after warm-up. Use repeated medians and run this test under the existing `performance` mark.

```python
assert median_16k / median_8k < 2.6
```

Current behavior should approach a 4x doubling ratio. A linear implementation can find the removable outer span with indices and slice once. It must preserve current semantics: the loop removes balanced-looking outer characters without actually validating parenthesis balance, and `.strip()` occurs after every removal.

---

### QPY-005 — ORM foreign-key introspection scans all database FKs for every declared FK column

**Confidence:** 0.96  
**Severity:** low-to-medium; startup schema validation, amplified on generated wide tables  
**Code:** `src/wreath/orm/introspection.py:210-268`, especially lines 249-259

`_validate_constraints()` stores foreign-key rows in a set, but then discards the set’s lookup advantage. For each declared reference column it builds `matches` by scanning every foreign tuple and comparing local name, schema, and table. For `C` declared FK columns and `F` actual foreign constraints this is `O(CF)`, quadratic when both scale with table width. The referenced column positions are not included in the predicate, so the scan also does not fully validate the target column.

**Red deterministic test:** add a private comparison probe around the foreign scan (the project already uses this pattern in `tests/orm/test_session.py:723-743`). Feed `_validate_constraints()` a fake async connection returning `N` declared/actual single-column foreign keys in adversarial set iteration order.

```python
assert probes_for(128) <= 4 * 128
assert probes_for(256) <= 4 * 256
```

Without production instrumentation, a repeated-median microbenchmark with synthetic `ModelSpec` and fake rows can use the same `<2.6` doubling-ratio gate. The repair is an index keyed by local column tuple (and ideally schema, table, and referenced positions), making declared-FK validation `O(C + F)`.

## Large Python surfaces reviewed but not convicted

These were specifically checked because they are large, request-adjacent, or lack a complete C equivalent:

- **`src/wreath/webhooks.py` (about 51 KiB):** replay expiry and eviction use a heap plus dictionary. Stale heap records are popped once, so the observed loops are amortized `O(N log N)`, not quadratic. Dispatcher loops are bounded by delivered work or time.
- **`src/wreath/http_client.py` (about 30 KiB):** close-delimited bodies accumulate chunks and join once; chunked bodies use `bytearray.extend`. No repeated immutable-body concatenation was found. Pool scans pop each idle connection once.
- **`src/wreath/orm/session.py`:** pending insert/delete membership uses ID sets/maps; ordering is `O(N log N)`. `tests/orm/test_session.py:723-743` already has a deterministic linear probe regression. Select-in relationship assembly uses a key-to-parent dictionary rather than parent×child scans.
- **`src/wreath/typegen/inspect.py` and `src/wreath/openapi.py`:** operation IDs and model ownership use dictionaries, and recursive model registration claims a name before descending. Route×parameter and model×field work reflects output size. No same-dimension quadratic scan was established.
- **`src/wreath/app.py` / middleware compilation:** route-specific middleware compilation is `O(RM)` when both routes and middleware scale, but the current representation emits a distinct chain per route, so that product also describes materialized output. Treat as a memory/startup watch item, not a proven accidental quadratic defect.
- **`src/wreath/orm/session.py:_hydrate`:** rebuilding a column-position dictionary per row is avoidable constant-factor work, but `O(rows × projected_columns)` matches the number of decoded cells; it is not an asymptotic conviction.

## Recommended order

1. Add the deterministic dependency-compiler test and fix QPY-001; it is the only worse-than-quadratic finding.
2. Add name and relationship indexes during registry compilation (QPY-002).
3. Decide whether deeply nested routers are a supported/generated workload; if yes, add QPY-003 before changing representation.
4. Fix QPY-004 opportunistically with a semantics-preserving index scan.
5. Index introspected foreign keys and strengthen referenced-column validation together (QPY-005).

## Audit limitations

This review is static and deliberately hostile. It proves source-level growth classes for the listed adversarial shapes, not user-visible latency at realistic sizes. No performance win should be claimed until the proposed test first fails, the implementation changes, the test passes, and repeated end-to-end startup/request measurements clear their A/A noise floor. Pure-Python fallback modules with direct native equivalents were not primary targets; C complexity remains covered by `wreath-native-lint` and requires its own audit.
