---
description: Run reproducible local and continuous fuzz campaigns with mutation evidence, corpora, seeds and crash artifacts.
keywords: fuzz mutation corpus seed replay shrinking artifacts GitHub Actions CI
---

# Fuzzing Wreath

Wreath combines four different kinds of evidence. Mutation testing identifies
controls that ordinary tests can remove without objection. Gold replay reruns
the exact tests that killed controls. Python campaigns combine PEP 669 branch
feedback with semantic features and versioned grammar-aware generation. Native
campaigns compile standalone libFuzzer harnesses with ASan, UBSan and Clang
SanitizerCoverage. Both retain useful corpus entries and exact reproduction
artifacts; stable Python failures are additionally shrunk in-process.

No clean campaign proves that two programs are equivalent. Differential fuzzing
of a surviving mutant records bounded evidence; only a stable difference in the
target's semantic features or exception upgrades that mutant to killed.

## Local campaigns

Run every registered target with a fresh seed and ten-minute shared budget:

```bash
uv run wreath test --mutant sample --fuzz on \
  --fuzz-backend all \
  --fuzz-cases 50000 --fuzz-budget 600 \
  --fuzz-corpus .wreath/fuzz/corpus \
  --fuzz-artifacts .wreath/fuzz/artifacts \
  --report .wreath/fuzz/report.json
```

The report records the generated master seed, each target-derived seed and the
initial corpus manifest. Pass the master seed back with `--fuzz-seed` against
that same corpus snapshot to repeat the same generation schedule. A corpus that
has grown is a different schedule input even when the seed matches.
Use `--fuzz-replay-only` to execute built-in seeds and every persisted corpus
entry without creating new inputs:

```bash
uv run wreath test --mutant sample --mutant-samples 1 --mutant-budget 10 \
  --fuzz on --fuzz-replay-only \
  --fuzz-target xml-parser --fuzz-cases 10000 --fuzz-budget 60 \
  --fuzz-corpus .wreath/fuzz/corpus \
  --fuzz-artifacts .wreath/fuzz/artifacts \
  --no-history --report .wreath/fuzz/replay.json \
  tests/test_fuzz_targets.py
```

`--fuzz-target` is repeatable. With no explicit target, Wreath first uses the
source path and operator metadata from mutation results; when nothing matches,
it runs every registered target rather than reporting success over an empty
selection.

`wreath test` defaults to the Python backend so the routine suite does not pay
for instrumented native builds. The explicit `wreath fuzz` command selects both
backends. `--fuzz-backend native` runs every maintained target through a
standalone compiler harness. GraphQL, HTTP/1, HTTP/2, multipart, recorded HTTP
exchanges and XML all have ASan/UBSan/SanitizerCoverage coverage in addition to
their Python-guided targets. The native build never changes Wreath's importable
production extensions and can be reused only through its digest-checked
instrumentation manifest. That manifest also fingerprints the source tree,
harness and build scripts, so an executable from older source is refused.

Native execution has four independent bounds: allocated libFuzzer campaign
time, native build time, time for one input, and a supervising-process deadline.
The shared fuzz budget excludes builds; whole-second native allocations are
rounded down and an allocation below one second is refused. A target that ignores its own
deadline is terminated and reported as an infrastructure failure rather than
leaving a CI shard hung. Native reports record the resulting corpus size,
additions and content manifests directly from the engine's normalized corpus.
They leave the generated-input manifest unset because libFuzzer does not expose
its complete execution schedule; the separate corpus-addition manifest covers
only inputs retained since the campaign began, including newly staged seeds.

HTTP/1 and XML additionally use explicitly versioned structured strategies.
They generate valid messages, mutate fields without immediately destroying the
grammar, cross over compatible documents, derive input-aware dictionary tokens
and offer bounded structural shrink candidates before byte-level reduction.
Every hook consumes the recorded campaign RNG and has a strict output bound, so
the same seed and strategy version reproduce the same schedule.

## Filesystem contract

The corpus is content-addressed:

```text
<corpus-root>/<target>/<SHA-256>.input
```

