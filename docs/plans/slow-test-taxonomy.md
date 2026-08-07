# Where the test suite's time goes

A diagnosis, not a plan. Every number below is measured on this machine; the
method for each is stated so a disagreeing measurement can be pointed at the
same thing.

## Method

`.wreath/test-history.json` records per-test durations for the last twenty runs.
`last_seconds` filtered to the last run's `finished_at` is what the default
marker set actually cost; `mean_seconds` is *not* usable for this, and why is a
finding in its own right (§7). Category costs were then re-measured directly
with `pytest --durations=0 --durations-min=0`, which splits setup / call /
teardown, and with targeted probes.

## The budget

Last default run: **36.5 s wall, 8 workers**, 15,088 collected, 14,642 passed,
446 skipped.

| Component | Cost | How it was measured |
| --- | --- | --- |
| Serial sum of every passing test | 186.3 s | sum of `last_seconds` |
| ... spread over 8 workers | 23.3 s | 186.3 / 8 |
| Collection, per worker | 5.3 s warm, 9.7 s cold | `pytest --collect-only -q` |
| Module imports inside that collection | 0.45 s | `-X importtime`, 454 modules |

Collection is ~0.3 ms per item over 15,088 items and is almost entirely pytest's
item construction, not imports. Each of the 8 workers pays it in parallel, so it
is a **~5 s floor no test-level change can touch**. 23.3 + 5.3 ≈ 29 s of the
36.5 s; the remainder is the mutation phase and scheduling slack.

Concentration: the top 10 tests are 58% of serial time, the top 100 are 81%.
This is a small-number problem.

---

## 1. Timeout, not work — a TLS shutdown defect  ✅ FIXED

The largest single item in the tree, and it does no work at all.

Ten tests, **30.0 s each, 300 s total** under `-m ''`. Split:

```
30.01s teardown   0.24s setup   0.01s call
```

The teardown is `server.close()`, decomposed by wrapping each step:

| Step | Cost |
| --- | --- |
| `close()` step 3, drain loop | 10.01 s |
| `close()` step 5, protocol teardown | 1.00 s |
| `asyncio.Server.wait_closed()` | 19.00 s |

**Read that table as one 30-second timer, not three costs.** The first reading
here blamed `server.py`'s drain predicate — `getattr(protocol,
"active_requests", None)` treats `None` as "still working", and the native
`Http2Protocol` kept `active_requests` as a C struct field it never published
to Python. That is a real defect, and fixing it alone changes nothing:

| | teardown |
| --- | --- |
| as shipped | 30.02 s |
| drain predicate fixed only | **30.02 s** |
| `ssl_shutdown_timeout=1` only | 1.02 s |
| both | 1.01 s |

The binding constraint is `asyncio.constants.SSL_SHUTDOWN_TIMEOUT`, which is
30 seconds and which `loop.create_server` was never told otherwise. The timer
starts when the first `transport.close()` runs, so everything after it queues
inside the same window — the drain loop was not *adding* 10 seconds, it was
spending 10 of the 30 that were going to elapse regardless.

It is also not a gRPC problem. Reproduced on plain HTTP/1.1 over TLS with a
client that stops being serviced: **30.02 s** to close a server that had
answered one request. Any peer that goes quiet without a `close_notify` holds
its transport, and therefore its protocol object, and therefore shutdown.

**Fixed** by `ServerConfig.ssl_shutdown_timeout` (default 1.0 s,
`WREATH_SSL_SHUTDOWN_TIMEOUT`, refused at construction if non-positive), passed
to `create_server` only when TLS is on. The drain predicate was fixed too,
because it is wrong on its own terms: `active_requests` is now published by
`server_http1.c`, `server_http2.c` and the pure `HttpProtocol`, so no shipped
protocol lands in the "cannot tell, assume busy" fallback.

Result: `test_grpc_interop.py` **300 s → 13 s**, all eleven passing, native and
pure. Held by `test_a_tls_connection_owing_nothing_does_not_hold_the_close` and
`test_an_idle_connection_reports_no_work_to_drain` in `tests/test_server.py`.

Two shapes that look like they reproduce this and do not, both recorded in the
test: `transport.abort()` sends a RST and passes against the unfixed server,
and an idle `open_connection(ssl=...)` peer answers the `close_notify` itself.

## 2. Out-of-process tests — ~50 s serial  ◐ PARTLY FIXED

26 files spawn children. Instrumenting `subprocess.Popen` across them: **84
spawns, 90.4 s of child wall in a 44.2 s serial run** (children overlap).

| Child | n | Total | Per spawn |
| --- | --- | --- | --- |
| `python -m wreath._mutant.cli` | 12 | 35.6 s | 2.97 s |
| `python -m wreath test` | 3 | 11.1 s | 3.71 s |
| `python -m wreath._devtools.native_lint` | 1 | 5.5 s | 5.49 s |
| security PoC scripts | 8 | ~11 s | ~1.4 s |
| nested `pytest` | 3 | 4.3 s | ~1.4 s |

The two at the top are the suite running the suite. Everything else pays a fixed
import tax before it does anything:

```
 27 ms  bare interpreter
 34 ms  import wreath
