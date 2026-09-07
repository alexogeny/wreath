# Performance guidance

Read this before designing, measuring, benchmarking, or claiming a performance or complexity change.

- `benchmarks/`: equivalent competitor applications and benchmark tooling

## Measurement and benchmark rules

- **Hot-path complexity is an asserted contract, not a docstring.**
  `wreath-complexity-probe` measures doubling-size scaling ratios against
  `tools/baselines/complexity-baseline.json` -- ~1 constant, ~2 linear, ~4
  quadratic. **Every probe needs a same-size control**: without one it proves
  only that something is slow at size. A known-defective contract is *marked*,
  not silently tolerated -- a marked probe still runs, records its observed and
  target degree with a written reason, and fails if the subject gets better as
  well as worse, so the mark cannot rot into permission.

  **A probe proves a contract somebody already suspected.**
  `wreath-complexity-probe --discover` is the other direction: a static sweep of
  `src/wreath` for the shapes that are provably superlinear -- a linear
  operation inside a loop over something the loop does not shrink -- so a
  candidate can be turned into a probe or restructured before anyone measures
  it. The scanner reports only container kinds and reuse it can prove. Bounded
  or output-sized work carries an exact-code `complexity: allow` waiver with a
  checkable reason; `tools/baselines/complexity-discovery.json` retains only
  exceptional confirmed hotspots. The baseline carries exact source
  fingerprints: byte-identical files reuse their findings, while new or edited
  files are scanned and any scanner change invalidates the whole cache. Record
  a confirmed exception with `--update-discovery` and say why in the change.
  The two rules that make it readable were both learned by getting them wrong:
  membership against a `set`/`dict` is O(1) and is not a finding at all, and
  iterating `d.values()` *is* the loop rather than an extra linear op inside
  one -- reporting either buried every real finding under several hundred lines
  of scenery.
- Know what a request costs at the boundary. The native linters read one C
  function at a time and cannot see a crossing that spans modules, so
  `uv run wreath-request-trace` counts them for a whole request against a
  realistic app and attributes each to a lifecycle phase. The intended shape is
  that ingress, routing, authentication, and authorization stay native and
  Python is entered when a route is *activated*; `pre_activation` measures the
  distance from that. `tools/baselines/request-boundary-baseline.json` records where
  it stands. Growth there is a trade-off, not automatically a defect -- a
  feature can be worth crossings -- but it should be a decision someone made and
  wrote down, not drift. Re-record with `--update-baseline` and say why.
- A crossing count is not a cost. `uv run wreath-policy-decomp` prices first-class
  HTTP policy and `uv run wreath-decomp` prices everything else -- lifecycle
  stages, one ORM read, and the ns-per-frame constant that converts crossing
  counts into microseconds. Both report a measured A/A noise floor and refuse to
  attribute any delta that does not clear it; on a powersave governor, per-hook
  costs routinely do not, and "below noise" means unresolved, not zero.
- **Measure the thing before building the fix for it, and do not use cProfile to
  decide.** It adds ~1-2us per call, which is larger than most of this codebase's
  hot paths, and it has already caused one accepted-then-worthless change: it
  blamed CSRF's cost on token glue, the glue was moved to C, and nothing got
  faster. Ablate instead -- remove a piece, time the whole request. The harness
  and its rules live in `src/wreath/_devtools/measure.py`.
- **Price a loop before rewriting it: what does one step cost, and how many
  steps are there?** Both halves decide the answer, and getting either wrong is
  how an optimisation lands and does nothing. Every win here has been a loop
  with real length whose body was doing avoidable work, and the size of the win
  tracked how expensive the deleted work was per step -- interpreter bytecode
  per byte pays most, an out-of-line C call per element pays next, a single
  instruction per byte pays least. Every loss has been the opposite: a body
  already minimal, or a length that some earlier partition had already reduced
  to one.

  Two consequences worth stating outright:

  * **Prefer deleting work to widening it.** Replacing a per-byte Python loop
    with one C call over the whole buffer is a different order of magnitude
    from replacing a scalar C loop with a vector one. Reach for the second only
    where the first does not apply and the buffer is genuinely large.
  * **Reach for the primitive that exists.** `wreath_memmem` and the `simd.h`
    arms are already written, already differentially tested against a scalar
    definition, and already dispatch per call. A hand-rolled byte loop beside
    one of them is usually an oversight rather than a decision -- but say which
    it is in a comment, with the number, so the next reader does not re-litigate
    it.
- Keep `uv run wreath-native-lint` clean. It encodes complexity defects that were
  actually found here — front-deleted queues, rescans in incremental parsers,
  per-value imports, additive buffer growth. When a match is intentional, waive
  it in place with a reason (`/* native-lint: allow NC001 -- why it is bounded */`)
  rather than loosening the rule; a bare waiver is itself a finding.
## Benchmark policy

- Keep reusable scratch scripts under `~/scratch`, organized by task or session,
  so they survive cleanup and remain available for reproduction. Temporary
  directories are for generated scratch outputs and artifacts only, never for
  scripts.
- Record Python version, platform, server, event loop, concurrency, duration, and scenario.
- Warm up before recording.
- Compare equivalent response bodies and route behavior.
- Run framework comparisons on the same ASGI server and event-loop configuration.
- Measure Wreath's own server separately from framework-overhead comparisons.
- Report throughput together with median, p95, p99, errors, and memory where available.
- Label the stdlib load generator as a development tool; use an independent generator for publishable results.
- Keep raw result files and provide enough metadata to reproduce them.
