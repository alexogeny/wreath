# CLAUDE.md

This file exists so a coding agent loads the repository's rules without being
told to. The rules themselves are not here — keeping two copies would mean
keeping two copies accurate.

**Read [`AGENTS.md`](AGENTS.md) before changing anything.** It is short, and it
carries the constraints that are not visible from the code: Python 3.14 only, no
mandatory runtime dependencies in `src/wreath`, no Pydantic, no SQLAlchemy, no
performance claim from a single run, and the measurement rules that have already
caught one accepted-then-worthless optimization.

## Finding your way

| You want | Read |
| --- | --- |
| The shape of the repository in prose | [`repo-map.md`](repo-map.md) |
| Traps where the tool says "clean" and nothing ran | [`AGENTS.md`](AGENTS.md#traps-that-have-already-cost-someone-a-day) — read before a native, mutant, or worktree session |

**The public API is literal.** Each feature lives in the module its name
implies: `wreath.pagination` is `src/wreath/pagination.py`, `wreath.jobs` is
`src/wreath/jobs.py`. Guess the path before searching for it. A leading
underscore means implementation — use the facade that exports it.

## Before you say a change is done

```bash
uv run wreath test           # the suite on its own — NOT `uv run pytest`, see below
uv run wreath-check          # ruff, ty, pytest, native lints, trace baseline
```

**Run the suite with `uv run wreath test`.** It keeps pytest's semantics, picks
`min(8, cpu_count)` workers itself, and adds the heat map, timing history and
bounded mutation confidence. A bare `uv run pytest` passes no `-n`, so it runs a
single worker and is several times slower for strictly less evidence — reach for
it only to attach a debugger. `wreath test -k <pattern>` and `-m ''` work as you
would expect, so there is no reason to drop to `pytest` for a subset.

`wreath-check` is the whole gate set, including timed complexity probes. It is
not a test runner; do not re-run it to read a single failure.

Prefer these task entry points over a bare `uv sync --group X`: `uv sync`
reconciles the venv to exactly the groups named and **removes everything else**,
so syncing one group uninstalls the last one's tools. The task runners use
`uv sync --inexact`, which adds without evicting.

## If you change where something lives

Update imports, focused tests, `repo-map.md`, and any command entry points in the
same change. Use the source tree rather than a secondary manifest to find owners.
