# Porting docs — staging note

These pages document the **`wreath port`** codemod, which is designed but not yet
shipped. They live under `docs/plans/porting/` because `docs/plans/` is listed in
`mkdocs.yml`'s `exclude_docs`, so the strict docs build does **not** collect them
or flag them as orphan pages while the tool is in flight.

When `wreath port` ships:

1. Move these pages into the mkdocs `nav` — either as a new top-level **"Porting"**
   section, or folded into the existing **"Coming from FastAPI"** section (they
   share the `docs/from-fastapi/` equivalence tables as their rule source).
2. Remove `plans/porting/` from `exclude_docs` (or move the files out of
   `plans/`) so strict mode validates them like any other page.
3. Regenerate the golden expected outputs under `tests/port/golden/` from the
   real tool output (see that directory's README) and drop the placeholders.

The anonymized test corpus lives at `tests/port/corpus/` and the skipped tests at
`tests/port/` (they `pytest.importorskip("wreath.port")`, so they activate
automatically once the module exists).
