# 0011. Bitset routing is the default matcher

Date: 2026-07-27
Status: Accepted
Supersedes: the retired `protected decision-tree pruning` record, whose subject
is no longer the default backend.

## Context

Wreath shipped three matcher backends: a trie (`dtrouter.c`), a decision tree
(`DecisionRouteTable`), and a bitset table (`dtbitset.c`). The decision tree was
the default and had received the most tuning, including subtree pruning for
authorization-ineligible branches.

Its structural problem is parameter folding. A route with a parameter at a
position matches *every* branch at that position, so the parameter must be
folded into each — and the compiled form grows super-linearly in the route
count. Tuning the traversal does not change the exponent.

## Decision

The bitset table is the default (`src/wreath/_routing.py:9`), measured against
the decision tree on CPU **and resident memory** before the switch.

Inside one `(method, segment-count)` group, routes are indexed `0..N-1` in
priority order. Per segment position, a bitset per distinct literal value plus
one bitset of the routes carrying a parameter there. Matching intersects one
mask per position:

```
survivors &= literal[p][seg] | param[p]
```

A parameter contributes its bit at every position, so nothing folds into every
branch and the compiled form stays **linear** in the route count. Positions are
tested strongest-discriminator-first — the survival measure is
`sum(size²)/total²` over the branches a position induces — and the pass stops as
soon as one route survives, which keeps it competitive with the tree's early
descent exit on small tables.

## Consequences

- Route activation is O(1) in *total* route count, and O(group size / 64) within
  a group. Both are asserted by complexity probes (ADR 0022), not assumed.
- Authorization is one more AND, because access clauses are just more bitsets —
  not a separate pruning pass, which is what the decision tree needed.
- The winner is the lowest set bit, so priority ordering is bit ordering and
  tie-breaking is a count-trailing-zeros.
- Segment keys live in an open-addressed table indexed by raw bytes, so matching
  never builds a `PyUnicode` per segment.
- The other two backends remain, tested cross-backend. Keeping them is a cost
  paid deliberately: a matcher defect that reproduces on all three is a routing
  defect, and one that does not is a backend defect.

## Alternatives rejected

- **Keep the decision tree and tune it.** Rejected on the exponent: parameter
  folding is the growth, and no traversal change removes it.
- **Trie only.** Rejected: it descends per segment and cannot express the
  access-clause intersection as part of the same pass.
- **Widen the intersection with SIMD.** Not rejected on principle but currently
  moot: `nwords = (ncand + 63)/64` where `ncand` is routes *per group*, so a
  group needs 65 routes before the intersection is even two words wide.

## What would reverse this

A route table whose groups are routinely large enough for the word count to
matter, plus a measurement showing a wider intersection wins there. The
per-group arithmetic above is the number to re-check first.
