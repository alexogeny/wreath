"""Continuous integration for whichever forge the project actually lives on.

A scaffold that emits CI for one forge is a scaffold that has picked your host
for you. GitHub, GitLab, Codeberg and Gitea all run the same three checks on the
same project; what differs is the file name, the schema, and where the runner
gets an action from. So the checks are declared **once**, as a `CiPlan`, and each
forge gets a renderer that knows only how to spell them.

That split is the whole design, and it exists for one reason: a check added to
the GitHub file and forgotten everywhere else is invisible. Nobody reviews four
YAML files against each other. `tests/test_ci.py` asserts that every renderer
emits every command in the plan, so the omission fails a test instead.

**What can and cannot be proven here.** The generated *project* is executed by
the suite -- `tests/test_scaffold.py` runs its tests, and the commands below are
literally the ones it runs. The generated *pipeline* is not: no test suite can
start a GitLab runner. So these files are checked for being well-formed, for
carrying every command, and for naming commands that are known to work; they are
not checked by having run. Treat the first push as the real test, which is also
why nothing here reaches for a clever runner feature.

Four targets, three renderings:

* `github` -> `.github/workflows/ci.yml`
* `gitlab` -> `.gitlab-ci.yml`
* `codeberg`/`forgejo` -> `.forgejo/workflows/ci.yml`
* `gitea` -> `.gitea/workflows/ci.yml`

Forgejo and Gitea both speak GitHub's workflow schema, and the temptation is to
emit one file for all three. They differ in the one place that decides whether
the pipeline runs at all: a bare `uses: actions/checkout@v4` resolves against
`code.forgejo.org` on Forgejo and against `github.com` on Gitea, and either
default can be changed by whoever runs the instance. Both renderings therefore
name the action by full URL, so the file does not depend on how the runner was
configured.

For the same reason every job runs in an explicit `python:` container rather
than on a named runner label. `ubuntu-latest` is a GitHub convention; a
self-hosted Forgejo or Gitea runner offers whatever labels its operator chose,
and `docker` is the one that is conventional across instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FORGES",
    "Check",
    "CiPlan",
    "plan",
    "render",
]

#: Every `--forge` value, in the order they are offered. `codeberg` and
#: `forgejo` render identically; both are listed because people reach for the
#: name of the host they use, not the name of the software it runs.
FORGES = ("github", "gitlab", "codeberg", "forgejo", "gitea")

#: The container every non-GitHub forge runs in. Pinned to a minor version
#: rather than `3.14` floating, because a pipeline that starts failing on a
#: Tuesday because an upstream tag moved is the least debuggable kind.
CONTAINER = "python:3.14-bookworm"


@dataclass(frozen=True, slots=True)
class Check:
    """One job: a name, and the commands that make it pass or fail.

    Commands are separate strings rather than one shell script deliberately.
    Every forge here can show a failing *step*, and a five-line `script:` block
    that failed tells you only that something in it did -- which, when the thing
    that failed is `cp .env.example .env`, sends people to read the test output
    that was never produced.
    """

    key: str
    name: str
    commands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CiPlan:
    """Everything a forge needs to know, before any forge is chosen."""

    project: str
    checks: tuple[Check, ...]

    def commands(self) -> tuple[str, ...]:
        """Every command in the plan, in order. What a renderer must not lose."""
        return tuple(command for check in self.checks for command in check.commands)


def plan(project: str) -> CiPlan:
    """The checks a generated project ships with.

    Four, and deliberately not more. Each one is a command the suite already
    proves works against a generated project, which is the only reason to
    believe a file nobody can execute here:

    * `ruff check .` -- the generated `pyproject.toml` configures it.
    * `ty check` -- the package, its ports, adapters, and tests type-check.
    * `pytest` -- `tests/test_scaffold.py::test_the_generated_project_passes_
      its_own_tests` runs exactly this.
    * `wreath doctor preflight` -- and `test_the_generated_application_passes_
      its_own_preflight` runs exactly this.

    `cp .env.example .env` comes first in the two checks that import the
    application, because that is step one in the README and in the `next:` block
    `wreath new` prints. With `--database postgres` the settings carry no default
    DSN, so an import without it refuses by name -- correct, and a confusing
    first failure to meet in CI.

    **A `--frontend react` project gets no front-end job.** `npm run build` needs
    `web/src/api/`, which is generated by `wreath typegen` and gitignored, so the
    job is a real one with real setup rather than a line -- and it is not
    written. The README says to run typegen with `--check` in CI; that stays a
    sentence somebody acts on, and this docstring is where that gap is recorded
    rather than left to be discovered.
    """
    return CiPlan(
        project=project,
        checks=(
            Check("lint", "Lint", ("uv run ruff check .",)),
            Check("types", "Types", ("uv run ty check",)),
            Check("test", "Tests", ("cp .env.example .env", "uv run pytest")),
            Check(
                "preflight",
                "Preflight",
                (
                    "cp .env.example .env",
                    f"uv run wreath doctor preflight {project}.app:app --environ",
                ),
            ),
        ),
    )


def render(ci: CiPlan, forge: str) -> dict[str, str]:
    """Every CI file one forge needs, as `relative path -> content`.

    A mapping rather than a single string because nothing guarantees a forge
    wants one file, and a signature that assumes it would have to change the
    first time one does not.
    """
    try:
        renderer = _RENDERERS[forge]
    except KeyError:
        raise ValueError(
            f"unknown forge {forge!r}; supported: {', '.join(FORGES)}"
        ) from None
    return renderer(ci)


def existing(directory: Path, forge: str) -> list[str]:
    """The plan's paths that are already present under `directory`.

    Separate from `render` so a caller can refuse *before* writing anything,
    which is the same promise `wreath new` makes about a non-empty directory.
    """
    return sorted(name for name in render(plan("_"), forge) if (directory / name).exists())


# --- YAML --------------------------------------------------------------------


def _scalar(text: str) -> str:
    """One YAML scalar, always double-quoted.

    Quoting unconditionally rather than only when required. A command carrying a
    colon (`wreath doctor preflight shop.app:app`) is a legal plain scalar right
    up until somebody edits it to have a space after that colon, at which point
    the file parses as a mapping and the failure is a schema error naming a line
    nobody changed.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --- GitHub Actions ----------------------------------------------------------


