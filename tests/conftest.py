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

from _gated_skips import banner_lines, container_runtime, gated_skip_count

#: Skip reports seen this process. A module-level list rather than state on the
#: config: there is exactly one controller per run, and `pytest_runtest_logreport`
#: is not handed the config, so threading it through every report would cost more
#: than it explains.
_SKIPPED: list[Any] = []


def pytest_runtest_logreport(report: Any) -> None:
    """Collect skips.

    On the controller xdist re-emits every worker's report through this hook --
    the same path ``-rs`` output takes -- so the count covers the whole run
    however many workers ran it.
    """
    if report.skipped:
        _SKIPPED.append(report)


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
    if not count:
        return

    terminalreporter.write_sep("=", "DATABASE TESTS DID NOT RUN", red=True, bold=True)
    for line in banner_lines(count, container_runtime()):
        terminalreporter.write_line(line, red=True)
    terminalreporter.write_line("")
    terminalreporter.write_line(
        "Tests marked `network` are excluded by the default marker expression too; "
        "add -m '' to include them.",
        yellow=True,
    )
    terminalreporter.write_line("=" * 79, red=True, bold=True)
