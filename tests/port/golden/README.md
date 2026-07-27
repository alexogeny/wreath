# Golden expected output

Each `*.py.expected` file here is the **exact** `wreath port` declarative-emit
output for the corresponding `../corpus/<app>/<module>.py` source.
`test_golden_output.py` compares the tool's output against these byte-for-byte.

These are **real emitter output** (Phase 1 landed), not placeholders. The emitter is
pure, deterministic Python — the provenance-header hashes depend only on the source
and emitted bytes — so the golden equals `emit_module(source)` regardless of when or
where it runs (the golden tests activate via `pytest.importorskip("wreath.port")`
once wreath builds).

Regenerate after an intentional emitter change:

```bash
uv run wreath-port-golden            # what drifted, and nothing written
uv run wreath-port-golden --update   # rewrite the drifted ones
```

To pin a new module, create the empty `.expected` beside its siblings and run
`--update` — the pinned set is this tree, so the tool fills in the goldens that
exist rather than deciding which corpus modules deserve one.

```bash
touch tests/port/golden/summit_ops/intake.py.expected
uv run wreath-port-golden --update
```

**This used to be a copy-pasteable Python snippet here, and it had already
drifted.** It named four `tumbleweed_api` modules in a hardcoded list; a fifth
golden was added later under `summit_ops/`, and following the documented
procedure regenerated four of five files and said nothing about the one it
skipped. A glob cannot go stale that way. The tool also checks, on the same
pass, that the emit is deterministic, that its output *compiles* rather than
merely parses, and that no `.expected` has outlived its corpus source — an
orphan keeps passing, because `test_golden_output.py` parametrizes over the
goldens and simply stops generating a case for it.
