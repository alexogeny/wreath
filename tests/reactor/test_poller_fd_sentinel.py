from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
from _metal import requires_metal

#: Every test here drives the metal loop, so the whole module goes.
pytestmark = requires_metal


REFUSALS = {
    "recv-buffers-not-a-power-of-two": {"WREATH_METAL_RECV_BUFFERS": "24"},
    "connection-capacity-out-of-range": {"WREATH_METAL_CONNECTION_CAPACITY": "3"},
    "operation-capacity-out-of-range": {"WREATH_METAL_OPERATION_CAPACITY": "3"},
    "trace-mode-not-zero-or-one": {"WREATH_METAL_TRACE": "yes"},
}

_PROGRAM = textwrap.dedent(
    """
    import gc, os, sys

    def open_fds():
        alive = []
        for fd in (0, 1, 2):
            try:
                os.fstat(fd)
                alive.append(fd)
            except OSError:
                pass
        return alive

    before = open_fds()
    from wreath.reactor import EventLoop
    try:
        EventLoop(native_loop=True, timers="wheel")
    except Exception:
        pass          # the refusal is the point; the fds are what we assert on
    gc.collect()
    after = open_fds()

    # stderr, so the report survives a closed stdout.
    print(f"{before}|{after}", file=sys.stderr)
    sys.exit(0 if before == after else 1)
    """
)


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_a_refused_poller_does_not_close_a_standard_descriptor(name, tmp_path):
    env = {**dict(os.environ), **REFUSALS[name]}
    result = subprocess.run(
        [sys.executable, "-c", _PROGRAM],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert result.returncode == 0, (
        f"a refused poller ({name}) closed a standard descriptor: "
        f"before|after = {result.stderr.strip()}"
    )


def test_an_uninitialised_poller_does_not_close_a_standard_descriptor(tmp_path):
    program = textwrap.dedent(
        """
        import gc, os, sys
        from wreath._native import _reactor

        def open_fds():
            alive = []
            for fd in (0, 1, 2):
                try:
                    os.fstat(fd)
                    alive.append(fd)
                except OSError:
                    pass
            return alive

        before = open_fds()
        poller = _reactor.ReactorPoller.__new__(_reactor.ReactorPoller)
        del poller
        gc.collect()
        after = open_fds()
        print(f"{before}|{after}", file=sys.stderr)
        sys.exit(0 if before == after else 1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert result.returncode == 0, (
        "an uninitialised poller closed a standard descriptor: "
        f"before|after = {result.stderr.strip()}"
    )
