# Native build guidance

Read this before changing C extensions, native buffers or caches, sanitizer inputs, SIMD, or architecture-specific code.

## Native rules and build traps


- **Native code owns no process-global mutable state.** Caches live for the
  operation that created them; buffers are allocated uninitialised and filled,
  never obtained-then-mutated. The JSON decoder is the pattern:
  `PyObject *key_cache[512]` is a member of the `Parser` struct
  (`_native/json.c`), created per decode and released with it. For object
  construction use `PyBytes_FromStringAndSize(NULL, n)` and fill the buffer --
  `NULL` always allocates, so the idiom cannot reach a cached singleton, and
  unlike a length guard it cannot regress when someone changes the bound. This
  is what keeps the free-threaded build honest.
- **`uv run` does not rebuild the extension you are editing, and every tool
  still works.** The import resolves to `src/wreath/`, whose `.so` files are
  built in place; `uv run` builds and installs a *wheel*, which nothing then
  imports. So a `.c` edit changes no behaviour, the tests pass, and a
  before/after benchmark times the same binary twice and reports the difference
  as noise. That is not hypothetical: three native changes were measured,
  declared regressions and reverted this way, and all three turned out to be
  8-39% wins once actually compiled. Rebuild with

      WREATH_BUILD_LINUX=1 uv run python setup.py build_ext --inplace

  and **prove it landed** rather than assuming: `uv run wreath-build-lint`
  reports BUILD001 for any artifact older than its sources, and the surest
  check is a sentinel -- add a distinctive string literal, rebuild, and confirm
  `strings` finds it in the `.so`. The lint is deliberately not in
  `wreath-check`, because a default build does not include `_http3` at all and
  a stale one left over from an earlier `WREATH_BUILD_HTTP3=1` build is not a
  finding about the change you are making. So nothing runs it for you. Run it
  yourself before believing any native measurement.

  The Linux switch is part of that command because the release base wheel is
  deliberately portable and the io_uring reactor ships in `wreath[linux]`.
  Source development still needs the reactor rebuilt in place; omitting the
  switch can leave an older `_reactor.so` importable while every portable
  extension is fresh.

  **`_http3` is buildable wherever its libraries are.** This paragraph used to
  say it "cannot be rebuilt here", which was read as a property of the
  repository and is not one -- it is a property of a machine. Where
  `pkg-config --exists libngtcp2 libnghttp3` succeeds, so does

      WREATH_BUILD_HTTP3=1 uv run python setup.py build_ext --inplace

  and this machine is one of them. The cost of believing otherwise is
  particular: an edit to `http3_asgi.c` or `http3_connection.c` compiles
  nowhere, every other gate stays green because nothing imports a module that
  was never rebuilt, and the change ships unverified. If `pkg-config` does not
  find them, say so and leave BUILD001 standing rather than treating it as
  scenery.
- **A new `.c` file must be registered in two places, and only the first fails
  loudly.** `setup.py` builds the extension; `tools/sanitizers/setup_core.py` has
  its own source list. Miss the second and the sanitized `_core.so` has an
  undefined symbol, *every* test fails to import, and `wreath-sanitize` reports
  "0 passed … clean" — the exact false success its own docstring warns about.
  The build and sanitizer tests must cover both source lists.
- **`wreath-sanitize --leaks` used to report every leak in Wreath's own C as
  libpython's.** ASan's default unwinder walks frame pointers; CPython is built
  with `-fomit-frame-pointer`; and essentially all of Wreath's C allocates
  through `PyMem_Malloc` -> `_PyObject_Malloc` before reaching `malloc`. The
  walk could not get back past libpython into our frame, so the leak record's
  stack jumped straight from `_PyObject_Malloc` to whichever interpreter
  function called us and the attribution -- which matches on the module path --
  found nothing of ours in it. The tool then printed "none attributable to
  Wreath" and "clean", for leaks that were entirely ours.

  Found by planting a 4 KiB leak in `kv_new` and running the KV suite over it:
  166 passed, 19 leak records, none attributable, clean. `sanitize.py` now sets
  `fast_unwind_on_malloc=0` under `--leaks`, and the same run names
  `kv_new .../kv.c:1148`. **A leak check you have not falsified is not a leak
  check** -- point the tool at a deliberate leak and confirm it goes red before
  believing a green one.
- **A per-architecture `#if` block is invisible to every other architecture.**
  `simd.h`'s NEON arms called their SWAR tails ~150 lines before those were
  declared. In C that is not a warning: the implicit declaration is assumed to
  return `int`, which *conflicts* with the real `static inline ptrdiff_t`
  definition below, and the translation unit fails to compile. It failed only on
  aarch64, so `wreath._native._core` would not have built on Apple Silicon or an
  ARM server, and nothing on an x86 machine said a word.

  Neither the compiler nor the test suite can find this from the wrong machine,
  so `tests/test_native_simd.py` reads the header as text and checks declaration
  order for every arm, reachable or not. Anything else behind an `#if
  defined(...)` deserves the same treatment: **if only one architecture compiles
  a block, only a source-level check will ever read it.**

- **`uv run` does not reliably rebuild after a `.c` edit.** A stale `.so` has
  produced two confident, wrong diagnoses. `uv sync --reinstall-package wreath`
  is the rebuild that works.
