# Signing tokens stopped doing the same sum twice

Status: **implemented and measured**, 2026-08-06.

## The short version

Every time Wreath signed or checked a CSRF token, or verified a JWT, it did a
piece of arithmetic it had already done — and thrown away — on the request
before. Doing it once instead made signing about **1.5x faster** and JWT
checking about **1.6x faster**, and it made a full request through the CSRF
middleware about **9% faster**. Nothing about the output changed: the signatures
are byte-for-byte what they were.

## What was happening

Wreath signs tokens with HMAC, the standard way to prove a short piece of text
came from someone holding a secret. HMAC works in two halves. Before it looks at
your message at all, it mixes the secret key into two fixed-size blocks — one
for each half — and hashes those. Only then does the message get involved.

That key-mixing step depends *only on the key*. It has nothing to do with the
message, the request, or the time. And the key never changes: an application
sets one CSRF secret at startup and uses it for the life of the process.

Python's `hmac.digest` has no way to know that. It is a one-shot function — you
hand it a key and a message together, and it has no memory between calls — so it
redoes the key-mixing every single time. That is two of the four hashing steps in
every signature, recomputing the same answer from the same input, on every
request.

## What changed

Wreath now mixes the key in **once**, keeps the resulting half-finished state,
and copies that state per signature. The message is hashed, as before; the key
preparation simply is not repeated.

This is not a shortcut or an approximation. It is HMAC's own definition — the
key blocks genuinely are constant — and it is how every long-lived HMAC
implementation is written. The digest is identical, which the tests check
directly against Python's `hmac` rather than against Wreath's own previous
answer.

Two things were deliberately *not* done:

- **SHA-256 was not rewritten in C.** That looks like the obvious move and it is
  the wrong one. The hash Python uses is OpenSSL's, which drops into dedicated
  CPU instructions; a hand-written C version measured *slower* than the call it
  would have replaced. The waste was never the hashing. It was the repetition.
- **The 384- and 512-bit JWT algorithms were left alone.** They use a different
  block size, so they keep the old path. They measured **0.99x** — unchanged,
  which is exactly what an untouched thing should measure, and is a useful check
  that the harness was not simply reporting improvements everywhere.

## The numbers

Each build was measured in a fresh interpreter, with the two versions taking
turns round by round so that anything drifting on the machine hit both equally.

First the control — **the same build measured against itself**, to establish
what "no difference" looks like on this machine:

| | build A | build A again | ratio |
| --- | --- | --- | --- |
| CSRF: sign a token | 895 ns | 903 ns | 0.99x |
| CSRF: check a token | 1054 ns | 1037 ns | 1.02x |
| JWT: verify HS256 | 661 ns | 662 ns | 1.00x |
| whole request through CSRF | 9686 ns | 9703 ns | 1.00x |

Then the real comparison, nine alternating rounds:

| | before | after | change |
| --- | --- | --- | --- |
| CSRF: sign a token | 1372 ns | 929 ns | **1.48x faster** |
| CSRF: mint a new token | 1449 ns | 991 ns | **1.46x faster** |
| CSRF: check a token | 1527 ns | 1063 ns | **1.44x faster** |
| JWT: verify HS256 | 1094 ns | 670 ns | **1.63x faster** |
| JWT: verify HS512 (untouched) | 1667 ns | 1684 ns | 0.99x — unchanged |
| **whole request through CSRF** | **10772 ns** | **9808 ns** | **1.10x faster** |

The end-to-end saving is about a microsecond per request. That is small in
isolation and it is paid on *every* request that carries a session cookie or a
bearer token, which for most applications is every request that matters.

As a separate corroboration, `wreath-tape-decomp` — which measures the whole
middleware stack rather than one primitive — puts the CSRF hook at **+11.32us**
where it read **+14.83us** before, and the whole middleware stack at **+28.61us
(44.7% of the request)** where it read **+33.44us (48.7%)**. Those are single
runs rather than paired rounds, so they are supporting evidence for the table
above rather than a claim in their own right.

## Why it is safe

The one new way this could go wrong is remembering the wrong key — signing with
a stale secret after the application changed it, or worse, accepting a token
signed with somebody else's. So that is what the tests attack:

- The signature is compared against Python's `hmac` for keys of every awkward
  length: empty, one byte, exactly one block, one byte over a block (where HMAC
  replaces the key with its own hash), and 256 distinct byte values.
- Keys are switched back and forth repeatedly — A, B, A, C, B, A — because a
  cache that is merely stale survives a single alternation and fails a
  repeated one.
- A token minted under one secret must be refused under another.
- A JWT signed with the 384-bit algorithm must not verify as a 256-bit one.

**These tests were then deliberately broken to prove they work.** A version of
the extension was built with the cache rigged never to notice a key change; it
turned eight tests red, including several that already existed. The working
build turns them green again. A parity test that has never been shown failing is
not evidence of anything.

The address sanitizer reports no errors and no leaked memory attributable to the
new code.

## A note on caching key material

There is a rule in this codebase (ADR 0007) against keeping key material lying
around in process-global state, and the comment next to the random-number code
cites it when refusing to cache pre-drawn random bytes. It is worth being
explicit about why this is a different trade.

Cached randomness is unused key material that exists *only because it was kept*
— it is new exposure. The states kept here are a pure function of a secret the
application already holds in memory for the entire life of the process. Keeping
them reveals nothing that was not already sitting there.

## Where this came from

Not from a profiler. The repository's own tools pointed at it in stages:

1. `wreath-cpu-probe` counts CPU instructions rather than time, so it does not
   move when the machine's clock does. It showed the middleware layer costing
   roughly 132,000 instructions per request — more than everything else in the
   request put together.
2. `wreath-tape-decomp` broke that down by middleware and put CSRF at nearly
   half of it, three times its nearest rival.
3. Removing one piece of CSRF at a time — with a control arm to say what counted
   as a real difference — showed the cost was in signing and checking tokens, and
   *not* in the places one would guess: parsing the cookie was free, and
   generating the random nonce cost 173 ns.
4. Reading the C then showed why: the "native" signing routine was handing the
   actual work back to Python.

The last step is the general lesson. `security.c` and `jose.c` both looked like
native code and both, in the middle of their hot path, called back into Python's
`hmac.digest`. Nothing was wrong with that decision when it was made — the note
in the file explains it, and the reasoning still holds for the hash itself. What
it missed is that a one-shot function cannot remember anything between calls, and
this framework calls it with the same key forever.
