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
| `uv run pytest` | Behaviour — the default suite (~3.5s, run serially). |
| `uv run pytest -m '' -n 4` | Everything, including network, fuzz, and performance. |
| `uv run ruff check .` | Lint and import hygiene. |
| `uv run ty check` | Types. |
| `uv run wreath-native-lint` | C complexity patterns; its siblings `wreath-native-error-lint`, `wreath-native-gil-lint`, and `wreath-native-memory-lint` cover error-handling, GIL, and memory. `0` means clean. |
| `uv run wreath-request-trace --check` | The Python↔native boundary — that you didn't add crossings. |

The request-trace baseline lives at `docs/agents/request-boundary-baseline.json`.
If a change *intentionally* alters the number of boundary crossings, re-record it
deliberately with `uv run wreath-request-trace --update-baseline` and say so — an
unexplained change to that count is a red flag, not a rounding error.