118 ms  import wreath._native._core     <- 74 ms of this is asyncio
221 ms  import pytest
```

`wreath/__init__.py` is lazy enough to import in 34 ms. `wreath/_native/__init__.py`
was not: it eagerly loaded `_client`, whose module init imports `asyncio` and
with it `ssl`, `subprocess`, `logging`, `inspect` and `dataclasses` — 74 ms on
every child that touches any native module, and on every xdist worker, for an
HTTP client most of them never open.

**Fixed**: `_client`, `_reactor` and `_edge` moved behind a module
`__getattr__` that caches into the module globals on first reference, so
`from wreath._native import _client` still works and still honours
`WREATH_PURE`. `_core` stays eager — nearly everything wants it and it costs a
dlopen.

    import wreath._native._core     118 ms  ->  39 ms

Held by `test_loading_the_native_package_does_not_drag_in_asyncio` (subprocess,
because `sys.modules` in the test process is already contaminated) and
`test_the_lazily_loaded_extensions_are_still_reachable_by_name`.

The two big items are not fixable this way and are not defects: `wreath._mutant.cli`
(35.6 s) and `wreath test` (11.1 s) are the suite testing the suite, and a real
child process is the thing under test.

## 3. Whole-repository scanners — ~13 s serial  ◐ PARTLY FIXED

Tests whose cost tracks the size of the repository rather than the size of the
thing under test:

```
4.44s  test_dup_scan::test_it_runs_on_this_repository_and_stays_a_report
2.85s  test_map_lint::test_repository_maps_are_accurate
2.35s  test_docs_ssg::test_builds_the_guides_as_a_real_site
1.16s  test_native_lint::test_the_native_tree_is_clean
1.16s  test_docs_api_shapes::test_every_component_method_is_callable...
0.95s  security/test_web_framework_hardening::...no_unsafe_deserializer...
0.92s  test_native_lint::test_cli_entrypoint_runs[args1]
0.67s  test_optimized_mode::test_no_module_level_assert_guards_an_invariant
```

The shared unit of cost, measured directly:

| Sweep | Files | Bytes | Read | `ast.parse` |
| --- | --- | --- | --- | --- |
| `src/wreath/**/*.py` | 379 | 7.64 MB | 22 ms | **1251 ms** |
| `tests/**/*.py` | 729 | 7.82 MB | 24 ms | **1639 ms** |

Reading is free; parsing is everything. Five or six independent full-tree parses
happen per run with no shared cache — `dup_scan` alone does both roots, which is
2.9 s of its 4.4 s. `tests/audit/test_code_rules.py` is the counter-example worth
copying: 214 tests in 0.27 s, because it parses snippets rather than the tree.

`test_docs_ssg::test_builds_the_guides_as_a_real_site` is a different flavour of
the same thing — `shutil.copytree` of the 5.4 MB / 352-file docs corpus, then a
full site build, per invocation.

### The cache that was not built

Instrumenting `ast.parse` across those files: **2,100 calls, 37.7 MB, of which
76% of the bytes are re-parses of identical source.** Five callers each sweep
the same 7.7 MB. That reads like an obvious win for a process-level cache, and
it is not — holding every tree costs **213 MB of RSS**, which is per xdist
worker. Caching would trade 1.9 s of CPU for a quarter-gigabyte of memory
eight times over. Measured before building it, which is the only reason it did
not get built.

### What was done instead: delete the work, don't cache it

Reading the whole tree costs 22 ms; parsing it costs 1.9 s. So the cheap half
answers first, via a **sound** token pre-filter — sound, not heuristic, because
every node these scans look for requires its own spelling to be present in the
source. A file lacking the substring is a provable non-match.

| Scan | Filter | Before | After |
| --- | --- | --- | --- |
| `test_map_lint::test_repository_maps_are_accurate` | a `BUDGETED` name | 2.85 s | 0.56 s |
| `test_docs_api_shapes::...callable_with_no_arguments` | `component` | 1.16 s | 0.38 s |
| `security::...no_unsafe_deserializer_or_shell_sink` | sink names | 0.95 s | 0.33 s |
| `test_optimized_mode::test_no_module_level_assert...` | `assert` | 0.67 s | 0.26 s |
| | | **5.63 s** | **1.53 s** |

Each filter was falsified against the real tree rather than argued: parse
everything, compute the true match set, confirm the filter's kept set is a
superset. **Zero matches dropped in all four.** Planted offenders of every
shape each scan looks for (`assert` at module level, a `component` method, an
`import pickle`, an `os.system`, a `shell=True`) are still detected.

None of the scans was narrowed to a subset of the tree — sweeping everything is
the point of all four, and `test_docs_api_shapes` says so explicitly.

`dup_scan` (4.35 s) is **not** fixed and cannot be by this route: it compares
every function body in the repository, so there is no token that predicts a
non-match. `test_docs_ssg`'s copytree-and-build (2.26 s) is untouched.

## 4. Interpreted-loop crypto — ~12 s serial  ◐ PARTLY FIXED

`test_aesgcm_parity::test_native_and_pure_agree_under_a_seeded_fuzz`, 6.2 s.

Priced as a loop, both halves: pure-Python AES-128-GCM runs at **0.13 MB/s**,
**8.1 ms per iteration** at the test's average size. 400 iterations × (encrypt +
decrypt) predicts **6.5 s** against 6.2 s observed, so the model is the
explanation and there is no hidden cost elsewhere.

The body cannot get cheaper — it is CPython doing AES. The length is the lever,
and the iteration count is not: what this test adds over the parametrized ones
is random *combinations* of key, nonce, length and AAD.

**Fixed** by capping the fuzz at 512 plaintext bytes and 96 of AAD, with the
iteration count untouched at 400. The C path steps four blocks at a time, so
512 bytes runs eight four-block steps and every remainder class; nothing new
happens at 2000. The lengths that *are* structurally interesting at the top end
— 1000, 4000, 4079, 4096 against AAD up to 1000 — stay covered exhaustively and
deterministically by `LENGTHS` and `AAD_LENGTHS`, which is where they belong: a
fuzz that reaches a case one run in ten is not coverage.

    6.2 s -> 2.0 s

`test_ec_p256.py` (2.86 s) and `test_ec_ed25519.py` (2.88 s) are the same shape
— pure-Python big-integer curve arithmetic over a corpus — and were **left
alone**. Their corpora are fixed vectors rather than a tunable fuzz, so there is
no length knob to turn that is not a coverage decision.

## 5. Unshared expensive fixture — ~4.2 s serial  ✅ FIXED

`tests/tracking/test_seed.py`: nine tests, 4.65 s, and `build_rows()` — which
generates 51,840 fixes — is called fresh in **every one** at ~0.47 s. Eight of
the nine only read the result. The ninth,
`test_two_runs_produce_byte_identical_rows`, calls it twice *on purpose*, and
that one must keep doing so.

**Fixed** with a module-scoped `rows` fixture for the eight readers. The
determinism test deliberately does not take it — its claim is that two *fresh*
builds agree, which a shared one cannot make. Module scope rather than session
so the cost still lands in this file's timings, where the heat map will show it.
Three builds now instead of nine: **4.65 s -> 1.7 s**.

## 6. Real-time waits  — LEFT ALONE

`tests/reactor/test_metal_gc.py` (4.23 s / 19) and `test_metal_tier.py`
(4.53 s / 44) spend their time in `time.sleep` and GC-quiescence windows. These
are wall-clock by construction — a collection floor cannot be observed faster
than it happens — and are listed for completeness rather than as a target.

## 7. The scheduler is balancing on stale weights  ✅ FIXED

`wreath test` already ships an LPT scheduler (`_test_runner.py:964`,
`HistoricalLoadScheduling`) that dispatches the heavy head longest-first, so tail
imbalance is handled. But it weights on `mean_seconds`, and that field is a
**cumulative mean over every sample ever recorded** (`_test_runner.py:852, 876`):

```python
"mean_seconds": old_mean + (seconds - old_mean) / samples
```

With no `WREATH_TEST_POSTGRES_DSN` set, the 446 tests that skip carry weights
earned when a real server was present:

| | weight the scheduler uses | what they actually cost |
| --- | --- | --- |
| 446 skipped tests | **160.6 s** | **0.383 s** |
| 14,642 passing tests | 201.7 s | 186.3 s |

**44% of the total weight the scheduler balances on belongs to tests that cost
nothing.** At 244+ samples each, that decays over hundreds of runs. The passing
tests' weights are fine (201.7 vs 186.3 actual); it is specifically the
skip/run transition the cumulative mean cannot follow.

**Fixed**, in two parts, because the field has two separate problems:

* The divisor is now `min(samples, _MEAN_WINDOW)` with a 20-run window. An
  unbounded mean is not merely slow to move, it is *unreachable*: at 200
  samples one observation shifts it by half a percent of the difference, so a
  test whose cost genuinely changed keeps its old weight for the tree's life.
  `samples` still counts everything; only the weighting is bounded.
* A test whose last outcome was `skipped` now weighs 0 at dispatch. A skip is a
  step change, not a drift, and even a bounded mean spends its whole window
  catching up to one — while what decides it (a DSN, a built extension, a
  free-threaded interpreter) is stable across a run, so last run's answer is
  the better prediction. The stored mean is left honest about what those runs
  actually cost; only the dispatch weight is zeroed.

Applied to the existing history file, the phantom weight goes to **0.000 s, 0%**
immediately — the skip rule reads at dispatch, so it needs no decay period.
Held by `test_a_test_that_stops_running_stops_carrying_its_old_weight` and
`test_a_weight_follows_a_lasting_change_in_what_a_test_costs`.

## 8. A KDF in test setup — ~2.9 s of 9.1 s  ◐ PARTLY FIXED

Found after the first seven, by asking what was left rather than what was
slowest. `tests/test_users_totp.py` shows a flat ~0.18 s plateau across dozens
of tests, which is the signature of a shared per-test cost rather than one slow
test.

Counting `hashlib.scrypt` across the twelve files that touch the user kit:
**234 calls, 9.1 s of a 13.67 s serial run — 67% of those files is one KDF.**
`hash_password` and `verify_password` each measured at ~60 ms, which is the
point of a KDF and not a defect.

Split by caller, it is two different things:

| | calls | cost | |
| --- | --- | --- | --- |
| `verify_password` on the login path | 106 | 3.83 s | the thing under test |
| `hash_password` in test `_seed` helpers | ~85 | ~2.9 s | setup, same password every time |

**Fixed** the second: `PASSWORD_HASH = hash_password(PASSWORD)` once per module
in `test_users_totp.py`, `test_users_webauthn.py` and `test_users_stepup.py`,
where `_seed` was re-deriving a constant. Login still verifies against it for
real, so the path under test is untouched.

    234 calls / 9.1 s  ->  153 calls / 6.6 s   (file wall 13.67 s -> 11.55 s)

Sharing one salt across seeded users is safe here because nothing in those
files reads a stored hash; a test asserting that two users hash differently
would have to call `hash_password` itself.

**Not fixed, and it is a decision rather than a cleanup:** the remaining 3.83 s
is real `verify_password` on the login path. Removing it means lowering
`_SCRYPT_N` for tests. The argument for is that `n` is *data* in the hash string
(`scrypt$n$r$p$salt$hash`), so a cheaper hash still round-trips through the
identical parse, derive and constant-time compare -- only the work factor
differs. The argument against is that it weakens a security parameter across
the suite, and it would need a test asserting the shipped default is still
16384 so a regression that lowered it in *production* could not hide behind the
test knob. Worth doing, but someone should choose it.

## Summary

| Class | Before | After | Status |
| --- | --- | --- | --- |
| 1. TLS shutdown timeout | 300 s under `-m ''` | 13 s | ✅ a real server defect |
| 2. Out-of-process import tax | 118 ms/child | 39 ms/child | ◐ the tax; not the nested suites |
| 3. Whole-repo scanners | 5.63 s (of ~13 s) | 1.53 s | ◐ `dup_scan`, `docs_ssg` remain |
| 4. Interpreted crypto loops | 6.2 s | 2.0 s | ◐ EC corpora left alone |
| 5. Unshared fixture | 4.65 s | 1.7 s | ✅ |
| 6. Real-time waits | ~9 s | ~9 s | — irreducible by construction |
| — Collection | ~5 s wall | ~5 s wall | — floor, paid per worker |
| 7. Stale scheduler weights | 44% phantom | 0% | ✅ |
| 8. scrypt in test setup | 9.1 s | 6.6 s | ◐ the login-path half is a decision |

Classes 1, 5 and 7 were outright wrong rather than expensive, and class 1 was a
production defect that a slow test happened to expose. Class 3 is the one that
gets worse on its own, because it grows with the repository — the four filtered
scans now grow with the *matching* subset instead, but `dup_scan` still does not.

### What is deliberately left

* **`dup_scan`, 4.35 s.** It compares every function body in the repository, so
  no token predicts a non-match and the pre-filter route does not apply.
* **`test_docs_ssg`'s copytree-and-build, 2.26 s.** Real work: it builds the
  real corpus, which is the test.
* **The nested `wreath._mutant.cli` and `wreath test` children, ~47 s.** A real
  child process is the thing under test.
* **`test_ec_p256` / `test_ec_ed25519`, 5.7 s.** Fixed vectors, not a tunable
  fuzz; shrinking them is a coverage decision, not a cleanup.
* **An AST cache.** Measured at 213 MB RSS per worker. Not worth it.
* **Lowering `_SCRYPT_N` for tests**, worth ~3.8 s in the user suites alone. See
  §8 — it trades a security parameter for time and needs a deliberate choice.
* **`test_replay_adversarial`, 3.9 s.** "Truncate at every offset" is a genuine
  loop over offsets; shortening it is dropping cases.
* **`security/test_attack_surface_matrix`, 4.8 s.** Each PoC is a standalone
  subprocess *on purpose* -- that it runs outside the suite's imports is the
  regression being guarded.
* **The broad tail.** Below the top 500 tests, 14,142 tests hold 32.7 s at a
  2.3 ms mean. There is no shared cause left in there to find; that is what a
  pytest test costs.
* **Aborting lingering transports during shutdown.** With `ssl_shutdown_timeout`
  at 1 s a graceful close still spends that second on a peer that will never
  answer. `close()` could `abort()` whatever survives its teardown wait, which
  would bound shutdown by `shutdown_timeout` alone — but `abort()` discards
  buffered writes where `close()` flushes them, so it risks truncating a
  response that had just completed. That is a behaviour decision, not a
  cleanup.
