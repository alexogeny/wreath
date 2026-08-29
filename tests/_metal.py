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
