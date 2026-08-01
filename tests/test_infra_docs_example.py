"""The worked example in `docs/guides/infra.md` is the command's real output.

A pasted terminal block is a claim about behaviour, and the only kind of claim
that rots without anybody noticing. So the block is executed here and compared
with the page.

Two lines are normalised before the comparison rather than asserted: the route
count and the ORM model count. Those move whenever somebody adds a route or a
model to the example, and failing this suite for that would attribute an
unrelated change to a documentation bug. Everything else -- every endpoint,
every pool bound, the whole subsystem table, every settings key and every gap --
is compared byte for byte.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guides" / "infra.md"
MEDIA_ROOT = "/tmp/camera-trap/media"

#: Exactly the variables the guide's command line sets, and nothing else, so the
#: `--environ` supplier is the documented one rather than the developer's shell.
ENVIRONMENT = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "PYTHONPATH": str(ROOT / "example"),
    "PYTHONWARNINGS": "ignore",
    "CAMERA_TRAP_DSN": "postgresql://camera_trap@db.internal:5432/camera_trap",
    "CAMERA_TRAP_MAX_WINDOW_DAYS": "90",
    "CAMERA_TRAP_SPECIES_CACHE_TTL": "300",
    "CAMERA_TRAP_MEDIA_ROOT": MEDIA_ROOT,
}

_COUNTS = (
    (re.compile(r"^  http  \d+ route\(s\), \d+ websocket route\(s\)$"), "  http  <counted>"),
    (re.compile(r"^    application tables  \d+ ORM model"), "    application tables  <counted>"),
)


def _normalised(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        for pattern, replacement in _COUNTS:
            if pattern.match(line):
                line = replacement
                break
        lines.append(line)
    return lines


def _documented_block() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(.*?)```", text, flags=re.DOTALL)
    for block in blocks:
        if block.startswith("Infrastructure inferred from camera_trap.app:app"):
            return block
    pytest.fail("docs/guides/infra.md has no ```text block with the inferred plan")


def test_the_guide_shows_what_the_command_actually_prints() -> None:
    result = subprocess.run(
        [
            sys.executable, "-m", "wreath._cli", "infra", "infer", "camera_trap.app:app",
            "--settings", "camera_trap.config:Settings=CAMERA_TRAP", "--environ",
        ],
        cwd=ROOT,
        env=ENVIRONMENT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert _normalised(result.stdout) == _normalised(_documented_block())


def test_the_worked_example_opened_no_socket() -> None:
    """The same run, with every way of reaching the network made to raise.

    The point is that inference *completes*, not that it declines to start, so
    the CLI is imported first and the connect paths are neutered afterwards --
    replacing the `socket.socket` class beforehand breaks `ssl`, which
    subclasses it, and the run would then fail for a reason that proves nothing.
    """
    program = (
        "import sys\n"
        "from wreath._cli import main\n"
        "import socket\n"
        "def refuse(*a, **k):\n"
        "    raise SystemExit('wreath infra infer opened a socket')\n"
        "socket.socket.connect = refuse\n"
        "socket.socket.connect_ex = refuse\n"
        "socket.create_connection = refuse\n"
        "socket.getaddrinfo = refuse\n"
        "sys.exit(main(['infra', 'infer', 'camera_trap.app:app', '--format', 'json']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        env=ENVIRONMENT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "camera_trap.app:app" in result.stdout
