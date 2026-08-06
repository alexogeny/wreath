# The hardening corpus

Each file here is a defect, written the way an application actually writes it,
with the rule it must produce marked in place:

```python
rows = await db.raw(sql).fetch()  # hardening-expect: sql-interpolation
```

`tests/test_hardening_corpus.py` runs the code ruleset over the whole directory
and asserts the two halves of the contract at once:

* every line carrying a `hardening-expect:` marker produced exactly those rules,
* and **no other line produced anything at all**.

The second half is the one that matters. A rule that fires on correct code gets
switched off within a week, and then it is worth less than no rule, so every
`insecure_*.py` here has a `secure_*.py` twin: the same handlers, written the
way the documentation says to write them, with no markers in them. If a rule
starts flagging a twin, the corpus goes red.

## Why this exists next to `tests/audit/test_code_rules.py`

That file tests each rule against a snippet written for it. This one tests the
*ruleset* against whole handlers — routers with decorators, ORM sessions,
sanitisers, and the ordinary correct code a real defect hides inside. The two
fail in different directions, which is the point: a rule can pass its own
snippet by matching a shape that never occurs in a real module, and a rule can
fail a real module by depending on taint that only propagates in a snippet.

Four rules were found to have exactly that gap when this corpus was first
pointed at them:

| defect | what the ruleset missed |
| --- | --- |
| a development key that shipped | `"northwind-dev-secret"` has no digits, so the key-alphabet test read it as an identifier |
| a timing oracle | the comparison was hand-rolled as a loop, and only `ast.Compare` was examined |
| a path traversal | `EXPORT_ROOT / name` had no rule at all |
| a weak draw | `random.randbytes(16).hex()` hid the draw one call in |

## Where the shapes come from

The insecure halves reproduce the Tier A defects planted in an intentionally
vulnerable Wreath application used to measure whether a defect of each class can
survive in a Wreath app at all. That application is not in this repository and
never will be; what is here is the *shape* of each defect, with the planted
credentials and flag values removed.

These files are never imported and never executed — they are read as text and
parsed. That is why they can contain a hardcoded credential and a path traversal
without either being a real one, and it is why `pyproject.toml` gives the
directory its own ruff per-file-ignores rather than letting anything here carry
an inline `noqa`.