The filename is the lowercase SHA-256 digest of the exact bytes. A mismatched
name, oversized entry, temporary file, or unexpected file in a target directory
is refused before the campaign starts.

Python and crash-isolated findings use:

```text
<artifact-root>/<target>/<SHA-256>/input
<artifact-root>/<target>/<SHA-256>/metadata.json
<artifact-root>/<target>/<SHA-256>/diagnostic.log  # worker crashes, when stderr exists
```

Differential mutant findings add a stable namespace derived from the mutation
identifier:

```text
<artifact-root>/mutants/<mutant-id-SHA-256>/<target>/<SHA-256>/...
```

Native findings retain the instrumented build and stable failure signature as
separate namespaces while keeping the minimized input itself content-addressed:

```text
<artifact-root>/<target>/<build-SHA-256>/<failure-SHA-256>/<input-SHA-256>/...
```

Rediscovering the same failure can therefore reuse its artifact even when the
generated original, command or diagnostic addresses differ. The same minimized
input under a different build, sanitizer failure class or determinism result is
retained separately instead of silently returning stale evidence.

`metadata.json` records the campaign seed, target, input digest, original and
minimized sizes, exception identity, determinism result, related source files,
and operator names. A worker signal, nonzero exit, or timeout retains the exact
active input from its journal and up to the worker's captured diagnostics. Such
an infrastructure or native crash is a finding, but it does not masquerade as a
stable semantic mutant kill.

For native findings, minimization is provisional until the minimized bytes are
replayed against the same instrumented harness. Wreath retains them only when
that replay still fails, and records whether a second replay reproduces the same
failure signature. Metadata also binds the finding to the build manifest, Python
ABI, platform and Clang identity. Otherwise the original crashing input remains
the artifact.

To replay a downloaded finding, read its target and seed from `metadata.json`,
copy `input` to `<corpus-root>/<target>/<input_sha256>.input`, then run the
replay-only command above for that target.

## Continuous fuzzing

`.github/workflows/fuzz.yml` has two trust and cost profiles:

- A relevant pull request restores the latest main-branch corpus read-only,
  evaluates Python mutations changed from the PR base, and replays every
  registered target. If the change has no Python source or yields no mutation
  candidate, it falls back to an eight-control sample so the smoke run never
  passes or fails merely because selection was empty. Pull requests never save
  corpus caches.
- The daily schedule and manual dispatch run four independently seeded shards
  for each target. At most two shards execute concurrently. Each shard gets
  50,000 primary cases and a four-minute wall-clock budget.

Every scheduled shard uploads two run artifacts:

- `fuzz-corpus-<target>-<shard>-<run-id>` contains its corpus snapshot for 14
  days, long enough for the merge job.
- `fuzz-results-<target>-<shard>-<run-id>` contains its JSON report and any
  minimized or crash findings for 90 days.

After all shards finish, even when one failed, `merge-corpus` validates every
content address and unions entries by target. It then retains the union of two
independently minimized sets: the smallest inputs preserving Python branch and
semantic features, and libFuzzer's `-merge=1` selection preserving native
SanitizerCoverage for all six targets. Taking their union prevents one feedback
domain from erasing evidence visible only to the other. The result is
downloadable as `fuzz-corpus-merged-<run-id>` for 90 days and is also saved as
the next run's restore cache.

Download a retained merged corpus with:

```bash
gh run download RUN_ID --name fuzz-corpus-merged-RUN_ID \
  --dir .wreath/fuzz/corpus
```

Caches are continuity hints, not durable storage. GitHub scopes caches by key,
version, and branch, removes entries that have not been accessed within its
configured retention window, and may evict older entries when the repository
exceeds its cache quota. The workflow's 90-day downloadable artifacts also
expire; repository settings may impose a shorter limit. Long-term corpus
retention therefore requires periodically downloading the merged artifact to
maintainer-controlled storage or committing a deliberately reviewed regression
seed to `src/wreath/_fuzz_targets/corpus/v1/`.

The workflow has only `contents: read`; it cannot update branches, releases, or
repository settings. Its concurrency group cancels superseded PR work but
serializes scheduled campaigns, and the campaign matrix limits parallel jobs to
two.
