# The gates

A change is done when it passes these, not when it looks finished. `wreath-check`
runs them together:

```bash
uv run wreath-check          # ruff, ty, pytest, native lints, complexity probe, trace baseline
uv run wreath-check --docs   # ... and a strict docs build
```

Run them individually while you work:

| Command | What it protects |
|---|---|
| `uv run pytest` | Behaviour — the default suite. Serial (~31s); add `-n 6` for ~8s. |
| `uv run pytest -m '' -n 6` | Everything, including network, fuzz, and performance. |
| `uv run ruff check .` | Lint and import hygiene. |
| `uv run ty check` | Types. |
| `uv run wreath-map-lint` | The maps you arrived by — that `docs/agents/manifest.json`, `AGENTS.md`, `repo-map.md`, and `docs/llms.txt` still describe this repository. |
| `uv run wreath-native-lint` | C complexity patterns; its siblings `wreath-native-error-lint`, `wreath-native-gil-lint`, and `wreath-native-memory-lint` cover error-handling, GIL, and memory. `0` means clean. |
| `uv run wreath-request-trace --check` | The Python↔native boundary — that you didn't add crossings. |

## The gate you can pass without running

Some suites need a real PostgreSQL and skip when `WREATH_TEST_POSTGRES_DSN` is
unset. They are the only cover for behaviour a fake cannot model — parameter
type inference, query plans, lock and timeout behaviour, DST boundaries — and
they went a long time without executing once, because a skip reason lives in
`-rs` output that a `-q` run never prints. When they finally ran they found a
defect in a *default* code path that worked on its first call and raised on
every call after.

`tests/conftest.py` now prints a banner naming how many skipped, and
`wreath-check` repeats it on its last line. Neither fails the run: a warning
that breaks the build gets suppressed, and then the skip is invisible again. If
you see the banner, your green tick covers less than it looks like it does.

```bash
docker run -d --name wreath-test-pg -e POSTGRES_PASSWORD=wreath \
  -e POSTGRES_USER=wreath -e POSTGRES_DB=wreath_test -p 55432:5432 \
  postgres:17-alpine -c max_connections=200 -c fsync=off -c synchronous_commit=off
export WREATH_TEST_POSTGRES_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"
```

`podman` and `nerdctl` work as well. A few database suites also carry the
`network` mark and so are excluded by the default marker expression outright —
`-m ''` includes those.

`wreath-map-lint` is the cheapest gate and runs first. It fails when the manifest
cites a path that isn't there, when a public module under `src/wreath` belongs to
no subsystem, when a prose map names a file that no longer exists, or when a
guide is missing from `docs/llms.txt`. If you moved a file or added a module,
update `docs/agents/manifest.json` in the same change — the map is how the next
agent finds your work without reading the whole tree.

The request-trace baseline lives at `docs/agents/request-boundary-baseline.json`.
If a change *intentionally* alters the number of boundary crossings, re-record it
deliberately with `uv run wreath-request-trace --update-baseline` and say so — an
unexplained change to that count is a red flag, not a rounding error.
