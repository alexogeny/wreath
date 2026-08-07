# Looking for the same mistake everywhere else

Status: **one change implemented and measured**, plus three places checked and
deliberately left alone. 2026-08-06.

This is a follow-on from [`hmac-key-schedule.md`](hmac-key-schedule.md), which
found that signing a token redid a piece of arithmetic on every request that
only ever produced one answer. That is a *shape* of mistake, not a one-off, so
this went looking for the same shape elsewhere.

## The one that was worth fixing: turning a response into JSON

### What was happening

When a handler returns a plain dictionary, Wreath does two things with it. First
it walks the whole structure in Python, converting anything JSON cannot
represent — a UUID, a timestamp, a `Decimal`, an enum, raw bytes — into
something it can. Then it hands the result to the native encoder, which walks it
again and produces the actual bytes.

The encoder is quick. Turning `{"id": 42, "ok": True}` into JSON takes it
**100 nanoseconds**. The preparation walk in front of it took **790**.

Eight times the cost of the work, to do nothing: there is no UUID, no timestamp
and no `Decimal` in that dictionary. The walk read every value, decided each one
was already fine, and built an identical copy.

### Why it was so expensive

The walk asks a series of questions, most specific first: is this a plain
scalar? an enum? a UUID? a `Decimal`? bytes? a date or time? a list-like thing?
…and finally, is it a mapping?

A dictionary — far and away the commonest thing a handler returns — is the
*last* case, so it fails all seven questions ahead of it before matching. And
the question that finally matches is the most expensive of the lot: `Mapping`
is an abstract type, and asking "is this a Mapping" costs 153 ns where asking
"is this exactly a dict" costs 28.

Measured individually, those eight questions accounted for 578 ns of the 790.

### What changed

Two lines, in effect: check for an exact `dict` and an exact `list` up front,
before the ladder of unusual types.

The word *exact* is doing real work there. The check is `type(value) is dict`,
not "is this dict-like". Anything that is merely a subclass of dict still falls
through to the original questions in their original order, so nothing about how
subclasses resolve can move. That matters because the order encodes real
decisions — a string-backed enum *is* a string, and must still be reduced to its
value rather than passed through — and this change deliberately cannot disturb
any of them.

### The numbers

The control first: the new code measured against itself, so we know what "no
difference" looks like. Eleven alternating rounds, both versions in one process.

| payload | A | A again | ratio |
| --- | --- | --- | --- |
| small dict `{id, ok}` | 247 ns | 245 ns | 1.01x |
| dict containing a list | 435 ns | 433 ns | 1.00x |
| list of 20 dicts | 5512 ns | 5496 ns | 1.00x |
| dict with UUID/date/Decimal | 2025 ns | 2057 ns | 0.98x |

Then the real comparison:

| payload | before | after | change | rounds won |
| --- | --- | --- | --- | --- |
| small dict `{id, ok}` | 809 ns | 251 ns | **3.22x faster** | 11/11 |
| dict containing a list | 1347 ns | 440 ns | **3.06x faster** | 11/11 |
| list of 20 dicts | 17037 ns | 5575 ns | **3.06x faster** | 11/11 |
| nested three deep | 3471 ns | 973 ns | **3.57x faster** | 11/11 |
| flat scalars only | 944 ns | 381 ns | **2.47x faster** | 11/11 |
| dict with UUID/date/Decimal | 2551 ns | 2033 ns | 1.26x faster | 11/11 |

The last row is the honest one. A payload that genuinely contains unusual types
still has to convert them, so it gains least — only the outer dictionary
short-circuits. Everything else is a payload that never needed converting at all.

Counted in CPU instructions rather than time — a measure that does not move when
the machine's clock does — the whole request stage that serialises a response
went from **59,973 to 51,946 instructions**, about 8,000 fewer per request.

### Why it is safe

The risk is that reordering the questions changes which one answers first for
some value. So the test does not check against hand-written expectations; it
runs **the original ladder, unreordered, as the oracle** and requires the new
code to agree with it — same value and same type — across empty dicts and lists,
nested structures, tuples, sets, frozensets, dict and list subclasses, and
payloads containing UUIDs, `Decimal`s, timestamps and raw bytes.

There is a separate test for the boundary itself: a string-backed enum, a dict
subclass and a list subclass must all still take the old path, because only an
exact `dict` or `list` may take the new one.

## Three places checked, and left alone

Negative results, so nobody spends the afternoon re-deriving them.

**Per-request scratch space (`request.state`).** It is touched 23 times in a
full-stack request — 10 reads of `request.state`, 7 writes, 6 lookups — which
looked like an obvious target. It is not: each operation costs about 20-45 ns
against a floor of roughly 19-28 ns for the plainest possible equivalent. There
is perhaps half a microsecond in it, and taking it would mean moving a very
widely used API into C. Not worth the trade today.

**The proxy-header hook.** Already does the right things: it builds one shared
index of the request's headers rather than scanning the list three times, and
the trusted-network checks are already native.

**The CORS hook.** Also already careful, with the reasoning written down in
place — it reads `request.method` rather than materialising the ASGI scope, and
it merges the `Vary` header rather than appending a second one.

In all three cases the code was already close to its floor. That is worth
recording: "we looked and there was nothing there" is a result, and an
unrecorded one gets re-investigated.

## How each of these was found

The same three tools, in the same order, both times:

1. **`wreath-cpu-probe`** counts CPU instructions instead of time, so its ranking
   does not wobble when the processor changes speed. It named the two biggest
   layers in a request: the middleware stack, and response serialisation.
2. **`wreath-tape-decomp`** broke the middleware stack down hook by hook, with a
   control arm establishing what counted as a real difference.
3. **Removing one piece at a time** and timing the whole request attributed the
   cost — and in both cases contradicted the obvious guess. In the CSRF hook the
   cost was not parsing cookies or generating random numbers; here it was not
   the JSON encoder.

The common thread in what they found is worth stating plainly, because it is
probably not exhausted:

> Both defects were work performed unconditionally that, in the common case,
> produced exactly its own input. Neither was a slow algorithm. One recomputed a
> constant; the other asked seven questions whose answer was always "no" before
> asking the one that mattered.
