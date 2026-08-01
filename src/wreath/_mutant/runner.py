"""Build the mutants, learn which tests reach them, and fork one child each.

The shape of a run:

1. **Import** every module that will be mutated, so the operators can ask the
   live objects real questions -- does this keyword have a default? is this
   constant a compiled pattern? -- instead of guessing from syntax.
2. **Scan** each module's AST and compile the replacement code object for every
   mutation *in the parent*. A mutant that cannot be compiled or applied is
   found here, before any test runs, and reported as this tool's error rather
   than as the suite's failure.
3. **Warm** the interpreter by collecting the test suite. Collection imports
   every test module; after this, a forked child inherits all of it and pays
   nothing to import it again.
4. **Baseline** in a forked child, under the line tracer: which tests pass, and
   which of them touch each mutation site.
5. **One fork per mutant**, running only the tests that reach it.

`fork` is why this is cheap and also why it is Linux-first. The alternative --
rewrite the file, start a new interpreter -- costs the whole import graph per
mutant, and in a repository this size that is the difference between a coffee
and an afternoon.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import json
import os
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .model import Mutation, Outcome, Report, Site, Verdict
from .operators import Candidate, scan, tag
from .patch import (
    CodePatch,
    PatchError,
    PolicyPatch,
    ValuePatch,
    compile_module,
    find_code,
    transform_module,
)
from .trace import LineTracer, OutcomeRecorder

#: Tests a single mutant may run before the runner declines and says why. A
#: control on the hot path -- `AuthRequirement.access_level` is consulted for
#: nearly every request -- legitimately selects most of the suite, and declining
#: those would silently exclude the controls that matter most.
DEFAULT_MAX_CANDIDATES = 4000

#: Seconds a single mutant gets. A control removal that makes a test hang is a
#: real outcome, but it must not hold the run hostage.
DEFAULT_TIMEOUT = 60.0

_PYTEST_BASE = ("-q", "--no-header", "-p", "no:cacheprovider", "-p", "no:randomly")


@dataclass
class Plan:
    """Everything decided before a single test has been run."""

    mutations: list[Mutation] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    watched: dict[str, set[int]] = field(default_factory=dict)
    whole_file: set[str] = field(default_factory=set)
    """Files whose every line is watched, because a mutation there runs at
    import time and cannot be attributed to a line a test executes."""

    watch: dict[str, tuple[int, ...]] = field(default_factory=dict)
    """Mutation id -> the lines whose execution selects a test for it."""

    sources: list[str] = field(default_factory=list)


def module_name_for(path: Path) -> str | None:
    """Dotted name of a file, by walking up while `__init__.py` exists."""
    parts = [path.stem]
    parent = path.parent
    while (parent / "__init__.py").exists():
        parts.append(parent.name)
        parent = parent.parent
    if len(parts) == 1 and path.stem != path.name.removesuffix(".py"):
        return None
    return ".".join(reversed(parts))


def discover(roots: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "__main__.py":
                continue
            files.append(path)
    return files


class ChangedUnavailable(RuntimeError):
    """`--changed` was asked for where the answer cannot be computed."""


def changed_lines(repo: Path, ref: str) -> dict[str, set[int]]:
    """The lines that differ from `ref`, per repository-relative path.

    `--limit` takes the *first* N mutations, and mutations are ordered by line,
    so a bound spends its whole budget on the top of the file. New work is
    appended, which makes the one bound the tool had unable to reach the one
    code a run is usually about. This answers "the lines I just wrote" directly.

    An untracked file is entirely new, so every line of it counts; `git diff`
    says nothing about a file it has never seen.
    """
    import subprocess

    def git(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True, text=True, check=False, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ChangedUnavailable(f"could not run git: {error}") from error
        if completed.returncode != 0:
            raise ChangedUnavailable(
                f"git {' '.join(args)} failed: {completed.stderr.strip() or 'no output'}"
            )
        return completed.stdout

    lines: dict[str, set[int]] = {}
    current: str | None = None
    for line in git("diff", "--unified=0", ref).splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@ ") and current is not None:
            # `@@ -a,b +c,d @@`; `d` defaults to 1 and may be 0 for a deletion.
            span = line.split("+", 1)[1].split(" ", 1)[0]
            start, _, count = span.partition(",")
            length = int(count) if count else 1
            if length:
                lines.setdefault(current, set()).update(
                    range(int(start), int(start) + length)
                )
    for path in git("ls-files", "--others", "--exclude-standard").splitlines():
        if path.endswith(".py"):
            lines[path] = set(range(1, 1_000_000))
    return lines


def build_plan(
    roots: Sequence[Path],
    repo: Path,
    *,
    operators: Sequence[str] = (),
    only: Sequence[str] = (),
    changed: str | None = None,
) -> Plan:
    """Compile every mutation this run will attempt."""
    plan = Plan()
    seen: dict[str, int] = {}
    touched = changed_lines(repo, changed) if changed is not None else None
    for path in discover(roots):
        name = module_name_for(path)
        if name is None or name.startswith("wreath._mutant"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, ValueError) as error:
            plan.errors.append((str(path), f"unreadable: {error}"))
            continue
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001 - a target that cannot be
            # imported contributes no mutants; naming it is the whole point, and
            # an optional-dependency ImportError must not end the run.
            plan.errors.append((name, f"not importable: {type(error).__name__}: {error}"))
            continue
        relative = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
        if touched is not None and relative not in touched:
            continue
        plan.sources.append(relative)
        scopes = tag(tree)
        baseline_code = compile_module(tree, str(path))
        for candidate in scan(tree, name):
            if operators and not any(candidate.operator.startswith(p) for p in operators):
                continue
            if touched is not None and candidate.line not in touched[relative]:
                # A value patch has no line of its own worth trusting here: it
                # rebinds a module-level name, and the name's assignment *is*
                # the line, so the same test applies.
                continue
            identifier = f"{candidate.operator}@{relative}:{candidate.line}"
            # The suffix is part of the id, so it has to exist before `--only`
            # can match on it -- otherwise `...:403#1` selects nothing and the
            # run silently reports on a mutation the caller did not ask for.
            count = seen.get(identifier, 0)
            seen[identifier] = count + 1
            if count:
                identifier = f"{identifier}#{count}"
            if only and not any(token in identifier for token in only):
                continue
            mutation = _build(
                candidate, tree, scopes, baseline_code, name, relative, str(path), identifier
            )
            if isinstance(mutation, str):
                plan.errors.append((identifier, mutation))
                continue
            plan.mutations.append(mutation)
            if candidate.kind == "value":
                plan.whole_file.add(str(path))
            else:
                lines = plan.watched.setdefault(str(path), set())
                lines.add(candidate.line)
                lines.update(candidate.watch)
                plan.watch[mutation.identifier] = (candidate.line, *candidate.watch)
    return plan


def _build(
    candidate: Candidate,
    tree: ast.Module,
    scopes: dict[int, tuple[str, ...]],
    baseline_code: object,
    module: str,
    relative: str,
    filename: str,
    identifier: str,
) -> Mutation | str:
    site = Site(path=relative, line=candidate.line, scope=candidate.scope_name)
    if candidate.kind == "value":
        # A Cedar policy is compiled the moment the module binds it, so
        # rebinding the text is only half the mutation -- see `PolicyPatch`.
        build = PolicyPatch if candidate.operator.startswith("cedar.") else ValuePatch
        patch = build(module_name=module, path=candidate.value_path, value=candidate.value)
        try:
            if patch.is_noop():
                return "the replacement value equals the declared one"
        except PatchError as error:
            return str(error)
        return Mutation(identifier, candidate.operator, candidate.control, site, module, patch)

    if candidate.mutate is None:  # pragma: no cover - defensive
        return "no transform"
    try:
        mutated = transform_module(tree, candidate.node_id, candidate.mutate)
        code = compile_module(mutated, filename)
    except (PatchError, SyntaxError, ValueError, TypeError) as error:
        return f"did not compile: {type(error).__name__}: {error}"
    replacement = find_code(code, candidate.scope_name)
    if replacement is None:
        return f"no code object named {candidate.scope_name} after the rewrite"
    patch = CodePatch(module_name=module, scope=candidate.scope_name, code=replacement)
    try:
        patch.verify()
    except PatchError as error:
        return str(error)
    if patch.is_noop():
        return "compiles to the same bytecode"
    return Mutation(identifier, candidate.operator, candidate.control, site, module, patch)


# ---------------------------------------------------------------------------
# running


@dataclass
class Baseline:
    passed: frozenset[str]
    failed: tuple[str, ...]
    index: dict[tuple[str, int], tuple[str, ...]]
    per_file: dict[str, tuple[str, ...]]
    seconds: float


def _pytest_argv(targets: Sequence[str], extra: Sequence[str]) -> list[str]:
    return [*_PYTEST_BASE, *extra, *targets]


def run_baseline(
    tests: Sequence[str],
    plan: Plan,
    *,
    extra: Sequence[str],
    workdir: Path,
) -> Baseline:
    """One instrumented full run, in a fork, so the parent stays pristine."""
    import pytest

    watched = {path: frozenset(lines) for path, lines in plan.watched.items()}
    for path in plan.whole_file:
        try:
            span = len(Path(path).read_text(encoding="utf-8").splitlines()) + 1
        except OSError:  # pragma: no cover - the file was read moments ago
            continue
        watched[path] = frozenset(range(1, span)) | watched.get(path, frozenset())

    target = workdir / "baseline.json"
    started = time.perf_counter()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        code = 3
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            tracer = LineTracer(watched)
            outcomes = OutcomeRecorder()
            tracer.start()
            try:
                code = int(pytest.main(_pytest_argv(tests, extra), plugins=[tracer, outcomes]))
            finally:
                tracer.stop()
            index = tracer.index()
            per_file: dict[str, set[str]] = {path: set() for path in plan.whole_file}
            for (path, _), nodes in index.items():
                bucket = per_file.get(path)
                if bucket is not None:
                    bucket.update(nodes)
            payload = {
                "passed": sorted(outcomes.passed),
                "failed": sorted(outcomes.failed),
                "hits": [
                    [f"{path}:{line}", list(nodes)] for (path, line), nodes in index.items()
                ],
                "files": {path: sorted(nodes) for path, nodes in per_file.items()},
                "code": code,
            }
            target.write_text(json.dumps(payload), encoding="utf-8")
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    elapsed = time.perf_counter() - started
    if not target.exists():
        raise RuntimeError("the baseline run produced no result; the suite may have crashed")
    payload = json.loads(target.read_text(encoding="utf-8"))
    index: dict[tuple[str, int], tuple[str, ...]] = {}
    for key, nodes in payload["hits"]:
        path, _, line = key.rpartition(":")
        index[(path, int(line))] = tuple(nodes)
    return Baseline(
        passed=frozenset(payload["passed"]),
        failed=tuple(payload["failed"]),
        index=index,
        per_file={path: tuple(nodes) for path, nodes in payload["files"].items()},
        seconds=elapsed,
    )


def candidates_for(
    mutation: Mutation, plan: Plan, baseline: Baseline, source_root: Path
) -> tuple[str, ...]:
    """The tests that could possibly notice this mutation.

    Deliberately a superset. A test that runs the line but could not have been
    affected only costs a fraction of a second; a test wrongly left out turns a
    killed mutant into a reported survivor, which is a lie in the direction
    people act on.
    """
    site = Path(mutation.site.path)
    absolute = str(site if site.is_absolute() else (source_root / site).resolve())
    if isinstance(mutation.patch, ValuePatch):
        # An import-time constant is bound before the first test runs, so no
        # line attributes it. Anything that executed the defining module is a
        # candidate.
        return tuple(n for n in baseline.per_file.get(absolute, ()) if n in baseline.passed)
    found: set[str] = set()
    for line in plan.watch.get(mutation.identifier, (mutation.site.line,)):
        found.update(baseline.index.get((absolute, line), ()))
    return tuple(sorted(n for n in found if n in baseline.passed))


def run_mutant(
    mutation: Mutation,
    tests: Sequence[str],
    *,
    extra: Sequence[str],
    workdir: Path,
    timeout: float,
    ordinal: int,
) -> tuple[Outcome, tuple[str, ...], float, str]:
    """Fork, install the mutation, run the candidate tests, report."""
    import pytest

    target = workdir / f"m{ordinal}.json"
    started = time.perf_counter()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            recorder = OutcomeRecorder()
            patch = mutation.patch
            if patch is None:  # pragma: no cover - never planned
                target.write_text(json.dumps({"error": "no patch"}), encoding="utf-8")
                os._exit(0)
            try:
                patch.apply()
            except Exception as error:  # noqa: BLE001 - the child's only job is
                # to report why it could not run; re-raising loses the reason.
                target.write_text(
                    json.dumps({"error": f"{type(error).__name__}: {error}"}), encoding="utf-8"
                )
                os._exit(0)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            code = int(pytest.main(_pytest_argv(tests, extra), plugins=[recorder]))
            target.write_text(
                json.dumps({"code": code, "failed": sorted(recorder.failed)}), encoding="utf-8"
            )
        finally:
            os._exit(0)

    deadline = started + timeout
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.perf_counter() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return (Outcome.TIMEOUT, (), time.perf_counter() - started,
                    f"exceeded {timeout:g}s; undecided")
        time.sleep(0.002)
    elapsed = time.perf_counter() - started
    if not target.exists():
        signalled = os.WIFSIGNALED(status)
        note = "the child died before reporting"
        if signalled:
            return (Outcome.KILLED, (), elapsed,
                    f"the interpreter took signal {os.WTERMSIG(status)} with the control removed")
        return (Outcome.ERROR, (), elapsed, note)
    payload = json.loads(target.read_text(encoding="utf-8"))
    target.unlink(missing_ok=True)
    if "error" in payload:
        return (Outcome.ERROR, (), elapsed, payload["error"])
    failed = tuple(payload["failed"])
    if failed:
        return (Outcome.KILLED, failed, elapsed, "")
    if payload["code"] not in (0, 5):
        return (Outcome.KILLED, (), elapsed,
                f"pytest exited {payload['code']} with the control removed")
    return (Outcome.SURVIVED, (), elapsed, "")


def execute(
    *,
    repo: Path,
    roots: Sequence[Path],
    tests: Sequence[str],
    workdir: Path,
    operators: Sequence[str] = (),
    only: Sequence[str] = (),
    extra: Sequence[str] = (),
    timeout: float = DEFAULT_TIMEOUT,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    limit: int = 0,
    changed: str | None = None,
    progress: bool = True,
) -> Report:
    started = time.perf_counter()
    plan = build_plan(roots, repo, operators=operators, only=only, changed=changed)
    if limit:
        plan.mutations = plan.mutations[:limit]
    report = Report(sources=tuple(plan.sources))

    if progress:
        print(
            f"wreath mutant: {len(plan.mutations)} mutation(s) in "
            f"{len(plan.sources)} file(s); {len(plan.errors)} declined.",
            file=sys.stderr,
        )

    import pytest

    # Warm the parent: collection imports every test module, and a fork
    # inherits all of it. Its own output is noise -- the run has not started.
    with contextlib.redirect_stdout(io.StringIO()):
        pytest.main([*_PYTEST_BASE, "--collect-only", *extra, *tests])

    baseline = run_baseline(tests, plan, extra=extra, workdir=workdir)
    report.baseline_tests = len(baseline.passed) + len(baseline.failed)
    report.baseline_failures = baseline.failed
    report.baseline_seconds = baseline.seconds
    if progress:
        print(
            f"wreath mutant: baseline {len(baseline.passed)} passed, "
            f"{len(baseline.failed)} failed, {baseline.seconds:.1f}s.",
            file=sys.stderr,
        )

    for ordinal, mutation in enumerate(plan.mutations):
        selected = candidates_for(mutation, plan, baseline, repo)
        if not selected:
            report.verdicts.append(
                Verdict(mutation, Outcome.UNREACHED, note="no test executed this line")
            )
            continue
        if len(selected) > max_candidates:
            report.verdicts.append(
                Verdict(
                    mutation,
                    Outcome.ERROR,
                    candidates=selected,
                    note=f"{len(selected)} candidate tests exceeds --max-candidates",
                )
            )
            continue
        outcome, killers, seconds, note = run_mutant(
            mutation, selected, extra=extra, workdir=workdir, timeout=timeout, ordinal=ordinal
        )
        report.verdicts.append(
            Verdict(mutation, outcome, candidates=selected, killers=killers,
                    seconds=seconds, note=note)
        )
        if progress:
            print(
                f"  [{ordinal + 1}/{len(plan.mutations)}] {outcome.value:<10} "
                f"{mutation.identifier} ({len(selected)} test(s), {seconds:.2f}s)",
                file=sys.stderr,
            )

    for identifier, reason in plan.errors:
        report.verdicts.append(
            Verdict(
                Mutation(identifier, "-", reason, Site("-", 0, "-"), "-"),
                Outcome.EQUIVALENT if "same bytecode" in reason else Outcome.ERROR,
                note=reason,
            )
        )
    report.total_seconds = time.perf_counter() - started
    return report
