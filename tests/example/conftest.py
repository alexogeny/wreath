"""Put the example package on the import path, in its own PostgreSQL schema.

``example/`` is a top-level directory rather than part of ``src/wreath`` -- it
is an application built *on* wreath, not part of it, and it must never ship to
users who ``pip install wreath``. That choice is right and it costs this: the
package is not installed, so the tests add it to ``sys.path``.

The alternative is a ``pyproject.toml`` entry making it a workspace member,
which would remove that half of this file. That decision is open; see the
example's design notes.

**The schema name is per worker, and that is not a nicety.** Every fixture in
this directory does ``DROP SCHEMA ... CASCADE`` then ``CREATE SCHEMA``, so six
xdist workers sharing one name delete each other's tables mid-test. What comes
back is not a recognisable isolation failure: PostgreSQL reports the losing
side as ``duplicate key value violates unique constraint
"pg_namespace_nspname_index"``, or the test simply finds an empty table and
fails an assertion about seeded rows. Measured before this was added: green
serially, six errors under ``-n 6``, and one intermittent 8-failure run
serially while another process was using the same database.

``CAMERA_TRAP_SCHEMA`` must be set *before* ``camera_trap.models`` is imported,
because ``schema=`` is fixed when each model class is built. Setting it here
works because pytest imports a directory's ``conftest.py`` before the test
modules that sit beside it.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_EXAMPLE = Path(__file__).resolve().parents[2] / "example"
if str(_EXAMPLE) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE))

#: ``"main"`` when pytest runs in one process; ``"gw0"``..``"gw5"`` under
#: ``-n 6``. Distinct per worker is the whole requirement -- the value is
#: otherwise arbitrary.
#:
#: Assigned, not ``setdefault``. The xdist controller imports this file during
#: collection and then spawns workers with its own environment, so a
#: ``setdefault`` here writes ``camera_trap_main`` in the controller and every
#: worker inherits it and no-ops -- six processes back on one schema, which is
#: precisely the bug this is here to prevent. Measured: 28 errors under ``-n 6``
#: with ``setdefault``, none with assignment.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
os.environ["CAMERA_TRAP_SCHEMA"] = f"camera_trap_{_WORKER}"

#: The object store's root, per worker and inside the temp directory, for the
#: same two reasons as the schema: it is start-up configuration read once at
#: import, and two workers sharing one root would write and delete each other's
#: uploads. Unset, it defaults to ``example/media`` next to the package, and a
#: test run would leave archives in the working tree.
_MEDIA = Path(tempfile.gettempdir()) / f"camera-trap-media-{_WORKER}"
_MEDIA.mkdir(parents=True, exist_ok=True)
os.environ["CAMERA_TRAP_MEDIA_ROOT"] = str(_MEDIA)

#: Long enough to satisfy the minimum, and fixed so the warning about the
#: public development secret does not fire on every test run. Its value is not
#: a secret in any sense that matters: nothing outside this process ever sees a
#: URL it signs.
os.environ["CAMERA_TRAP_MEDIA_SECRET"] = "camera-trap-test-presign-secret-0123456789"
