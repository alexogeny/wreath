# Test runner guidance

Read this before changing the test runner, its worker cap, collection strategy, timing history, or mutation sampling defaults.


## Repository layout

See [`repo-map.md`](../../repo-map.md) for a subsystem-oriented source, test, benchmark, and documentation map.

- `src/wreath/`: dependency-free framework core
- `tests/`: correctness and ASGI behavior tests. **Parallelism now pays on the
  default marks, and it did not used to.** The suite was ~3.5s, where a worker's
  worker's re-import of the native extensions cost more than it saved; it has
  since grown past 8,600 default-collected tests, and the trade inverted. The
  old measured curve flattened at six workers, but the grown 13,297-test tree
  moved the knee: three warm mutation-disabled runs averaged 27.727s ± 0.349s
  at six workers and 25.860s ± 0.282s at eight. Ten workers were unstable and
  no faster in the clean samples, so prefer `-n 8` over `-n auto` on a wide
  machine. **Prefer `uv run wreath test` for routine agent runs.** It applies
  `min(8, cpu_count)`, uses the native C dispatch engine, and adds the state map,
  timing history, and bounded mutation
  confidence; a bare `uv run pytest` stays the serial process you attach a
  debugger to. This recommendation is measured rather than aspirational: three
  interleaved warm fixed-workload runs with mutation disabled averaged
  25.903s ± 0.242s native against 47.152s ± 1.303s for pytest (1.820x), with
  602.417B ± 1.138B against 1,064.063B ± 6.423B retired instructions (43.4%
  fewer), measured 2026-08-25. The exact exclusions, reusable harness, and raw outputs live under
  `~/scratch/wreath/native-default/`.
  A first run
  after source changes also builds the mutation candidate catalog; its planning
  and compilation overlap the ordinary workers without collecting the whole
  suite again. One hundred ninety-two sampled controls are watched by default.

  Broad history-backed runs also stop making every xdist worker import every
  test module. `--collection auto` assigns each conventional module to one
  fresh worker once at least 80% have current broad-run timings, then greedily
  balances whole modules by those timings. Focused, cold-history, explicitly
  distributed, and cross-module `xdist_group` runs retain replicated dynamic
  scheduling. `--collection replicated|sharded` makes the A/B explicit. On an
  8,000-test synthetic corpus with 80 repeated immutable module objects, three
  interleaved eight-worker rounds measured 11.94s ± 1.11s replicated against
  6.62s ± 0.19s sharded. That is evidence for repeated collection only, not a
  claimed full-suite wall time.

  **The curve below does not reproduce, and the sample count is very nearly
  free.** It reads as though controls dominate the mutation phase; measured as an
  interleaved A/B against a pristine checkout of this tree — no DSN, three warm
  rounds per arm, mutation time taken as the same arm's as-typed run minus its
  `--mutant off` run — 192 controls cost **62.66s** of mutation against 48
  controls at **59.02s**. Three quarters of the controls for 3.6 seconds. The
  phase is roughly 55s of fixed cost — catalog build, baseline seal, the live
  probe window — plus about 0.02s per control.

  That was found by cutting the default to 48 on the strength of the old curve
  and then failing to measure the predicted saving, so it is written down here
  rather than left for the next person to rediscover: **to make this phase
  cheaper, attack the fixed cost, not the sample.** Cutting the sample trades
  123-124 gold files for 34-40 and buys noise. The historical numbers follow,
  and should be treated as describing a machine and a tree that no longer exist.

  After the slow-tail cleanup: three warm
  12-control runs averaged 25.899s ± 0.220s and produced nine gold files with one
  undecided control. Three warm 48-control
  runs averaged 33.155s ± 1.704s, produced 34 gold files, and decided all 48
  controls. Three warm 96-control requests averaged 45.401s ± 1.030s through
  complete mutation evidence against 29.792s ± 0.210s for the ordinary suite,
  produced 73-75 gold files and 86-88 kills, and left no control undecided.
  Concurrent catalog edits meant those runs contained 94-96 eligible controls.
  The 192-control setting was measured over three clean warm runs:
  complete mutation evidence averaged 76.142s ± 2.343s against
  32.048s ± 0.629s for the ordinary suite, produced 123-124 gold files and
  186-187 kills, and left no control undecided. Its 50-second post-suite ceiling
  is a ceiling rather than a routine charge; those runs used 42.3-46.4 seconds
  after the ordinary suite sealed.
  192 was verified on the grown 13,231-test
  tree: the ordinary suite took 29.44 seconds and the sample produced 125 gold
  files, 191 kills, no survivors, and one unreached refusal. A focused
  follow-up reached and killed that refusal in 0.11 seconds, adding the 126th
  gold file and leaving all 192 selected controls answered.

  Completed green tests feed isolated mutant children during the ordinary run;
  that live window does not spend the fifty-second post-suite tail budget. The
  full test pool starts alone. At ten percent of completed per-file blocks, the
  first test worker yields between cases and mutation receives one CPU slot;
  the split then ramps toward one tester and seven mutators on an eight-worker
  run, before mutation inherits the full pool at the baseline seal.
  Speculative passes are retried against the atomically sealed full baseline.
  The versioned history caches selection and invalidates
  it from source mtimes and sizes. `uv run wreath-check` likewise
  applies `min(6, cpu_count)` for its pytest gate. **The full measured curve lives in one place —
  `_devtools/tasks.py::_pytest_command`'s docstring — and that is the copy to
  read and to update.** Its timings predate the current suite size; re-measure
  there, off battery power, before changing the cap.
