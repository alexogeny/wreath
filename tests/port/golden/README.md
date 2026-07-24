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

```python
import sys; sys.path.insert(0, "src/wreath")   # load _port standalone (no native build)
import _port
from pathlib import Path
corpus = Path("tests/port/corpus/tumbleweed_api")
golden = Path("tests/port/golden/tumbleweed_api")
for rel in ["schemas.py", "models.py", "routers/bookings.py", "routers/llamas.py"]:
    dest = golden / (rel + ".expected")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_port.emit_module(corpus / rel), encoding="utf-8")
```

Add new `.expected` files as more corpus modules are worth pinning.
