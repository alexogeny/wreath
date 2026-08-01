"""Put the tracking example on the import path, in its own PostgreSQL schema.

``example/`` is a top-level directory rather than part of ``src/wreath`` -- the
two packages under it are applications built *on* wreath, not part of it, and
neither must ship to anyone who ``pip install wreath``. That choice is right and
it costs this: they are not installed, so the tests add them to ``sys.path``.

**The schema name is per worker, and that is not a nicety.** Every fixture in
this directory does ``DROP SCHEMA ... CASCADE`` then ``CREATE SCHEMA``, so
several xdist workers sharing one name delete each other's tables mid-test. What
comes back is not a recognisable isolation failure: PostgreSQL reports the
losing side as ``duplicate key value violates unique constraint
"pg_namespace_nspname_index"``, or the test simply finds an empty table and
fails an assertion about seeded rows.

``TRACKING_SCHEMA`` must be set *before* ``tracking.config`` is imported,
because ``schema=`` is fixed when each model class is built. Setting it here
works because pytest imports a directory's ``conftest.py`` before the test
modules beside it.

Assigned, never ``setdefault``. The xdist controller imports this file during
collection and then spawns workers with *its own* environment, so a
``setdefault`` here writes ``tracking_main`` in the controller and every worker
inherits it and no-ops -- every process back on one schema, which is precisely
the bug this exists to prevent. That failure looks like the fix not working
rather than like a mistake in the fix.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_EXAMPLE = Path(__file__).resolve().parents[2] / "example"
if str(_EXAMPLE) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE))

#: ``"main"`` in one process; ``"gw0"``..``"gw5"`` under ``-n 6``. Distinct per
#: worker is the whole requirement -- the value is otherwise arbitrary.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
os.environ["TRACKING_SCHEMA"] = f"tracking_{_WORKER}"

#: Long enough to satisfy the minimum, and fixed so the warning about the
#: published development key does not fire on every test run. Its value is not a
#: secret in any sense that matters: nothing outside this process ever sees a
#: cookie it signs.
os.environ["TRACKING_SESSION_SECRET"] = "tracking-test-session-secret-0123456789ab"
