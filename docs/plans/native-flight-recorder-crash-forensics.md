# Crash forensics: a ring that survives the process

Logging stage 7, and the recorder's last unfinished promise.
See [ADR 0025](../decisions/0025-a-log-record-is-a-ring-cell.md) for why a log
record is a ring cell, and [the logging plan](first-class-logging.md) for the
stage table this closes.

## The gap

Every cell the recorder publishes — completions, correlations, phase batches,
log records — lands in a ring allocated with `PyMem_Calloc`. That memory is the
process's. When the process dies badly the ring dies with it, and the records
that mattered most are the ones nearest the end: the last requests before a
segfault, the log line written immediately before an abort, the completion whose
handler took the interpreter down.

The projector drains the ring on an interval and hands what it finds to the
writer and the exporters, so a *clean* shutdown loses nothing. A crash loses
everything since the last drain, which is exactly the window a post-mortem is
about.

Three things were deliberately built to make this closable without a format
break, and they are what this plan spends:

- every cell carries its schema version in byte 0 and its kind in byte 1;
- every decode validates lengths against the buffer rather than trusting them;
- the recorder already owns a checksummed on-disk container (`WFR1`) with a
  reader that recovers a file whose tail was torn off.

## What ships

**1. A file-backed ring.** `TelemetryConfig.ring_path` maps the ring from a file
with `MAP_SHARED` instead of allocating it on the heap. The pages become the
kernel's, so a `SIGSEGV`, a `SIGKILL`, or an `abort()` leaves them intact on
disk. The file is self-describing: a one-page header carries the magic, both
versions, the ring geometry, the worker id, the pid, and the clock calibration,
so a decoder needs nothing from the dead process.

**2. An archival stream.** A ring holds `ring_records` cells and then wraps. For
history beyond that, the projector's drained cells are appended to the `WFR1`
file as `EVNT` chunks — a path `WFR1Writer.write_events` already implements and
nothing called. The archive and the ring file are complements, not alternatives:
the archive holds everything up to the last drain, the ring file holds what was
still in flight when the process died.

**3. A decoder.** `wreath.recording.read_ring_file` returns the cells in publish
order with the geometry, the clock calibration and the provenance around them,
and `wreath flight read` is the operator-facing form.

## What this is not

**It is not durability.** `MAP_SHARED` pages survive the *process*, because the
kernel holds them and writes them back. They do not survive a machine that loses
power or a kernel that panics, unless they have been written back first. A clean
shutdown `msync`s; nothing else does, and no promise beyond "the process died"
is made anywhere in the docs. Confusing the two would be the easiest possible
way to make this feature a lie.

**It is not tied to Forensic mode.** Capture slabs are; this is not. A crash is
worth reconstructing whether or not anyone armed request capture, and requiring
`Mode.FORENSIC` to get crash forensics would mean paying for slab pools and
redaction machinery to answer "what was it doing when it died". The ring file is
driven by a configured path, in any mode that has a ring at all.

**It does not change the wire format.** Cells in the file are the cells on the
ring, byte for byte. The header is new bytes in front of them, not a re-framing.

## The ring file

```
offset 0     header, one page (4096 B), so the cells start page-aligned
offset 4096  ring_records * 64 B of cells, in slot order
```

The header is two cache lines of fixed provenance followed by the two indices
the writer and reader actually move:

| offset | field | why a decoder needs it |
| --- | --- | --- |
| 0 | magic `WFRR` | it is this, and not something else |
| 4 | container version | refuse an unknown one rather than guess |
| 5 | schema version | the cells' format, checked against this build |
| 6 | flags | reserved |
| 8 | `ring_records` | how many slots follow |
| 12 | `cell_size` | refuse anything but 64 rather than misparse |
| 16 | `worker_id` | which worker's ring this was |
| 24 | `epoch_mono_ns` | a cell carries an offset; this is its origin |
| 32 | `epoch_unix_ns` | ... and this puts the origin on a wall clock |
| 40 | `created_unix_nano` | when the mapping was made |
| 48 | `pid` | whose crash this was |
| 64 | `head` | where the writer had got to |
| 72 | `tail` | where the reader had got to |

`head` and `tail` sit on their own cache line, away from the fixed fields, and
are mirrored by the writer and the drain as they move. Without them a decoder
can still read every slot, but it cannot tell a live cell from one the ring
overwrote a lap ago — and silently replaying a stale request is worse than
reporting a gap.

**The cost of mirroring is one relaxed store per publish**, and it is paid
whether or not a file is mapped: the mirror points at a worker-local scratch
word when there is no file, so the hot path has a store and no branch. A branch
would be the cheaper-looking choice and the wrong one — it puts a predictable-
but-real test on every publish to save a store to a line that is already hot.
Measured below.

## Reading it back

`read_ring_file` reports what it found rather than raising at the first problem,
because a file recovered from a crash is exactly where a strict reader is least
useful:

- cells outside `[head - ring_records, head)` are stale laps and are skipped;
- a cell that fails to decode is counted, not raised — one torn slot must not
  cost the other 8,191;
- a `head`/`tail` pair that could not have happened — tail past head, or a
  window wider than the ring — is clamped and flagged `cursors_inconsistent`,
  because the two are mirrored independently and a crash can land between them.

