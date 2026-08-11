# Foreign-framework fixtures

Input text for `wreath port`, in frameworks it does **not** translate. Never
imported or executed — only read as source, like `../corpus/`.

The contract here is the opposite of `../corpus/`'s. Everything in `corpus/`
must yield findings; everything here must yield either none at all or a refusal,
and the test that matters is that the tool *says which*. Two failures put this
tree here, both found by running `wreath port` over applications it was never
built for:

- **Silence with a zero exit.** aiohttp, Tornado and Pyramid trees produced no
  findings, `coverage_overall: null`, and a successful exit — indistinguishable
  from a clean bill of health on an application the tool had understood nothing
  of.
- **A perfect score off a spelling coincidence.** A Bottle application scored
  1.00, because `@app.get("/x")` is spelled identically in Bottle and FastAPI.
  The tree the tool was most confident about was the one that must be refused
  outright, because it is monkeypatched.

Each fixture is small and carries the one construct that defeats static analysis
in its framework, so a rule that starts firing here is a rule that has begun
guessing:

| fixture | framework | what it encodes |
| --- | --- | --- |
| `mudflat_gauge` | Flask | `@app.route` and blueprint registration |
| `thicket_registry` | Django | `.objects` on a manager that filters rows out |
| `saltpan_monitor` | Tornado | regex route tuples, handler classes, two async generations |
| `cairn_index` | Pyramid | traversal — the URL space is a runtime object graph |
| `estuary_hub` | aiohttp | routes registered in a loop from stored configuration |
| `heathland_sync` | gevent | `monkey.patch_all()` over an ordinary-looking module |

Adding one is a claim about a framework's shape, so keep it to the smallest file
that carries the claim.
