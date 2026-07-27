# 0022. Complexity is a contract, asserted by probes

Date: 2026-07-27
Status: Accepted

## Context

A benchmark says how fast something is on one machine on one day. It does not
say whether a hot path is O(n) or O(n²), and the difference only appears at a
scale nobody benchmarks.

Worse, a benchmark regression is easy to explain away — a noisy box, a
background process, a different governor. A *shape* change is not.

Wreath already refuses performance claims from a single run (`AGENTS.md`), and
already learned that `cProfile` cannot decide these questions: it adds ~1–2 µs
per call, larger than most of this codebase's hot paths, and it produced one
accepted-then-worthless change by blaming CSRF's cost on token glue that was
then moved to C for no gain.

## Decision

Hot-path complexity is an asserted contract. `wreath-complexity-probe`
(`src/wreath/_devtools/complexity_probe.py`) measures doubling-size scaling
ratios and checks them against `docs/agents/complexity-baseline.json`.

A ratio of ≈1 is constant, ≈2 linear, ≈4 quadratic, ≈8 cubic. **Every probe
needs a same-size control**: the wheel finding meant nothing until the spread
arrangement showed 0.36 ms against 26 ms at identical timer count. Without a
control, a probe proves only that something is slow at size.

A **known-defective contract is marked, not silently tolerated**. A marked probe
still runs, records its observed and target degree with a written reason, and
fails if the subject gets *better* as well as worse — otherwise the mark rots
into permission and a landed fix leaves a lie in the file.

## Consequences

- Complexity claims in docstrings are checked. `bitset-router-static-scale`
  asserts O(1) in total route count; the guides may say so because a probe
  holds it.
- Ratios are far more robust than absolute timings on a shared machine, which is
  what makes this usable on a box running eight agents.
- Roughly forty probes exist across `extended`, `metal-http1`, `metal-host` and
  `web` groups. The gaps are the territory: a probe that passes is a covered
  claim, and the defects live where none looks.
- Nine shapes have been measured *linear* and recorded with the constant that
  bounds them. Those negative results are as valuable as the positives, because
  they stop the same candidate being re-investigated.
- The marked-defect state is what let the wheel's colliding-slot quadratic be
  recorded honestly instead of either hidden or rushed into a structural rewrite
  nobody had time to validate.

## Alternatives rejected

- **Benchmarks only.** Rejected: they measure the constant, not the exponent,
  and they are arguable in a way a doubling ratio is not.
- **Big-O in a docstring.** Rejected: prose that nothing checks is ADR 0024's
  pattern.
- **Fail the build on any superlinear result.** Rejected: it forces either a
  rushed structural fix or a silenced probe, and the marked state is the honest
  third option.

## What would reverse this

Nothing. The extension worth making is coverage — the probe list is the
documentation of what Wreath promises about cost, and it is incomplete.
