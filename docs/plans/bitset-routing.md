# Proposal: replace the decision tree with a survival-ordered bitset pass

Status: **promoted into the application policy router**. The measured bitset
implementation in `src/wreath/_native/policy_router.c` is now exposed as
`PolicyRouteTable`; `routing="policy"` is the only accepted spelling and this is
the only compiled routing implementation. The former decision-tree and trie
sources were removed after the measurements below selected the winner. These
numbers remain historical evidence, not alternate shipped modes.

Related: `docs/plans/profile-guided-hotspot-remediation.md` (section 2, which
explicitly says *do not combine that work with a new routing algorithm* — hence
this being separate), and its implementation report.

## The problem this solves

`dnode_build` in `src/neo/_native/dtrouter.c` folds every parameter route into
every literal branch, because a parameter matches any literal. That is correct,
and it is also why the compiled tree grows super-linearly with the parameter
fraction. Measured over generated route tables:

| routes | DT nodes | bitset words | ratio |
| --- | --- | --- | --- |
| 64 | 4,242 | 50 | 85x |
| 128 | 47,824 | 104 | 461x |
| 256 | 116,170 | 206 | 565x |
| 512 | 185,552 | 402 | 462x |

A 512-route table at 30% parameters compiles to roughly 400,000 tree nodes. That
is compile time and resident memory, and no pivot heuristic fixes it: a
[separate study](profile-guided-hotspot-remediation-report.md) found the shipped
heuristic is already within 0.63% of optimal at realistic shapes. The structure
is the problem, not the choice of pivot.

## The formulation

Keep the existing `trees[method][nseg]` dict partition, and keep the
`static_routes` fast path — both are exact O(1) lookups that no tree or bitset
can beat, and this proposal does not touch either. Replace only what happens
*inside* one (method, nseg) group, which today is the decision tree.

Compile, per group, over the routes in that group indexed `0..N-1`:

- `literal_mask[p][value]` — bitset of routes whose segment `p` is `value`
- `param_mask[p]` — bitset of routes whose segment `p` is a parameter
- `public_mask` — routes with a zero access clause
- `clause_mask[c]` — routes carrying distinct clause `c`

Routes are indexed in (specificity, registration order), so bit order *is*
priority order.

Match, in one pass:

```text
survivors = ALL
for p in order:                       # compile-time order, see below
    survivors &= literal_mask[p].get(seg[p], 0) | param_mask[p]
    if popcount(survivors) <= 1: break
eligible = public_mask
for c in distinct_clauses:            # K small, fixed at compile time
    if (c & ~caller) == 0: eligible |= clause_mask[c]
winner = ctz(survivors & eligible)
```

Parameters stop being special: they contribute their bit at every position, so
nothing folds and nothing replicates. Authorization is one more AND rather than
a separate pruning concept. Specificity and registration-order tie-breaking are
`ctz`, not a verify loop.

## Why the pass is ordered

A naive pass touches every position and loses to the tree on small tables,
because the tree stops descending early:

| | dict lookups | verify cmps | word ops |
| --- | --- | --- | --- |
| decision tree | 3.16 | 18.93 | 0 |
| bitset, naive | 4.67 | 0 | 24.73 |
| bitset, survival-ordered | 3.50 | 0 | 27.58 |

The order is free to choose at compile time, and the survival metric already
says which positions discriminate: `sum(size^2)/total^2` over the branches a
position induces, parameters counted into every branch. Order strongest first,
drop positions that cannot discriminate (all-parameter: the mask is all-ones and
intersecting is a no-op), and stop once one route survives. That recovers the
tree's early exit without its folding.

On small realistic tables (<=32 routes, parameter fraction <=0.2) this is 2.52
lookups vs the tree's 2.20, while removing 4.98 verify comparisons. At 50%
parameters the tree performs 62.6 verify comparisons per match and the bitset
none; at 512 routes, 74.9 versus none.