def _github(ci: CiPlan) -> dict[str, str]:
    """`.github/workflows/ci.yml`.

    The one forge with a first-party uv action, so it gets it: `setup-uv` caches
    the download and picks the interpreter out of `requires-python`, which is
    two steps the container-based renderings have to spell out.
    """
    lines = [
        "name: ci",
        "",
        "on:",
        "  pull_request:",
        "  push:",
        "    branches: [main]",
        "",
        "permissions:",
        "  contents: read",
        "",
        "# Supersede an in-progress run when new commits land on the same ref.",
        "concurrency:",
        "  group: ci-${{ github.ref }}",
        "  cancel-in-progress: true",
        "",
        "jobs:",
    ]
    for check in ci.checks:
        lines += [
            f"  {check.key}:",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - uses: actions/checkout@v7",
            "      - uses: astral-sh/setup-uv@v9.0.0",
            "        with:",
            "          enable-cache: true",
            "      - name: Install",
            "        run: uv sync",
        ]
        for command in check.commands:
            lines += [f"      - run: {_scalar(command)}"]
        lines += [""]
    return {".github/workflows/ci.yml": "\n".join(lines).rstrip("\n") + "\n"}


# --- GitLab CI ---------------------------------------------------------------


def _gitlab(ci: CiPlan) -> dict[str, str]:
    """`.gitlab-ci.yml`.

    The one rendering that is not GitHub's schema. Every job shares one
    `default:` block, because GitLab runs each job in a fresh container and the
    install is identical -- repeating it per job is how the three copies drift.

    All three jobs sit in a single stage so they run concurrently. Staging them
    would serialise a lint that takes a second behind nothing at all.
    """
    lines = [
        "# Generated by `wreath new --forge gitlab`.",
        "",
        "default:",
        f"  image: {CONTAINER}",
        "  before_script:",
        "    - pip install --quiet uv",
        "    - uv sync",
        "",
        "# One stage, so the jobs run concurrently rather than queueing behind",
        "# a lint that takes a second.",
        "stages:",
        "  - check",
        "",
    ]
    for check in ci.checks:
        lines += [
            f"{check.key}:",
            "  stage: check",
            "  script:",
        ]
        lines += [f"    - {_scalar(command)}" for command in check.commands]
        lines += [""]
    return {".gitlab-ci.yml": "\n".join(lines).rstrip("\n") + "\n"}


# --- Forgejo (Codeberg) and Gitea --------------------------------------------


def _actions_in_container(ci: CiPlan, *, checkout: str, comment: str) -> str:
    """The body both Forgejo and Gitea use, differing only in where actions live.

    Written as one function rather than two near-copies on purpose: these two
    renderings agree on everything a check does and disagree only about a URL,
    and two files that agree until somebody edits one is exactly the drift this
    module exists to prevent.
    """
    lines = [
        comment,
        "",
        "name: ci",
        "",
        "on:",
        "  pull_request:",
        "  push:",
        "    branches: [main]",
        "",
        "jobs:",
    ]
    for check in ci.checks:
        lines += [
            f"  {check.key}:",
            # `docker` rather than a named OS label: a self-hosted runner offers
            # whatever labels its operator chose, and this is the conventional
            # one. The container then fixes the interpreter, so the pipeline
            # does not depend on what the host image happens to ship.
            "    runs-on: docker",
            "    container:",
            f"      image: {CONTAINER}",
            "    steps:",
            f"      - uses: {checkout}",
            "      - run: pip install --quiet uv",
            "      - run: uv sync",
        ]
        for command in check.commands:
            lines += [f"      - run: {_scalar(command)}"]
        lines += [""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _forgejo(ci: CiPlan) -> dict[str, str]:
    """`.forgejo/workflows/ci.yml` -- Codeberg, and any other Forgejo instance.

    The checkout action is named by full URL. A bare `actions/checkout@v4`
    resolves against whatever `[actions] DEFAULT_ACTIONS_URL` the instance was
    configured with, which is `code.forgejo.org` out of the box and is routinely
    changed; naming it outright means the file does not depend on that setting.
    """
    return {
        ".forgejo/workflows/ci.yml": _actions_in_container(
            ci,
            checkout="https://code.forgejo.org/actions/checkout@v4",
            comment="# Generated by `wreath new --forge codeberg`. Forgejo Actions.",
        )
    }


def _gitea(ci: CiPlan) -> dict[str, str]:
    """`.gitea/workflows/ci.yml`.

    Same schema as Forgejo, different default action host -- Gitea resolves a
    bare `uses:` against github.com -- so the URL is spelled out here too.
    """
    return {
        ".gitea/workflows/ci.yml": _actions_in_container(
            ci,
            checkout="https://github.com/actions/checkout@v4",
            comment="# Generated by `wreath new --forge gitea`. Gitea Actions.",
        )
    }


_RENDERERS = {
    "github": _github,
    "gitlab": _gitlab,
    "codeberg": _forgejo,
    "forgejo": _forgejo,
    "gitea": _gitea,
}
