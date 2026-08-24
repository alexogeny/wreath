"""Make a skipped database-gated suite impossible to miss.

Tests gated on ``WREATH_TEST_POSTGRES_DSN`` went a long time without executing
once. When a container was finally started they found, among other things, a
defect in the *default* progress denominator that worked on its first call and
raised on every call after -- a shape no fake could model, and one a single call
could not have caught either.

The failure was not that nobody could run them. It was that skipping was
**invisible**: a skip reason lives in ``-rs`` output, which the default ``-q``
run never prints. So this prints a banner.

It deliberately does not fail the run. A warning that breaks the build gets
suppressed, and a suppressed warning leaves you exactly where this started.

The detection and the wording live in `_gated_skips.py`, so a test can import
them without importing a conftest.
"""

from __future__ import annotations

from typing import Any

import pytest
from _gated_skips import (
    banner_lines,
    container_runtime,
    deselect_lines,
    deselected_by_mark,
    gated_skip_count,
    marks_in_expression,
    merge_worker_counts,
)

#: Skip reports seen this process. A module-level list rather than state on the
#: config: there is exactly one controller per run, and `pytest_runtest_logreport`
#: is not handed the config, so threading it through every report would cost more
#: than it explains.
_SKIPPED: list[Any] = []

#: Per-mark deselection counts. Filled locally by `pytest_deselected` (serial, or
#: on a worker) and by `pytest_testnodedown` (on an xdist controller, from what
#: each worker sent back).
_DESELECTED: dict[str, int] = {}


@pytest.fixture(autouse=True)
def _startup_audit_off(request: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the boot-time source audit out of every unrelated test.

    `hardening="warn"` is the shipped default, so every application a test
    starts re-scans the file its handlers were defined in. The scan costs about
    0.1ms per line and caches nothing: measured across this suite it ran 674
    times for 65.6 seconds of worker time, and 71% of that was re-reading files
    that had not changed since the scan before. A test fixture's own source is
    not what the audit exists to police.

    `WREATH_HARDENING` outranks the `hardening=` argument, which is what makes
    this cheap and also what makes it dangerous: set globally it would quietly
    neuter the tests that assert what each policy *does*, and they would keep
    passing. So they carry `pytestmark = pytest.mark.hardening` and this fixture
    leaves them alone -- the exemption is written in the file that needs it,
    where a reader of that file can see it.
    """
    if request.node.get_closest_marker("hardening") is None:
        monkeypatch.setenv("WREATH_HARDENING", "off")


def pytest_runtest_logreport(report: Any) -> None:
    """Collect skips.

    On the controller xdist re-emits every worker's report through this hook --
    the same path ``-rs`` output takes -- so the count covers the whole run
    however many workers ran it.
    """
    if report.skipped:
        _SKIPPED.append(report)


def pytest_deselected(items: Any) -> None:
    """Collect deselections, wherever collection happened.

    Deselected tests never produce a run report, so `pytest_runtest_logreport`
    cannot see them -- which is why the banner was blind to the whole
    marker-filtered population until now. This hook is the only place they
    surface.

    Under xdist it fires on the *workers*, not the controller: measured, the
    controller's `pytest_deselected` sees nothing, `terminalreporter.stats` has
    no ``deselected`` key, and pytest's own summary line silently drops the
    count. `pytest_sessionfinish` ships the numbers across.
    """
    if not items:
        return
    config = items[0].config
    excluded = marks_in_expression(getattr(config.option, "markexpr", ""))
    for name, count in deselected_by_mark(items, excluded).items():
        _DESELECTED[name] = _DESELECTED.get(name, 0) + count


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """On a worker, hand the deselection counts to the controller.

    ``workeroutput`` is xdist's channel back; the attribute exists only on a
    worker, so this is a no-op in a serial run where `pytest_deselected` already
    filled `_DESELECTED` on the process that will print.
    """
    output = getattr(session.config, "workeroutput", None)
    if output is not None:
        output["gated_deselected"] = dict(_DESELECTED)


def pytest_testnodedown(node: Any, error: Any) -> None:
    """Fold a finished worker's counts in, taking the max rather than the sum.

    Replicated xdist workers collect the whole suite and report the same number,
    so those counts take the maximum. Wreath's collection shards are disjoint,
    so their counts are summed instead; the worker plugin names that mode in
    ``workeroutput`` rather than making this conftest infer a scheduler.
    """
    output = getattr(node, "workeroutput", None) or {}
    counts = output.get("gated_deselected")
    if counts:
        if output.get("wreath_collection_shard"):
            for name, count in counts.items():
                _DESELECTED[name] = _DESELECTED.get(name, 0) + count
        else:
            _DESELECTED.update(merge_worker_counts(_DESELECTED, counts))


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: Any) -> None:
    """Print the banner once, after the run, and change nothing else.

    Guarded on ``workerinput`` so an xdist worker stays quiet: six copies of a
    warning is noise, and noise is the thing people learn to scroll past.

    Silent when the tests ran. A banner that is always there is the same failure
    wearing a different hat.
    """
    if hasattr(config, "workerinput"):
        return
    count = gated_skip_count(_SKIPPED)
    if not count and not _DESELECTED:
        return

    if count:
        terminalreporter.write_sep("=", "DATABASE TESTS DID NOT RUN", red=True, bold=True)
        for line in banner_lines(count, container_runtime()):
            terminalreporter.write_line(line, red=True)
        terminalreporter.write_line("=" * 79, red=True, bold=True)

    if _DESELECTED:
        terminalreporter.write_sep("=", "TESTS NOT COLLECTED", yellow=True, bold=True)
        for line in deselect_lines(_DESELECTED):
            terminalreporter.write_line(line, yellow=True)
        terminalreporter.write_line("=" * 79, yellow=True, bold=True)
