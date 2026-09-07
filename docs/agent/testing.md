# Testing and mutation guidance

Read this before adding tests, using skips or suppressions, running mutation checks, or diagnosing an existing failure.

## Test and mutation rules


- **A mutant killed in one execution mode and surviving in another has
  survived.** `wreath mutant` reports per run, and free-threading and the JIT are
  separately swept modes. When combining runs, the rule is **survived wins**,
  never "killed in either":

  | mode A | mode B | combined |
  | --- | --- | --- |
  | killed | killed | killed |
  | killed | unreached | killed |
  | unreached | killed | killed |
  | **killed** | **survived** | **survived** |
  | survived | anything | survived |
  | unreached | unreached | unreached |

  `unreached` is absence of evidence — that mode simply does not execute the
  line, so it neither confirms nor denies. `survived` is evidence: the line *was*
  executed and no test objected, so a regression on it ships undetected to
  everyone running that mode. Taking the optimistic union instead reports a
  module as covered when one of its shipped configurations is not, which is the
  same lie as a wrong `--tests` set, in the same direction.
- **Never `xfail`, and never `skip`, to park a test for something unbuilt.** A
  test exists to be green or to be red. `xfail` invents a third state that means
  "we know", and a checklist of `xfail`s is worse than no checklist: it reports
  success for work nobody did, it survives every gate, and `strict=True` does not
  save it -- that only moves the alarm to whenever the feature lands, which is the
  one moment somebody was already looking.

  So: if a surface is worth a test, implement the surface. If it is not ready to
  implement, leave `tests/` alone. Red-green TDD is welcome and is not this: writing a
  failing test and *then making it pass in the same change* is the good version.
  Committing the red half on its own is not a checklist, it is a broken gate with
  a note attached.

  The narrow exception is a test skipped on a **missing capability of the
  environment**, not of Wreath -- no `WREATH_TEST_POSTGRES_DSN`, no `pgvector`, no
  free-threaded build. Those already have their rules above, including the banner
  that makes the skip visible, and they are gated on something the reader can go
  install.
- **Never add a `noqa` to make your own new code pass.** Write it to the modern
  standard instead. A suppression is a claim that the rule is wrong *here*, and
  that claim belongs in `[tool.ruff.lint.per-file-ignores]` in `pyproject.toml`
  where it is declared, scoped, reviewed, and carries the comment explaining it --
  the way `src/wreath/_port/rules/` earns its `E501`. An inline directive on a
  line you just wrote is the same move as `xfail`: it converts "this does not meet
  the standard" into a third state that passes the gate silently.

  If a lint rule blocks the **only** way to express a test, that is a finding, not
  an obstacle. Say so in the test that gets as close as it can, and leave the rest
  undecided for a human. This rule exists because a mutation-testing session
  wanted to cover `binding.py`'s `args[0] if args else Any` fallbacks, which are
  reachable only from the deprecated `typing.List`/`typing.Dict` aliases that
  UP006 forbids. Four `# noqa: UP006`s went in to reach them. Ruff's `--fix` had
  already rewritten one of the *unsuppressed* uses to `list`, which inverted what
  the test asserted -- it passed in isolation for the wrong reason and failed in
  the suite -- and the correct answer was that those fallbacks exist for a
  *caller's* annotations and cannot be measured from inside this repository at all.
  The suppression would have hidden a real design note behind four green lines.

  Deleting a `noqa` that no longer applies is always in scope; see the next rule.
- **"Pre-existing" is a diagnosis, not a disposition.** When a test or a lint is
  already failing before you touched anything, say so — attributing it correctly
  matters — and then spend a minute finding out whether it is *fixable*. Most
  are: a stale `noqa` for a rule nobody enabled, an import ruff wants regrouped,
  a test that fails only under `pytest -n` because it was written before the
  suite went parallel. Fix those in passing. Establishing that a failure is not
  yours is the beginning of the job, not the end of it.

  Leave one alone when fixing it is a real change — a behaviour decision, a
  risky refactor, or something the human should weigh — and then **say what you
  found and why you left it**, so the next agent inherits a diagnosis instead of
  repeating the investigation. What is not acceptable is a green-except-for-the-
  usual-two gate that everyone routes around: that is how a suite stops being
  read, and then a real regression hides in the noise nobody looks at any more.

  This rule exists because a `wreath-check` run reported the same two failing
  gates for a long time. One was five bench-task tests that fail only under
  `pytest -n`, because `wreath-bench` refuses to run beside competing workloads
  and the sibling xdist workers *are* competing workloads — the tests stub
  `subprocess.run` and never benchmark anything, so the guard was pure noise
  there and one fixture line fixed all five. The other was three lint findings,
  every one a dead directive or a misgrouped import. Both were mistaken for
  scenery for months.
## Traps that have already cost someone a day

Each of these was found the expensive way. They share a shape: **the tool reports
success, or the test passes, and the thing you wanted to happen did not happen.**
None is discoverable by reading the code you are changing.

- **`wreath mutant --limit N` samples the *head* of a file.** It takes the first
  N candidates in line order, so a bound pass over a long module never reaches
  code you appended to it — and it reports a clean-looking score for somebody
  else's function. Three separate sessions spent their whole window on unrelated
  pre-existing lines this way. **Use `--changed <ref>`**, which selects only
  lines changed against a git ref. `--only` works too, but its `operator@path:line`
  is where the operator *anchors*, not where the control reads: a keyword carries
  its *value*'s line, so line numbers read off the source match nothing. A
  selector that matches nothing now exits 2 rather than reporting a vacuous pass.
