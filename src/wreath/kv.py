"""One bounded in-process key/value table, for everything that needed one.

A response cache, a session table, an idempotency ledger, a JWKS cache and a
claim ledger are five features that need the same small thing: values under a
key, a hard ceiling on how many, and a deadline. Written five times, the same
three decisions get re-made five times -- and they are the decisions that are
easy to get quietly wrong:

* eviction has to be **least recently used**, not insertion order, or a hot key
  is thrown away while a cold one survives;
* expiry has to be **lazy**, because a background sweep thread duplicates
  across workers and swallows its own failures;
* `len()` has to count what the table will still *return*, not what it happens
  to be holding, or it reports debris as occupancy.

```python
from wreath.kv import KV

sessions = KV(max_entries=10_000, ttl=1800.0)
sessions.set(session_id, payload)
payload = sessions.get(session_id)          # None once the deadline passes
```

**This is one worker's memory.** Enough on its own for a single-worker
deployment or a sticky load balancer; behind anything else it is a fast path in
front of a shared store rather than a substitute for one, because a second
worker's memory knows none of it. `wreath.store.PostgresStore` is the shared
half, and the two agree on the semantics that matter -- notably that a claim's
window opens on first write and a later write does not move it, which `set`
spells `keep_deadline=True`.

**Not synchronised.** Every caller is event-loop-local, exactly as the pure
`BoundedCache` this replaces was, and adding a lock would charge every reader
for a race no reader has. `wreath.queue` is the primitive for handing work
between threads.

The native table is open-addressed with a one-byte tag per slot, scanned 32
lanes at a time; `_native/simd.h` documents the encoding and `_native/kv.c` the
rebuild policy. `WREATH_PURE=1` selects an `OrderedDict` twin with identical
behaviour and the same counters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._native import _core

if _core is not None and hasattr(_core, "KV"):
    KV: Any = _core.KV
else:  # pragma: no cover - exercised by the WREATH_PURE parity run
    from ._pure.kv import KV


@dataclass(frozen=True, slots=True)
class Stats:
    """A point-in-time view of a table's activity."""

    hits: int
    misses: int
    evictions: int
    expirations: int
    size: int

    @property
    def hit_rate(self) -> float:
        """Hits as a fraction of reads, or 0.0 before the first read."""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


def stats(table: Any) -> Stats:
    """The counters `table` has accumulated, as one value.

    A function rather than a method so it reads the same on both arms without
    the native type having to build a dataclass in C. `size` is the live entry
    count, so it agrees with `len(table)` and not with `table.slots`.
    """
    return Stats(
        hits=table.hits,
        misses=table.misses,
        evictions=table.evictions,
        expirations=table.expirations,
        size=len(table),
    )


__all__ = ["KV", "Stats", "stats"]
