# Test validity guidance

Read this when designing a regression test, test double, refusal assertion, generated corpus check, database plan assertion, or mutation follow-up.


## Tests that pass without proving anything

The same failure in test form. `AGENTS.md`'s rules above say what to write; these
say how a written test still manages to assert nothing.

- **A check that silently has nothing to check is the failure mode this whole
  section is named after.** Prove a check *can* fail: break the subject, watch
  it go red, restore -- in a scratchpad shadow tree, never by reverting in
  place. An assertion never observed failing is not evidence. Assert the
  property, not the symptom ("foreign keys are emitted last", not "this
  particular ordering bug does not recur"). A case list derived from a
  generated corpus must pin its own count and names, so an entry that stops
  being generated turns the suite red rather than quietly shrinking it. And a
  waiver names what it buys -- a bare directive is itself a finding.
- **A double is never more capable than the real thing.** Where a real
  implementation refuses -- an unencodable parameter, an unprepared statement, a
  type with no codec -- the double refuses identically. Prefer doubles that
  *derive* their refusals from the real implementation rather than restating
  them, so the two cannot drift.
- **Falsify the harness before trusting it.** Point `WREATH_TEST_POSTGRES_DSN` at
  a dead port and confirm the gated tests *fail* rather than skip; neuter the
  implementation and confirm the test goes red. Several suites have looked green
  while executing nothing, and a suite that runs in 0.17s usually is not.
- **A refusal test that asserts only the field name proves nothing**, because
  every refusal message contains the field name — so it passes whichever branch
  fired, including the fallthrough. Assert the distinct message text.
- **Centre a geometric test off the interesting case and it proves nothing.** A
  bounding-box superset property caught 48/72 bearings at latitude 60 and 29/72
  at the date line — and **0/72 at the equator**, where it was first written.
  Parameterise across the cases the maths actually distinguishes.
- **An index assertion needs enough rows.** On a handful the planner picks a
  sequential scan however indexable the predicate is, so assert the `EXPLAIN`
  plan over a realistic seed (4020 rows, in the case that found this), not the
  result set.
- **A mutant survivor is often redundant code, not a missing test.** Five
  sessions have now resolved one by *deleting* a clause the guard above it
  already subsumed. Two spellings of one condition is how they drift apart later,
  so deletion is frequently the better answer — but prove the redundancy rather
  than assuming it.
