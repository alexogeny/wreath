"""One gate for the tests that need `wreath._native._reactor`.

The metal tier is the io_uring reactor, and it is **Linux only** by
construction: `_reactormodule.c` and `reactor_wheel.c` reach for
`<linux/io_uring.h>`, `<sys/eventfd.h>` and raw `syscall()`. `setup.py` builds
that extension only on Linux, so that macOS and Windows can have the rest of
the accelerators -- `_core`, `_server`, `_postgres` and `_client` carry no
platform headers and no POSIX-only calls -- instead of failing the install at
the first `#include`.

The library was already built for its absence: `wreath._native.__init__`
resolves it through `try/except ImportError`, and `wreath.reactor` raises a
named error only when `timers="wheel"` is asked for explicitly. The *suite* was
not, and that is what this fixes -- without the extension, 78 tests failed
rather than skipped, which on a macOS runner would read as 78 defects instead
of one absent optional component.

Import-time, not fixture-time: a module-level `pytestmark` needs the answer
before collection, and by then a fixture has not run.
"""

from __future__ import annotations

import pytest

from wreath._native import _reactor

#: True when the metal tier can actually run here.
HAS_METAL = _reactor is not None

#: Put on a module (`pytestmark = requires_metal`) or a single test. Prefer the
#: narrower one outside `tests/reactor/`: those files are about the reactor and
#: have nothing left to run without it, but a file that merely contains a
#: metal-loop case still has tests that are worth running on a platform the
#: reactor never reaches.
requires_metal = pytest.mark.skipif(
    not HAS_METAL,
    reason="needs wreath._native._reactor (the metal tier is Linux-only)",
)
