# Working notes

Dated records of what was planned, measured and decided at the time. They are
not published (`wreath_docs.py` excludes this directory) and they are **not
maintained**: a plan is evidence about the day it was written, so correcting one
after the fact would destroy the thing it is for.

Read them for the reasoning and the measurements. Check the tree for the shape.

Two things are deliberately *not* here:

* **Proposals.** A plan for work that has not been done, and may never be, is
  not a record of anything. Those live outside this repository.
* **The `future/` set.** Sixteen application-platform proposals, written before
  the rename, for `wreath.services`, `wreath.jobs`, `wreath.messaging`,
  `wreath.webhooks`, `wreath.orm`, `wreath.replay`, `wreath.reactor` and the
  rest. Every one of them shipped, so the documents described the present tense
  as though it were the future, and they are gone.

## What has changed under them

Two entries, because each invalidates a phrase that recurs throughout.

**Architecture decision records are gone (2026-08-10).** They were a private-SaaS
habit in a web framework: twenty-seven numbered files stating rules the code
already had to obey, cited by number from a hundred places. The rules that are
contributor rules are in [`AGENTS.md`](../../AGENTS.md) now, stated where they
apply; the rest were restated in the module or guide they governed. A plan that
cites "ADR 0019" means refuse rather than half-wire; "ADR 0024" means a check
that silently has nothing to check; "ADR 0020" means a double is never more
capable than the real thing.

**`wreath._pure` is gone (2026-08-10).** Every accelerated feature used to have a
readable Python twin under a byte-for-byte parity contract, selected by
`WREATH_PURE=1`. The twin was a *proof mechanism*: when it was written, nothing
else could establish that the C was right. A suite of 15,000-odd tests can, and
comparing two implementations of ours is the weaker instrument anyway — both
halves can be wrong and agree, and the agreement then reads as proof. So the
twins are gone, `_core` is required, and correctness is anchored on the RFC, the
published vectors or the stdlib.

Two modules moved rather than died, and a note in a plan pointing at either is
pointing at a real thing under a new name:

| was | is | why it is not a twin |
| --- | --- | --- |
| `_pure/postgres.py` | [`_pgdriver.py`](../../src/wreath/_pgdriver.py) | `_native._postgres.Connection` subclasses it, `pipeline.c` reads fifteen module-level names out of it at init, and `resolve_offsets` resolves its `__slots__` byte offsets |
| `_pure/flight.py` | [`_flight_reference.py`](../../src/wreath/_flight_reference.py) | the readable NFR container codec, which the recorder has none of |

Four more were never twins and only lived in `_pure/` by filing error:
`compression.py` → `_compression.py` (a facade over CPython's own `zlib`/`zstd`),
`snapshot.py` → `_snapshot.py`, `response.py` → `_prepared.py`, and
`typegen.py` → `typegen/typescript_renderer.py`. The last three were each priced
against a C port and declined; the numbers are in their module docstrings.

So: a plan that says "the pure twin" is describing the tree as it was. A plan
that proposes *adding* one is describing a rule that no longer holds — see
[`AGENTS.md`](../../AGENTS.md) for the one that replaced it.