## Measured, on the real prototype

The prototype that became `PolicyRouteTable` was differential-tested against
the former decision-tree table: 25,000 probes over random tables mixing public
and protected routes with random caller masks, zero mismatches. The selected
implementation now owns the request path; the comparison table is no longer
shipped.

Instructions per match (`perf stat -e instructions:u`, empty-loop baseline
subtracted, pinned, 200k iterations, 5 segments) and resident memory of the
compiled table:

| routes | param | DT instr | BS instr | delta | DT rss | BS rss |
| --- | --- | --- | --- | --- | --- | --- |
| 64 | 0.0 | 1,359 | 1,171 | -13.9% | 8k | 8k |
| 64 | 0.2 | 4,105 | 3,307 | -19.4% | 156k | 76k |
| 64 | 0.35 | 5,053 | 3,975 | -21.3% | 1,528k | 72k |
| 64 | 0.5 | 5,771 | 4,356 | -24.5% | 3,796k | 72k |
| 256 | 0.35 | 5,867 | 4,133 | -29.6% | 9,480k | 140k |
| 256 | 0.5 | 6,980 | 4,512 | -35.4% | 17,804k | 148k |
| 512 | 0.35 | 6,306 | 4,250 | -32.6% | 14,448k | 244k |
| 512 | 0.5 | 7,514 | 4,723 | -37.1% | 20,156k | 264k |

The memory column is the headline: a 512-route table at 50% parameters costs the
tree **20 MB** resident and the bitset **264 KB** — 76x. At 256 routes it is
120x. That is the parameter-folding explosion, in bytes.

A large constant (~2,500 instructions) of every row is Python call overhead
common to both, so the structural difference is proportionally larger than the
delta column suggests.

### Keep the static hash path

Folding fully-literal routes into the bitmap is tempting -- it removes the
static/parameter split, and with it the hand-written fallthrough. It was built
and measured, and it costs **+99% to +142%** on all-static tables (1,362 -> 2,713
instructions at 16 routes; 1,376 -> 3,329 at 512), because one hash of
`/a/b/c` beats one hash per segment. It also uses *more* memory there (68k vs
4k). Literal-vs-parameter precedence is known at compile time -- `/me` beats
`/{user}` exactly when the path is `/me` -- so a full-path index answers it in
one hash, and the parameter layer is only paid for on a miss. Statics stay in a
dict.

### The fallthrough is required, and is not a wart

A static route the caller cannot reach must not shadow a parameter route it can:
with a protected `/me` and a public `/{user}`, an unauthorized request for `/me`
resolves to `/{user}` with `user="me"`, not a 403. That is deliberate --
ADR 0015 prunes authorization-ineligible branches so they neither consume
traversal nor expose structure, and explicitly rejected returning 404 for all
protected routes. `match(caller_mask)` means "the best route this caller can
use". The first prototype answered `None` there and the differential caught it
(263 mismatches / 15,000); a public-only differential had missed it entirely.

## What is still not known

- **The `static_routes` fast path dominates realistic tables.** Most real routes
  are fully literal, and those never reach either the tree or the bitmap. The
  win concentrates in parameter-heavy tables; a mostly-static application would
  see close to nothing on match time, and gain only compile time and memory.
- `N > 64` needs multiple words: cost becomes `O(positions x ceil(N/64))`.
  Groups are per (method, nseg), so N is usually small.
- Neo's own pipeline benchmark uses a small table and would likely show nothing.
- The prototype implements `classify` only; `match`, `resolve`, `probe`, and
  HEAD fallback are not built, and route registration conflicts are not checked.

## Wallclock

Measured on an idle box, `taskset -c 8-11`, 9 trials, medians, with the A/A
noise floor (the same implementation run twice) measured alongside so the deltas
can be judged against it:

