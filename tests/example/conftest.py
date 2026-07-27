"""Put the example package on the import path.

``example/`` is a top-level directory rather than part of ``src/wreath`` -- it
is an application built *on* wreath, not part of it, and it must never ship to
users who ``pip install wreath``. That choice is right and it costs this: the
package is not installed, so the tests add it to ``sys.path``.

The alternative is a ``pyproject.toml`` entry making it a workspace member,
which would remove this file. That decision is open; see the example's design
notes.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EXAMPLE = Path(__file__).resolve().parents[2] / "example"
if str(_EXAMPLE) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE))