**The ring refuses when full; it does not overwrite.** That is worth stating
because it inverts the intuition a ring buffer invites. Once `head - tail`
reaches `ring_records` a publish is a counted `RING_FULL` drop, so a busy crash
loses the records *nearest the failure* rather than the oldest ones. This was
designed the other way round at first — with an `overwritten` count that turned
out to be structurally unreachable — and the correction is why the mirrored loss
counters exist: `ring_full_drops` is what says whether a file is the story or
the tail of it, and `wreath flight read` prints it before the records for that
reason.

## What a crash file can and cannot tell you

**It can name the request that killed you, by its absence.** A completion cell
is written when a request *finishes*, so the request in flight when the process
died is precisely the one with no completion. Its log records are on the ring
and carry its id, so the gap is legible rather than merely present.
`test_the_request_in_flight_when_it_died_is_the_one_with_no_completion` holds
that: two healthy requests complete, a third logs and then segfaults, and the
set difference between logged and completed ids is the doomed one.

**It cannot replay that request on its own, but it can check a replay.** A
completion cell carries a route id, a status and a duration — not the bytes. So
reproducing needs the request as it arrived, which is a `WTR1` transport
recording. The two compose: the ring file says *which* request, the recording
says *what* it was.

They join through the **sequence of log call sites**, because there is no shared
id to join on — a transport recording carries none, and adding one would mean
building live `WTR1` capture, which is a different feature. The sequence turns
out to be the better key anyway: it says not just *whether* the replay matched
but *where it stopped matching*, which is the question someone actually has.

`replay.reproduce_from_ring` — and `wreath flight replay <ring> <recording>
<target>` — re-drives the recording under a captured logging runtime and
compares. A replay that goes *further* than the file still reproduces: the crash
file stops where the process stopped, not where the request would have. It exits
1 on divergence, so "did my fix change the path?" is a question with a shell
answer.

**This compares site ids, so it is only meaningful against the build that
crashed.** A site's id is its position in the interned table, which is import
order — stable within one build, unrelated across two. Running it against a
different build does not fail loudly; it diverges at index 0, and the command
says so at exactly that index and nowhere else, so the hint cannot misdirect
someone whose replay parted company halfway through for real reasons.

**It infers the in-flight request rather than reading it.** The recorder has an
active table — the requests it believes are running right now — and that table
is still heap memory, so a crash takes it. Mapping it too would turn the
inference above into a direct read, and would also survive the case the
inference cannot cover: a request that died before writing any log record at
all. That is the obvious next piece of this work, and it is deliberately not in
this one; the active table is written per request with a seqlock, so mapping it
is a change to the request path rather than to an allocation, and it deserves
its own measurement.

## Stages

| # | Stage | State |
| --- | --- | --- |
| 1 | Ring-file header, mirrored in C and Python, with static asserts | landed |
| 2 | `MAP_SHARED` ring in `wreath_nfr_worker_new`, `msync` on close | landed |
| 3 | `read_ring_file` decoder, torn/stale/overwritten accounting | landed |
| 4 | `TelemetryConfig.ring_path`, server wiring, `wreath flight read` | landed |
| 5 | Archival `EVNT` stream from the projector's drain | landed |
| 6 | A real crash test: fork, publish, `SIGKILL`, decode the file | landed |
| 7 | `reproduce_from_ring` / `wreath flight replay`: does a recording retrace the crash | landed |

## Measurements

CPython 3.14, Linux x86-64, 2026-07-28,
`uv run python -m benchmarks.bench_logging --suite publish --trials 45`. Two
narrow questions, because only two things here touch a hot path.

**Does mapping the ring cost a publish anything?** A new arm publishes into a
mapped ring beside the heap one, in the same interleaved round, so the answer
does not carry the between-run drift a separate run would. Five runs:

| run | heap ring | mapped ring | A/A floor |
| --- | --- | --- | --- |
| 1 | 44.70 ns | 44.58 ns | 6.15 ns |
| 2 | 44.56 ns | 44.55 ns | 4.61 ns |
| 3 | 46.36 ns | 47.09 ns | 0.21 ns |
| 4 | 44.59 ns | 44.49 ns | 0.24 ns |
| 5 | 44.65 ns | 44.67 ns | 3.61 ns |

The difference is between −0.12 ns and +0.73 ns and never clears its own floor.
**Publishing into a file-backed mapping costs the same as publishing into the
heap**, which is the expected answer — a store to a dirty page is a store — and
now a measured one rather than an assumed one.

One caveat is worth writing down because it was seen rather than reasoned:
across seven total runs, one showed the mapped arm at +10.2 ns against a 0.87 ns
floor. That is the shape of periodic dirty-page writeback landing inside a timed
batch, not a per-publish cost — the median is unmoved and four of the five runs
above put the difference below a nanosecond. An operator should expect an
occasional excursion, not a tax.

**What does the mirrored cursor store cost?** It is one relaxed store per
publish, paid whether or not a file is mapped. Against the retained pre-mirror
runs on this machine (`benchmarks/results/logging_2026-07-28_native.json`:
`publish_log` 45.11 ns, native pack + publish 129.57 ns) the current build
measures 44.6–46.4 ns and 129.2–131.3 ns. The difference does not clear the
3–6 ns floor these runs report, so per the rule in
`src/wreath/_devtools/measure.py` it is **unresolved, not zero** — and it is a
cross-run comparison, which is weaker again. If it ever needs to be resolved,
the honest experiment is a scratchpad build with the store removed, A/B'd in one
round; that was not worth doing for a store to an already-hot line.

The mirrored loss counters are on the drop path, which was never fast, and are
not measured.