| routes | segmax | param | DT | bitset | delta | A/A floor |
| --- | --- | --- | --- | --- | --- | --- |
| 64 | 5 | 0.0 | 128.8 ns | 110.1 ns | -14.5% | +0.01% |
| 64 | 5 | 0.3 | 313.4 ns | 294.9 ns | -5.9% | -0.10% |
| 256 | 5 | 0.3 | 390.9 ns | 322.6 ns | -17.5% | -0.08% |
| 512 | 5 | 0.3 | 393.0 ns | 328.4 ns | -16.5% | +0.05% |
| 512 | 6 | 0.5 | 563.0 ns | 429.0 ns | -23.8% | +0.12% |
| 2000 | 6 | 0.3 | 498.4 ns | 386.7 ns | -22.4% | -0.81% |

Every delta clears the floor by 7x or more, and the ordering agrees with the
instruction counts.

## SIMD and a summary bitmap: both inapplicable

Both were considered and rejected on measurement, not taste. They optimise the
mask-intersection loop, and that loop runs over `nwords = ceil(N/64)` where N is
the routes in one *(method, segment-count)* group -- not the whole table. The
dict partition above the bitmap has already shredded the table:

| table | groups | biggest group | nwords |
| --- | --- | --- | --- |
| 512 routes | 29 | 23 routes | 1 |
| 2,000 routes | 30 | 93 routes | max 2 |
| 10,000 routes | 30 | 521 routes | max 9 |

A 512-route table's largest group is 23 routes, so the survivors bitmap is a
single `uint64`. There is no large sparse bitmap to index: a summary bitmap would
be a one-bit index over one word, and AVX needs 4-8 words to fill a register.
Both optimise a loop that executes once. They would start to matter somewhere
past ~10,000 routes in a *single* method+segment-count group, which the partition
makes unlikely.

## Where the remaining headroom actually is

`perf annotate` puts the hot instructions inside `brt_classify` in the MaskMap
probe -- pointer chasing, not the AND. Two concrete levers, both now measured:

- **Array-of-structs for `MaskMap`: tried, and it does nothing.** The reasoning
  was that four parallel arrays (`keys`, `lens`, `hashes`, `slots`) make one
  probe touch four cache lines to read four fields of one entry, so a 32-byte
  entry -- with keys up to 15 bytes inlined, removing the dependent load into a
  separate key allocation -- should make it one line. Built, verified identical
  over 30,000 probes across the inline/heap boundary, and measured:

  | | instructions/match | RSS |
  | --- | --- | --- |
  | array-of-structs + inline keys | 4,156 | 276 kB |
  | structure-of-arrays (kept) | 4,127 | 232 kB |

  Slightly *worse* on both, and every wallclock delta sat inside the A/A floor.
  The premise was wrong: a group holds at most a couple of dozen routes, so the
  whole MaskMap is 1-2 kB and is L1-resident. Cache lines *touched* do not
  matter when there are no cache *misses*. Reverted.

- **`hash_bytes` is byte-at-a-time FNV-1a.** Path segments are short; a
  word-at-a-time hash would cut the loop. Largely moot now: the discriminating-byte
  keying below removes the hash entirely for 85-90% of lookups, and what remains
  is the fallback for literals no few bytes can separate.

The pattern across SIMD, the summary bitmap, and array-of-structs is the same:
each optimises something that the `(method, nseg)` partition has already made
small. That partition is doing more work than any of them could.

## Probe behaviour (measured)

Instrumented counters in `maskmap_get`, readable via
`BitsetRouteTable.probe_stats()` (reads and resets):

| routes | param | buckets/lookup | key compares/lookup | hit% | max probe |
| --- | --- | --- | --- | --- | --- |
| 64 | 0.3 | 1.059 | 0.593 | 59% | 3 |
| 256 | 0.3 | 1.000 | 0.586 | 59% | 1 |
| 512 | 0.3 | 1.000 | 0.615 | 62% | 1 |
| 2000 | 0.3 | 1.000 | 0.622 | 62% | 1 |
| 2000 | 0.5 | 1.008 | 0.513 | 51% | 4 |
| 512 | 0.1 | 1.272 | 0.720 | 72% | 6 |

