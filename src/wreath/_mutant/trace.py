"""Which tests touch which lines, measured with PEP 669 rather than `settrace`.

This is the difference between a mutation tester people run and one they mean
to run. Without it, every mutant costs a full test suite; with it, a mutant
costs only the tests that execute the line it changed -- which for a control
buried in one subsystem is usually single digits.

Wreath is 3.14-only, so it may use the API that every cross-version tool is
denied. Two properties of `sys.monitoring` matter here and neither is available
from `sys.settrace`:

* **Per-location disable.** Returning `DISABLE` from the callback retires that
  one bytecode location permanently. Every file that is not a mutation target
  therefore costs exactly one callback for the whole run, and then nothing.
* **Nothing is paid for what is not watched.** The callback is only armed for
  lines that some operator actually proposed a mutation for.

The consequence to be honest about: a line reached only through a C extension
frame, a subprocess, or a thread that outlives the test is attributed wrongly or
not at all. `runner` treats an empty candidate set as UNREACHED and says so,
rather than silently scoring the mutant as survived.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from typing import Any

_TOOL_ID = 4
_TOOL_NAME = "wreath-mutant"


class LineTracer:
    """Records, per test, which watched lines that test executed."""

    def __init__(self, watched: dict[str, frozenset[int]]) -> None:
        self.watched = watched
        self.hits: dict[str, set[tuple[str, int]]] = {}
        self._current: set[tuple[str, int]] | None = None
        self._armed = False

    def begin(self, node_id: str) -> None:
        """Begin one engine-independent test attribution window."""
        self._current = self.hits.setdefault(node_id, set())

    def end(self) -> None:
        """End the current engine-independent attribution window."""
        self._current = None

    def pytest_runtest_setup(self, item: Any) -> None:
        self.begin(item.nodeid)

    def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
        self.end()

    def _line(self, code: Any, line: int) -> Any:
        lines = self.watched.get(code.co_filename)
        if lines is None or line not in lines:
            return sys.monitoring.DISABLE
        current = self._current
        if current is not None:
            current.add((code.co_filename, line))
        return None

    def start(self) -> None:
        if self._armed:
            return
        monitoring = sys.monitoring
        monitoring.use_tool_id(_TOOL_ID, _TOOL_NAME)
        monitoring.register_callback(_TOOL_ID, monitoring.events.LINE, self._line)
        monitoring.set_events(_TOOL_ID, monitoring.events.LINE)
        self._armed = True

    def stop(self) -> None:
        if not self._armed:
            return
        monitoring = sys.monitoring
        monitoring.set_events(_TOOL_ID, 0)
        monitoring.register_callback(_TOOL_ID, monitoring.events.LINE, None)
        monitoring.free_tool_id(_TOOL_ID)
        self._armed = False

    def index(self) -> dict[tuple[str, int], tuple[str, ...]]:
        """Invert the recording: (file, line) -> the tests that ran it."""
        inverted: dict[tuple[str, int], list[str]] = defaultdict(list)
        for node_id, pairs in self.hits.items():
            for pair in pairs:
                inverted[pair].append(node_id)
        return {key: tuple(sorted(value)) for key, value in inverted.items()}


class OutcomeRecorder:
    """The baseline pass/fail set.

    A mutant is only KILLED when a test that *passed at baseline* fails with the
    control removed. Without this, every already-broken test in the tree would
    read as a mutation being caught, which is the same failure AGENTS.md names:
    a check reporting safety it is not providing.
    """

    def __init__(self) -> None:
        self.failed: set[str] = set()
        self.passed: set[str] = set()

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when != "call" and report.outcome != "failed":
            return
        if report.outcome == "failed":
            self.failed.add(report.nodeid)
            self.passed.discard(report.nodeid)
        elif report.when == "call" and report.nodeid not in self.failed:
            self.passed.add(report.nodeid)
