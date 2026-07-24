# Golden expected output

Each `*.py.expected` file here is the intended `wreath port` emission for the
corresponding `../corpus/<app>/<module>.py` source. `test_golden_output.py`
compares the tool's output against these byte-for-byte.

**Every file here is currently a PLACEHOLDER** — it carries a
`# GOLDEN PLACEHOLDER` header and does NOT contain real emitted output. The
golden tests are skipped (via `pytest.importorskip("wreath.port")`) until the
codemod ships, so the placeholders never fail CI today.

When `wreath port` Phase 1 lands: run the tool over the corpus, hand-review the
emission for each placeholder, and replace the file contents with the reviewed,
canonical output (dropping the placeholder header). Add new `.expected` files as
more corpus modules become translatable.