The table is effectively collision-free: one bucket per lookup, max chain 6.
About 40% of lookups miss on an empty bucket without reaching a `memcmp`.

### Swiss-table control bytes: rejected

Abseil's design earns its keep by rejecting many buckets cheaply (SIMD over
control bytes) when probe chains are long. These chains are length **1**. There
is nothing to reject, and control bytes would be overhead on a probe that
already terminates on the first bucket. Immutability does remove tombstones,
resize state, and refcounting -- but none of those are costing anything either.

### Discriminating bytes: tried, and it works

The probe is already minimal, so the remaining per-lookup cost is not *finding*
the bucket -- it is that `hash_bytes` reads every byte of the segment
(byte-at-a-time FNV-1a) and then ~60% of lookups `memcmp` it again. Each segment
is read roughly 1.6 times.

What makes a cheaper key sound here, and would not in a general hash table: **the
scan already fully verifies the winning route's literals before accepting it**.
The mask lookup therefore does not need to be exact. It only needs to never drop
a true match. So it can key on the byte positions that actually discriminate the
literals at that position and let the existing verify reject false positives.
That removes both the full hash and the probe's memcmp.

Built (`disc_key` / `disc_choose` in `src/neo/_native/policy_router.c`). At compile
time each position greedily picks the byte offsets that split its literals
apart; the key is the segment length plus those bytes, packed into one integer
and compared as one integer. Positions whose literals need more than four bytes,
or that differ only past `BRT_MAX_DISC_OFF`, keep the exact keying, so the worst
case is bounded at the previous behaviour.

Measured with `benchmarks/bench_bitset_key.py`, `taskset -c 8-11`, 200k
iterations, 9 trials, medians of 4 interleaved runs per arm, raw JSON in
`benchmark-results-routing-key/`. The A/A floor (same build twice) is **±1.5%**
per row, wider than the 0.80% of the wallclock table above because this
benchmark cycles over many request paths and several groups. Both arms carry the
same instrumentation; the counters were measured at +0.01% median, i.e. free.

| vocabulary | routes | segmax | param | exact | disc | delta | memcmp/lookup |
| --- | --- | --- | --- | --- | --- | --- | --- |
| realwords | 64 | 5 | 0.3 | 378.2 ns | 372.3 ns | -1.56% | 0.69 -> 0.00 |
| realwords | 256 | 5 | 0.3 | 403.6 ns | 390.4 ns | -3.26% | 0.62 -> 0.00 |
| realwords | 512 | 6 | 0.3 | 477.9 ns | 460.0 ns | -3.73% | 0.62 -> 0.00 |
| realwords | 512 | 6 | 0.5 | 515.4 ns | 502.3 ns | -2.55% | 0.51 -> 0.00 |
| realwords | 2000 | 7 | 0.3 | 662.4 ns | 603.8 ns | **-8.85%** | 0.64 -> 0.00 |
| prefixed | 512 | 5 | 0.3 | 461.6 ns | 443.5 ns | -3.91% | 0.60 -> 0.00 |
| prefixed | 512 | 6 | 0.5 | 514.0 ns | 485.8 ns | -5.49% | 0.49 -> 0.00 |
| tenants | 512 | 5 | 0.3 | 455.5 ns | 445.8 ns | -2.15% | 0.60 -> 0.00 |
| hostile | 512 | 5 | 0.3 | 500.1 ns | 455.8 ns | -8.87% | 0.60 -> 0.00 |
| hostile | 2000 | 6 | 0.3 | 623.4 ns | 548.8 ns | **-11.97%** | 0.62 -> 0.00 |

Median -3.13% across all 16 shapes. The win grows with the route count, because
a bigger group tests more positions and so does more lookups per match, and the
hash is per lookup.

