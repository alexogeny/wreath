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
| Which files a subsystem owns, and its tests, invariants, and ADRs | `docs/agents/manifest.json` — look up the subsystem before grepping |
| The shape of the repository in prose | [`repo-map.md`](repo-map.md) |
| Every check a change must pass | [`docs/cookbook/agents/checks.md`](docs/cookbook/agents/checks.md) |
| The smallest correct change, end to end | [`docs/cookbook/agents/add-an-endpoint.md`](docs/cookbook/agents/add-an-endpoint.md) |
| Proving behaviour rather than green tests | [`docs/cookbook/agents/verify-a-change.md`](docs/cookbook/agents/verify-a-change.md) |
| The docs a new public module must ship with | [`docs/cookbook/agents/documenting-a-module.md`](docs/cookbook/agents/documenting-a-module.md) |
| Which verb to use when one already exists elsewhere | [`docs/cookbook/agents/naming.md`](docs/cookbook/agents/naming.md) |
| What is deliberately not shipped yet | `docs/reference/roadmap.md` |
| Traps where the tool says "clean" and nothing ran | [`AGENTS.md`](AGENTS.md#traps-that-have-already-cost-someone-a-day) — read before a native, mutant, or worktree session |

**The public API is literal.** Each feature lives in the module its name
implies: `wreath.pagination` is `src/wreath/pagination.py`, `wreath.jobs` is
`src/wreath/jobs.py`. Guess the path before searching for it. A leading
underscore means implementation — use the facade that exports it.

## Before you say a change is done

```bash
uv run wreath test           # the suite on its own — NOT `uv run pytest`, see below
uv run wreath-check          # ruff, ty, pytest, native lints, map lint, trace baseline
uv run wreath-check --docs   # ... and a strict docs build
```

**Run the suite with `uv run wreath test`.** It keeps pytest's semantics, picks
`min(8, cpu_count)` workers itself, and adds the heat map, timing history and
bounded mutation confidence. A bare `uv run pytest` passes no `-n`, so it runs a
single worker and is several times slower for strictly less evidence — reach for
it only to attach a debugger. `wreath test -k <pattern>` and `-m ''` work as you
would expect, so there is no reason to drop to `pytest` for a subset.

`wreath-check` is the whole gate set — thirteen of them, including a full docs
build and timed complexity probes. It is not a test runner; do not re-run it to
read a single failure.

Prefer these task entry points over a bare `uv sync --group X`: `uv sync`
reconciles the venv to exactly the groups named and **removes everything else**,
so syncing one group uninstalls the last one's tools. The task runners use
`uv sync --inexact`, which adds without evicting.

## If you change where something lives

Update `docs/agents/manifest.json` in the same change — add the subsystem, move
the paths, or fix the tests list. `uv run wreath-map-lint` fails when the
manifest, `AGENTS.md`, `repo-map.md`, or `docs/llms.txt` cite a path that is not
there, when a public module belongs to no subsystem, or when a guide is missing
from the compact index. That gate exists because those maps drifted badly once
and every agent that trusted them lost a plan to it.
