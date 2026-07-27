# 0024. Name the failure: a check that silently has nothing to check

Date: 2026-07-27
Status: Accepted

## Context

This is the single most useful thing this project knows about itself, and it was
nowhere written down.

A check that fails is a good check. A check that passes because the thing it
examines is *absent, unreachable, or out of scope* is worse than no check at
all, because it reports safety while providing none — and it displaces the
check somebody would otherwise have written.

It has appeared at least nine times in this codebase, in materially different
shapes:

1. **A waiver against a disabled rule.** 49 `# noqa: BLE001` comments existed
   while `ruff`'s `BLE` rules were not enabled. Later, ten more were decorative
   because `BLE001` never fires on a handler that re-raises or calls
   `logging.exception`.
2. **A refusal that cannot fire.** Two `= ANY($1)` refusals that never worked,
   and an `on_late=reopen` refusal with no reachable case.
3. **A decorative assertion.** An `EXPLAIN` assertion whose subject was always
   absent.
4. **A fake more capable than the real thing.** `FakeConnection` scripted with
   rows no PostgreSQL would send, so thirteen tests passed over a read that had
   never worked (ADR 0020).
5. **A lint with something to say and nothing listening.**
   `wreath-native-boundary-lint` reports 74 findings, exits 1, is registered in
   `pyproject.toml`, and is absent from the gate's step list.
6. **A check the interpreter removes.** Eight module-level struct-layout asserts
   guarding the flight-recorder wire format vanish under `python -O`, and
   nothing in the repository tests that mode.
7. **A check whose scope excludes the defect.** The native/pure multipart parity
   test passes with a live interpreter-corruption bug, because both
   implementations return the same bytes and the divergence is in what happens
   on the way (ADR 0007).
8. **A test that asserts existence instead of behaviour.** The route table
   asserted `GET /session` existed; nothing ever asked it its question, so it
   reported `signed_in: false` to a caller holding a cookie the server had just
   issued.
9. **A linter proposing one.** `ruff` UP012 suggested `"A".encode()` → `b"A"` in
   a test whose entire purpose was to read the interpreter's cache — the
   substitution removes the read and leaves a literal compared to itself.

Note that the last one arrived *while writing the guard against the pattern*.

## Decision

The pattern has a name, and every check is designed against it.

- **Prove a check can fail.** Break the subject, watch it go red, restore. An
  assertion never observed failing is not evidence. Report the count proved.
- **Prove it in a scratchpad shadow tree**, never by reverting in place (ADR
  0023).
- **Assert the property, not the symptom.** "Foreign keys are emitted last" over
  "this particular ordering bug does not recur".
- **A waiver names what it buys.** A bare directive is itself a finding — the
  rule in `AGENTS.md` for native lints, and the same rule for `noqa`.
- **A derived case list asserts its own count and names.** A test parametrised
  from a generated corpus must pin what it expects, so an entry that stops being
  generated turns the suite red rather than quietly shrinking coverage.
- **A skip is announced.** `tests/conftest.py` prints a banner naming how many
  DSN-gated tests skipped; it never fails the run, because a warning that breaks
  the build gets suppressed.
- **Guard the guard.** `test_the_guard_can_see_a_real_corruption` drives a
  genuine corruption and asserts the helper notices, so a silent regression of
  the *checker* is red.
- **Mark rather than hide.** Where a defect is known and unfixed, record it as a
  marked contract that fails if the subject *improves* as well as regresses
  (ADR 0022) — otherwise the mark rots into permission.

## Consequences

- Writing a check costs more, because proving it can fail is part of writing it.
- Reports carry a count of assertions proved red-before-green. This is a
  deliverable, not a flourish.
- Some checks are deleted rather than fixed, when what they examined turned out
  never to exist.
- New tooling is designed with its own failure mode in mind first. The
  executable-docs floor has a coverage ratchet because one bad vocabulary entry
  would drop resolution to near zero *while still exiting 0* (ADR 0021).

## Alternatives rejected

- **Trust code review to catch it.** Rejected on the evidence: every instance
  above was reviewed and shipped. The pattern is invisible precisely because the
  check *looks* correct — it is well-named, well-placed, and green.
- **A lint for it.** Rejected as impossible in general; whether an assertion has
  a reachable subject is not decidable from syntax. Specific shapes are lintable
  and `RUF100` is proposed for the waiver case, but the general pattern needs
  the discipline.

## What would reverse this

Nothing. If the count of instances stops growing, that is this record working,
not this record becoming unnecessary.