Three things the measurements decided, none of which were obvious in advance:

- **The vocabulary matters more than the route count.** The idea's value is
  entirely a function of whether a few bytes separate the literals at a
  position. Every route table generated by tying the segment pool size to the
  route count is secretly the `prefixed` case, because the filler is numbered;
  a table whose literals are actual words is a different measurement. Both are
  reported above.
- **One or two bytes are not enough.** The first cut demanded separation within
  two offsets and fell back to the exact keying otherwise. That is bimodal: -8.79%
  where it engaged, but **+3.7%** where it did not, because the fallback still
  pays for the branch. Numbered segments (`resource-137`, `tenant-42`) need three
  or four bytes and were exactly the cases that fell back. Going to four offsets
  turned `hostile` from +2.29% into -10.55%.
- **Reading bytes a position does not need is not free.** Unconditionally
  reading four bytes cost `realwords` more than half its win (-8.85% -> -4.43%),
  since most word-like literals separate on one byte. `disc_key` therefore has a
  two-byte and a four-byte form and branches on the width; build and lookup both
  route through it so the two can never disagree.

Cost: the greedy search runs once per position per group at compile. A
2,000-route `hostile` table's group build goes 1.33 ms -> 3.18 ms; `realwords`
at 2,000 routes goes 1.43 ms -> 1.50 ms. Milliseconds once, against the tree's
tens of megabytes.

Two shapes do not win: the synthetic `words` vocabulary at 512/0.5 (+1.24%) and
2,000 routes (+1.87%), both inside or beside the ±1.5% A/A floor. Both are
positions that fall back to the exact keying, and both are generator artifacts
(numbered filler words), not shapes a real route table produces.

Parity: 1.5M differential probes against `DecisionRouteTable` over vocabularies
chosen to stress the offset search -- long shared prefixes, literals differing
only past the offset cutoff, mixed and single-character lengths, and multi-byte
UTF-8 -- with zero mismatches, plus the existing suite. 85-90% of lookups take
the discriminating path; memcmp per lookup falls from ~0.60 to ~0.05.

The pattern holds that the `(method, nseg)` partition has already made most
things small -- but this one is per *lookup*, and the partition does not make
lookups cheaper. That is why it is the one that paid.

## What promotion took

Done, and two real bugs surfaced on the way -- both found by widening a
differential, neither by reading the code:

- **The prototype's `classify` was `match`.** It returned `(handler, params)`
  and took a caller mask; the real `classify(method, path)` returns
  `(classification, public_match | ticket | None)`. Renamed, with the real
  `classify`/`resolve`/`probe` built on top. `classify` is now `match` with
  `caller_mask=0` that accumulates the rejects into a ticket -- the same scan.
- **`BRoute` had no method field**, so groups were keyed on segment count alone
  and GET and POST routes were matched against each other. Invisible while the
  differential used one method; 2,383 mismatches the moment it used two.

Also added: HEAD fallback, duplicate/conflicting-route detection matching the
shipped wording (`duplicate route` for static, `conflicting route` for a
parameter-shape clash), and verify-before-access ordering so a route that does
not match the path can never reach a ticket.

Parity: `decision` vs native bitset vs pure bitset agree on `match`, `classify`,
`resolve`, and `probe` across 16,000 randomised probes with mixed methods, HEAD,
protected routes, and random caller masks. Both bitset backends are registered in
`tests/_routing_impls.py`, so the existing parity suite covers them in native and
`NEO_PURE=1` modes.

## If this is implemented

Do it behind the existing routing-mode seam, with the decision tree retained and
differential-tested against it, exactly as `decision` was introduced alongside
`legacy`. The invariants that must hold, from the remediation plan: route
specificity, registration-order tie-breaking, HEAD fallback, authorization
tickets, and path-parameter ownership.

Then measure on an idle machine with the A/A noise floor established first — it
was 0.80% on the development box, and any claim must clear it.
