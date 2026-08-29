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
