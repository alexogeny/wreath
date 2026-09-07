# Toolchain guidance

Read this before syncing dependency groups, choosing a test or check command, or changing development-environment dependencies.


## Commands

`uv sync` reconciles the venv to exactly the selected groups and **removes
everything else**, so anything a workflow needs must be declared in a group.
That default also means `uv sync --group benchmark` uninstalls the dev
toolchain and the next `uv sync --group dev` uninstalls sanic, each tool working
only until the next one runs. **Prefer the task entry points** -- `wreath-check`
and `wreath-bench` -- which install their own group with
`uv sync --inexact`, adding without evicting. Reach for a bare `uv sync --group X`
only to reconcile deliberately, and name *every* group you still need when you do.

`[tool.uv] default-groups = ["dev"]` keeps the dev toolchain installed for every
sync. Two entries in `dev` exist only for that reason and look redundant
otherwise:
`setuptools` (`[build-system] requires` populates uv's *isolated* build env, not
this venv, and in-place native rebuilds import it from here) and `cryptography`
(the TLS and HTTP/3 tests mint throwaway certs with it). Both used to be present
only as accidental transitives; when a sync pruned them, native rebuilds failed
while leaving a stale `.so` importable, and the TLS/HTTP-3 tests skipped
themselves rather than failing. `tests/test_dev_environment.py` now asserts both.

```bash
uv run wreath-check              # ruff, ty, pytest, native lints, trace baseline
uv run wreath-bench --framework wreath starlette fastapi   # installs competitors first

# The individual gates, when you want one of them.
# ** Run the suite with `wreath test`, not with `pytest`. ** It is the routine
# check: it picks min(8, cpu_count) native workers itself, preserves the
# supported pytest compatibility surface, and adds the state map, timing history and bounded mutation
# confidence. A bare `uv run pytest` takes no `-n` of its own, so it runs one
# worker and is several times slower for no extra evidence.
uv run wreath test             # THE routine suite: grid, timings, auto confidence
uv run wreath test -m ''       # everything, including network/fuzz/performance
uv run wreath test -k pattern  # a subset, same runner
uv run pytest                  # ONLY to attach a debugger to a serial process
uv run ruff check .
uv run ty check
uv run wreath-native-lint        # C complexity patterns (see below); 0 = clean
uv run wreath-sanitize --all     # build each ASan/UBSan extension and drive tests at it
uv run wreath-sanitize core --leaks   # ... and attribute what is still live at exit
uv run wreath-port-golden        # tests/port/golden/ still matches the emitter
uv run wreath-port-golden --update    # ... rewrite what drifted, on purpose
uv run wreath-dup-scan           # function bodies sharing a structure (a report, not a gate)
uv run wreath-request-trace      # Python/native crossings for one request lifecycle
uv run wreath-request-trace --check   # ... vs tools/baselines/request-boundary-baseline.json
uv run wreath-complexity-probe --discover        # superlinear shapes no probe covers yet
uv run wreath-complexity-probe --discover-check  # ... vs tools/baselines/complexity-discovery.json
uv run wreath-policy-decomp      # what first-class HTTP policy costs a request
uv run wreath-decomp             # request stages, ORM internals, ns/frame calibration
```
