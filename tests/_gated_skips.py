"""Detection and wording for the database-skip banner.

Tests gated on ``WREATH_TEST_POSTGRES_DSN`` went a long time without executing
once. When a container was finally started they found, among other things, a
defect in the *default* progress denominator that worked on its first call and
raised on every call after -- a shape no fake could model, and one a single call
could not have caught either.

The failure was not that nobody could run them. It was that skipping was
**invisible**: a skip reason lives in ``-rs`` output, which the default ``-q``
run never prints. So `conftest.py` prints a banner instead, using what is here.

Separate from `conftest.py` because a test has to import it, and `import conftest`
is ambiguous: this tree has eight of them and pytest puts each one's directory on
`sys.path`, so the name resolves to whichever was collected first.
"""

from __future__ import annotations

import shutil
from typing import Any

#: The variable every database-gated test names in its skip reason. Detection
#: keys on the reason text rather than on a list of files, so a suite added
#: tomorrow is counted without anyone remembering to register it here.
#: ``tests/test_gated_skips.py`` pins that convention, so the count cannot
#: quietly stop tracking reality.
DSN_ENV = "WREATH_TEST_POSTGRES_DSN"

#: Checked in order; the first one present is the one the banner suggests.
RUNTIMES = ("docker", "podman", "nerdctl")

_START = (
    "docker run -d --name wreath-test-pg -e POSTGRES_PASSWORD=wreath "
    "-e POSTGRES_USER=wreath \\\n"
    "  -e POSTGRES_DB=wreath_test -p 55432:5432 postgres:17-alpine \\\n"
    "  -c max_connections=200 -c fsync=off -c synchronous_commit=off"
)
_EXPORT = f'export {DSN_ENV}="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"'

#: Skip reports seen this process. A module-level list rather than state on the
#: config: there is exactly one controller per run, and `pytest_runtest_logreport`
#: is not handed the config, so threading it through every report would cost more
#: than it explains.
_SKIPPED: list[Any] = []


def container_runtime(which: Any = None) -> str | None:
    """The first container runtime on PATH, or ``None``.

    Takes *which* so a test can drive both branches without uninstalling docker.
    """
    lookup = which if which is not None else shutil.which
    for runtime in RUNTIMES:
        if lookup(runtime):
            return runtime
    return None


def gated_skip_count(reports: Any) -> int:
    """How many of *reports* skipped for want of the DSN.

    Derived, not declared. A skip's ``longrepr`` is ``(path, lineno, reason)``,
    and both a ``skipif`` mark and a runtime ``pytest.skip()`` land there --
    which is why the reason text is the seam rather than the mark.
    """
    total = 0
    for report in reports:
        longrepr = getattr(report, "longrepr", None)
        if isinstance(longrepr, tuple) and len(longrepr) == 3 and DSN_ENV in str(longrepr[2]):
            total += 1
    return total


def banner_lines(count: int, runtime: str | None) -> list[str]:
    """The banner body. Split out so a test can read it without a run.

    The two cases are deliberately different sentences. "You could run these"
    and "these cannot run here, and N assertions are unverified" are different
    situations, and reading identically would flatten the one that matters.
    """
    plural = "s" if count != 1 else ""
    if runtime is not None:
        return [
            f"{count} database-backed test{plural} did not run: {DSN_ENV} is unset.",
            "",
            "They are the only cover for behaviour a fake cannot model: parameter",
            "type inference, query plans, lock and timeout behaviour, DST boundaries.",
            f"You have {runtime} installed, so you can run them:",
            "",
            _START,
            _EXPORT,
        ]
    verb = "are" if count != 1 else "is"
    return [
        f"{count} database-backed test{plural} did not run: {DSN_ENV} is unset",
        "and no container runtime (docker, podman, nerdctl) is installed here.",
        "",
        f"So {count} assertion{plural} about real PostgreSQL behaviour {verb} unverified",
        "on this machine -- parameter type inference, query plans, lock and timeout",
        "behaviour, DST boundaries. Run them where a container runtime exists, or",
        "point at a database you already have:",
        "",
        _EXPORT,
    ]
