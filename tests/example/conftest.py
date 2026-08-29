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

#: A DSN that is never dialled, for the tests here that need the *application
#: object* and no database at all -- typegen reads the route table and the
#: annotations on it. `camera_trap.app` builds `app` at module import on
#: purpose, so an unset DSN fails there naming the variable rather than deep
#: inside a driver; that is right for the tooling and it means importing the
#: module is impossible without one. `tests/test_infra_docs_example.py` supplies
#: a placeholder for the same reason.
#:
#: Set here rather than in a test module for the same reason as
#: ``CAMERA_TRAP_SCHEMA`` above: ``camera_trap.config`` binds ``SETTINGS`` at
#: *its* import, so anything written after the first module imports it is not
#: read. Setting it inside the helper that builds the app looks like it works --
#: it does, run alone -- and fails once a sibling module is collected first.
#:
#: Guarded rather than ``setdefault`` because ``CAMERA_TRAP_DSN`` wins over
#: ``WREATH_TEST_POSTGRES_DSN`` in ``Settings.from_env``: writing it
#: unconditionally would point a real database-backed run at this dead address.
if not os.environ.get("CAMERA_TRAP_DSN") and not os.environ.get("WREATH_TEST_POSTGRES_DSN"):
    os.environ["CAMERA_TRAP_DSN"] = "postgresql://camera-trap@127.0.0.1:1/unused"
