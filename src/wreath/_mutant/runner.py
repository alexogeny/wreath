"""Build the mutants, learn which tests reach them, and fork one child each.

The shape of a run:

1. **Import** every module that will be mutated, so the operators can ask the
   live objects real questions -- does this keyword have a default? is this
   constant a compiled pattern? -- instead of guessing from syntax.
2. **Scan** each module's AST and compile only the replacement's top-level
   definition *in the parent*. A mutant that cannot be compiled or applied is
   found here, before any test runs, and reported as this tool's error rather
   than as the suite's failure.
3. **Warm** the interpreter by collecting and indexing the test suite once.
   After this, a forked child inherits the pristine case image and resolves an
   exact candidate set without importing or rebuilding an index.
4. **Baseline** in a forked child, under the line tracer: which tests pass, and
   which of them touch each mutation site.
5. **One fork per mutant**, running only the tests that reach it and reporting
   completion through a pidfd and a bounded pipe on the default maxfail path.

`fork` is why this is cheap and also why it is Linux-first. The alternative --
rewrite the file, start a new interpreter -- costs the whole import graph per
mutant, and in a repository this size that is the difference between a coffee
and an afternoon.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import io
import itertools
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from .differential import DifferentialFuzzConfig, apply_differential_fuzz
from .model import Mutation, Outcome, Report, Site, Verdict
from .operators import Candidate, scan, tag, unsupported_module_declarations
from .patch import (
    CapturedDefault,
    CodePatch,
    PatchError,
    PolicyPatch,
    ValuePatch,
    _ScopeFacts,
    compile_scope,
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

#: Mutant children to execute at once.  The parent forks them from one warmed,
#: pristine interpreter, so this costs neither another import graph nor a
#: thread calling ``fork()``.  One remains the public command's conservative
#: default; ``wreath test`` opts into a measured bounded value.
DEFAULT_JOBS = 1

#: Failures a mutant's test run collects before pytest stops it.
#:
#: One, because one is what the verdict reads: `KILLED` is decided by whether
#: *anything* that passed at baseline failed, and the text report prints
#: `killers[0]`. Every test after the first failure was being run so its result
#: could be discarded -- on `wreath.cache_control` that was 330 test executions
#: where 73 produced the same twenty verdicts, and the ratio grows with the
#: candidate set, so a hot-path control selecting most of the suite wastes far
#: more than this module does.
#:
#: Zero restores exhaustive collection. Raise it to gather more entries for the
#: `killers` list in `--format json`, which is capped at twenty regardless.
DEFAULT_MAXFAIL = 1

_PYTEST_BASE = ("-q", "--no-header", "-p", "no:cacheprovider", "-p", "no:randomly")

#: Tests each mutant child actually executed, appended per mutant in run order.
#: A counter rather than a stopwatch, so it says what a change to the selection
#: or the failure bound did even on a machine that is busy with something else.
TESTS_RUN: list[int] = []


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


@dataclass(frozen=True, slots=True)
class SampleSelection:
    """A deterministic sample and the coverage it represents."""

    identifiers: tuple[str, ...]
    eligible_candidates: int
    candidate_counts_by_operator: dict[str, int]
    selected_counts_by_operator: dict[str, int]
    candidate_files: int
    selected_files: int
    missing_operators: tuple[str, ...]
    unsupported_declarations: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible_candidates": self.eligible_candidates,
            "selected_candidates": len(self.identifiers),
            "candidate_files": self.candidate_files,
            "selected_files": self.selected_files,
            "by_operator": {
                operator: {
                    "eligible": eligible,
                    "selected": self.selected_counts_by_operator.get(operator, 0),
                }
                for operator, eligible in self.candidate_counts_by_operator.items()
            },
            "missing_operators": list(self.missing_operators),
            "unsupported_declarations": list(self.unsupported_declarations),
        }


def _identify_candidate(candidate: Candidate, relative: str, seen: dict[str, int]) -> str:
    identifier = f"{candidate.operator}@{relative}:{candidate.line}"
    duplicate = seen.get(identifier, 0)
    seen[identifier] = duplicate + 1
    return f"{identifier}#{duplicate}" if duplicate else identifier


def module_name_for(path: Path) -> str | None:
    """Dotted name of a file, by walking up while `__init__.py` exists."""
    parts = [] if path.name == "__init__.py" else [path.stem]
    parent = path.parent
    while (parent / "__init__.py").exists():
        parts.append(parent.name)
        parent = parent.parent
    if not parts or (len(parts) == 1 and path.suffix != ".py"):
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


def changed_lines(repo: Path, ref: str) -> dict[str, set[int] | range]:
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
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
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
                lines.setdefault(current, set()).update(range(int(start), int(start) + length))
    result = cast("dict[str, set[int] | range]", lines)
    for path in git("ls-files", "--others", "--exclude-standard").splitlines():
        if path.endswith(".py"):
            result[path] = range(1, 1_000_000)
    return result


def build_plan(
    roots: Sequence[Path],
    repo: Path,
    *,
    operators: Sequence[str] = (),
    only: Sequence[str] = (),
    changed: str | None = None,
    selected_ids: frozenset[str] | None = None,
) -> Plan:
    """Compile every mutation this run will attempt."""
    plan = Plan()
    seen: dict[str, int] = {}
    touched = changed_lines(repo, changed) if changed is not None else None
    selected_paths = None
    operator_prefixes = tuple(operators)
    only_pattern = re.compile("|".join(map(re.escape, only))) if only else None
    if selected_ids is not None:
        selected_paths = {
            identifier.split("@", 1)[1].rpartition(":")[0] for identifier in selected_ids
        }
    for path in discover(roots):
        name = module_name_for(path)
        if name is None or name.startswith("wreath._mutant"):
            continue
        relative = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
        if selected_paths is not None and relative not in selected_paths:
            continue
        if touched is not None and relative not in touched:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, ValueError) as error:
            plan.errors.append((str(path), f"unreadable: {error}"))
            continue
        if selected_ids is None:
            plan.sources.append(relative)
        try:
            importlib.import_module(name)
        except Exception as error:
            plan.errors.append((name, f"not importable: {type(error).__name__}: {error}"))
            continue
        scopes = tag(tree)
        selected: list[tuple[Candidate, str]] = []

        for candidate in scan(tree, name, scopes=scopes):
            if operator_prefixes and not candidate.operator.startswith(operator_prefixes):
                continue
            if touched is not None and candidate.line not in touched[relative]:
                # A value patch has no line of its own worth trusting here: it
                # rebinds a module-level name, and the name's assignment *is*
                # the line, so the same test applies.
                continue
            identifier = _identify_candidate(candidate, relative, seen)
            # The suffix is part of the id, so it has to exist before `--only`
            # can match on it -- otherwise `...:403#1` selects nothing and the
            # run silently reports on a mutation the caller did not ask for.
            if only_pattern is not None and only_pattern.search(identifier) is None:
                continue
            if selected_ids is not None and identifier not in selected_ids:
                continue
            selected.append((candidate, identifier))
        if selected_ids is None:
            for candidate in unsupported_module_declarations(tree, name, scopes=scopes):
                if operator_prefixes and not candidate.operator.startswith(operator_prefixes):
                    continue
                if touched is not None and candidate.line not in touched[relative]:
                    continue
                identifier = _identify_candidate(candidate, relative, seen)
                if only_pattern is not None and only_pattern.search(identifier) is None:
                    continue
                plan.errors.append(
                    (
                        identifier,
                        "module-level declaration cannot be mutated without replaying startup "
                        "side effects; place it inside an application factory function",
                    )
                )
        if not selected:
            continue
        if selected_ids is not None:
            plan.sources.append(relative)
        captured_defaults = None
        if len(selected) > 1:
            default_names = frozenset(
                candidate.value_path[0]
                for candidate, _ in selected
                if candidate.kind == "value"
                and not candidate.operator.startswith("cedar.")
                and len(candidate.value_path) == 1
            )
            captured_defaults = (
                cast(
                    dict[str, tuple[CapturedDefault, ...]],
                    _captured_default_targets(tree, (), selected_names=default_names),
                )
                if default_names
                else {}
            )
        scope_facts = None
        if len(selected) > 1:
            code_count = 0
            for candidate, _ in selected:
                if candidate.kind != "value" and candidate.mutate is not None:
                    code_count += 1
                    if code_count == 2:
                        scope_facts = _ScopeFacts.from_tree(tree)
                        break
        for candidate, identifier in selected:
            mutation = _build(
                candidate,
                tree,
                name,
                relative,
                str(path),
                identifier,
                captured_defaults=captured_defaults,
                scope_facts=scope_facts,
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


def sample_identifiers(
    roots: Sequence[Path],
    repo: Path,
    count: int,
    *,
    operators: Sequence[str] = (),
    only: Sequence[str] = (),
    changed: str | None = None,
) -> tuple[str, ...]:
    """Choose a stable risk-stratified whole-corpus sample."""
    return select_sample(
        roots,
        repo,
        count,
        operators=operators,
        only=only,
        changed=changed,
    ).identifiers


def select_sample(
    roots: Sequence[Path],
    repo: Path,
    count: int,
    *,
    operators: Sequence[str] = (),
    only: Sequence[str] = (),
    changed: str | None = None,
) -> SampleSelection:
    """Choose a deterministic sample that represents operator families first.

    One candidate from each family is selected before remaining slots are
    filled by whole-corpus hash rank. When the budget is smaller than the
    family count, rare families go first.
    """
    if count < 1:
        raise ValueError("mutation sample size must be at least 1")
    touched = changed_lines(repo, changed) if changed is not None else None
    seen: dict[str, int] = {}
    identifiers: list[tuple[str, str, str]] = []
    errors: list[tuple[str, str]] = []
    unsupported: list[str] = []
    operator_prefixes = tuple(operators)
    only_pattern = re.compile("|".join(map(re.escape, only))) if only else None
    for path in discover(roots):
        name = module_name_for(path)
        if name is None or name.startswith("wreath._mutant"):
            continue
        relative = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
        if touched is not None and relative not in touched:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except OSError, SyntaxError, ValueError:
            # The selected build pass reports errors for selected sources. A
            # file that cannot yield identifiers cannot enter the sample.
            continue
        try:
            importlib.import_module(name)
        except Exception as error:
            errors.append((name, f"not importable: {type(error).__name__}: {error}"))
            continue
        scopes = tag(tree)
        for candidate in scan(tree, name, scopes=scopes):
            if operator_prefixes and not candidate.operator.startswith(operator_prefixes):
                continue
            if touched is not None and candidate.line not in touched[relative]:
                continue
            identifier = f"{candidate.operator}@{relative}:{candidate.line}"
            duplicate = seen.get(identifier, 0)
            seen[identifier] = duplicate + 1
            if duplicate:
                identifier = f"{identifier}#{duplicate}"
            if only_pattern is not None and only_pattern.search(identifier) is None:
                continue
            identifiers.append((identifier, candidate.operator, relative))
        for candidate in unsupported_module_declarations(tree, name, scopes=scopes):
            if operator_prefixes and not candidate.operator.startswith(operator_prefixes):
                continue
            if touched is not None and candidate.line not in touched[relative]:
                continue
            identifier = _identify_candidate(candidate, relative, seen)
            if only_pattern is not None and only_pattern.search(identifier) is None:
                continue
            unsupported.append(identifier)

    def rank(identifier: str) -> tuple[bytes, str]:
        digest = hashlib.blake2b(identifier.encode(), digest_size=16).digest()
        return digest, identifier

    candidate_counts: dict[str, int] = {}
    files: set[str] = set()
    for _, operator, relative in identifiers:
        candidate_counts[operator] = candidate_counts.get(operator, 0) + 1
        files.add(relative)
    if count <= len(candidate_counts):
        chosen = set(
            sorted(candidate_counts, key=lambda item: (candidate_counts[item], item))[:count]
        )
        best: dict[str, tuple[tuple[bytes, str], int]] = {}
        for index, (identifier, operator, _) in enumerate(identifiers):
            if operator not in chosen:
                continue
            key = rank(identifier)
            previous = best.get(operator)
            if previous is None or key < previous[0]:
                best[operator] = key, index
        selected = [identifiers[index] for _, index in sorted(best.values())]
    else:
        identifiers.sort(key=lambda item: rank(item[0]))
        remaining = set(candidate_counts)
        selected_indices: set[int] = set()
        for index, (_, operator, _) in enumerate(identifiers):
            if operator in remaining:
                # complexity: allow SL-LINEAR-METHOD -- `remaining` is a set.
                remaining.remove(operator)
                selected_indices.add(index)
                if not remaining:
                    break
        for index in range(len(identifiers)):
            selected_indices.add(index)
            if len(selected_indices) >= count:
                break
        selected = [identifiers[index] for index in sorted(selected_indices)]
    selected_counts: dict[str, int] = {}
    for _, operator, _ in selected:
        selected_counts[operator] = selected_counts.get(operator, 0) + 1
    candidate_counts = dict(sorted(candidate_counts.items()))
    missing = tuple(operator for operator in candidate_counts if operator not in selected_counts)
    return SampleSelection(
        identifiers=tuple(item[0] for item in selected),
        eligible_candidates=len(identifiers),
        candidate_counts_by_operator=candidate_counts,
        selected_counts_by_operator=dict(sorted(selected_counts.items())),
        candidate_files=len(files),
        selected_files=len({item[2] for item in selected}),
        missing_operators=missing,
        unsupported_declarations=tuple(unsupported),
        errors=tuple(errors),
    )


def watch_selected_identifiers(
    roots: Sequence[Path],
    repo: Path,
    selected_ids: frozenset[str],
) -> tuple[dict[str, frozenset[int]], frozenset[str]]:
    """Find only the lines a selected sample needs, without importing targets."""
    seen: dict[str, int] = {}
    watched: dict[str, set[int]] = {}
    whole_file: set[str] = set()
    selected_paths = {identifier.split("@", 1)[1].rpartition(":")[0] for identifier in selected_ids}
    for path in discover(roots):
        name = module_name_for(path)
        if name is None or name.startswith("wreath._mutant"):
            continue
        relative = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
        if relative not in selected_paths:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except OSError, SyntaxError, ValueError:
            continue
        for candidate in scan(tree, name):
            identifier = f"{candidate.operator}@{relative}:{candidate.line}"
            duplicate = seen.get(identifier, 0)
            seen[identifier] = duplicate + 1
            if duplicate:
                identifier = f"{identifier}#{duplicate}"
            if identifier not in selected_ids:
                continue
            filename = str(path)
            if candidate.kind == "value":
                if filename not in whole_file:
                    whole_file.add(filename)
                    span = len(source.splitlines()) + 1
                    watched.setdefault(filename, set()).update(range(1, span))
            else:
                lines = watched.setdefault(filename, set())
                lines.add(candidate.line)
                lines.update(candidate.watch)
    return (
        {path: frozenset(lines) for path, lines in watched.items()},
        frozenset(whole_file),
    )


def _build(
    candidate: Candidate,
    tree: ast.Module,
    module: str,
    relative: str,
    filename: str,
    identifier: str,
    *,
    captured_defaults: dict[str, tuple[CapturedDefault, ...]] | None = None,
    scope_facts: _ScopeFacts | None = None,
) -> Mutation | str:
    site = Site(path=relative, line=candidate.line, scope=candidate.scope_name)
    if candidate.kind == "value":
        # A Cedar policy is compiled the moment the module binds it, so
        # rebinding the text is only half the mutation -- see `PolicyPatch`.
        if candidate.operator.startswith("cedar."):
            patch = PolicyPatch(
                module_name=module,
                path=candidate.value_path,
                value=candidate.value,
            )
        else:
            if len(candidate.value_path) != 1:
                targets = ()
            elif captured_defaults is None:
                targets = cast(
                    tuple[CapturedDefault, ...],
                    _captured_default_targets(tree, candidate.value_path),
                )
            else:
                targets = captured_defaults.get(candidate.value_path[0], ())
            patch = ValuePatch(
                module_name=module,
                path=candidate.value_path,
                value=candidate.value,
                captured_defaults=targets,
            )
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
        code = compile_scope(mutated, candidate.scope_name, filename, facts=scope_facts)
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


def _captured_default_targets(
    tree: ast.Module,
    value_path: tuple[str, ...],
    *,
    selected_names: frozenset[str] | None = None,
) -> tuple[CapturedDefault, ...] | dict[str, tuple[CapturedDefault, ...]]:
    if selected_names is None and len(value_path) != 1:
        return ()
    names = frozenset((value_path[0],)) if selected_names is None else selected_names
    if not names:
        return {}
    found: dict[str, dict[str, tuple[set[int], set[str]]]] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.classes: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.classes.append(node.name)
            for statement in node.body:
                self.visit(statement)
            self.classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._record(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._record(node)

        def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            scope = ".".join((*self.classes, node.name))
            defaults = found.setdefault(scope, {})
            for index, default in enumerate(node.args.defaults):
                if isinstance(default, ast.Name) and default.id in names:
                    positions, _ = defaults.setdefault(default.id, (set(), set()))
                    positions.add(index)
            for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
                if isinstance(default, ast.Name) and default.id in names:
                    _, keywords = defaults.setdefault(default.id, (set(), set()))
                    keywords.add(argument.arg)

    Visitor().visit(tree)
    targets: dict[str, list[CapturedDefault]] = {name: [] for name in names}
    for scope, defaults in found.items():
        for name, (positions, keywords) in defaults.items():
            targets[name].append(
                CapturedDefault(
                    scope=scope,
                    positional=tuple(sorted(positions)),
                    keywords=tuple(sorted(keywords)),
                )
            )
    if selected_names is None:
        return tuple(targets[value_path[0]])
    return {name: tuple(items) for name, items in targets.items()}


# running


@dataclass
class Baseline:
    passed: frozenset[str]
    failed: tuple[str, ...]
    index: dict[tuple[str, int], tuple[str, ...]]
    per_file: dict[str, tuple[str, ...]]
    seconds: float


def read_baseline(path: Path) -> Baseline:
    """Read coverage captured by ``wreath test``'s ordinary run."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        hits = {
            (key.rpartition(":")[0], int(key.rpartition(":")[2])): tuple(nodes)
            for key, nodes in payload["hits"]
        }
        files = {key: tuple(nodes) for key, nodes in payload["files"].items()}
        return Baseline(
            passed=frozenset(payload["passed"]),
            failed=tuple(payload["failed"]),
            index=hits,
            per_file=files,
            seconds=float(payload["seconds"]),
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise ValueError(f"invalid reused mutation baseline {path}: {error}") from error


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
                "hits": [[f"{path}:{line}", list(nodes)] for (path, line), nodes in index.items()],
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


def run_native_baseline(
    tests: Sequence[str],
    plan: Plan,
    *,
    workdir: Path,
    native_collection: Any | None = None,
) -> Baseline:
    """Measure reachability in a fork of one pristine native collection."""
    from wreath._native_test_runner import (
        _facade_import,
        _run,
        _test_import_paths,
    )

    watched = {path: frozenset(lines) for path, lines in plan.watched.items()}
    for path in plan.whole_file:
        try:
            span = len(Path(path).read_text(encoding="utf-8").splitlines()) + 1
        except OSError:  # pragma: no cover - the planner read it immediately before
            continue
        watched[path] = frozenset(range(1, span)) | watched.get(path, frozenset())
    owns_collection = native_collection is None
    collection = native_collection or prepare_native_collection(tests)
    target = workdir / "native-baseline.json"
    started = time.perf_counter()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.close(devnull)
            tracer = LineTracer(watched)

            def observe(node_id: str, outcome: str | None) -> None:
                if outcome is None:
                    tracer.begin(node_id)
                else:
                    tracer.end()

            if watched:
                tracer.start()
            try:
                with _facade_import(), _test_import_paths(collection.files):
                    results = _run(collection, 0, observe)
            finally:
                if watched:
                    tracer.stop()
            traced_failures = tuple(
                result.node_id for result in results if result.outcome in {"failed", "interrupted"}
            )
            if watched and traced_failures:
                retried = _retry_native_results(tuple(result.node_id for result in results))
                results = tuple(
                    replace(result, outcome=retried[result.node_id])
                    if result.node_id in retried
                    else result
                    for result in results
                )
            index = tracer.index()
            per_file: dict[str, set[str]] = {path: set() for path in plan.whole_file}
            for (path, _line), nodes in index.items():
                bucket = per_file.get(path)
                if bucket is not None:
                    bucket.update(nodes)
            target.write_text(
                json.dumps(
                    {
                        "passed": sorted(
                            result.node_id for result in results if result.outcome == "passed"
                        ),
                        "failed": [
                            result.node_id
                            for result in results
                            if result.outcome in {"failed", "interrupted"}
                        ],
                        "hits": [
                            [f"{path}:{line}", list(nodes)] for (path, line), nodes in index.items()
                        ],
                        "files": {path: sorted(nodes) for path, nodes in per_file.items()},
                    }
                ),
                encoding="utf-8",
            )
        except Exception as error:
            target.write_text(
                json.dumps({"error": f"{type(error).__name__}: {error}"}),
                encoding="utf-8",
            )
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    if not target.exists():
        exit_code = os.waitstatus_to_exitcode(status)
        raise RuntimeError(f"the native baseline exited {exit_code} without a result")
    payload = json.loads(target.read_text(encoding="utf-8"))
    target.unlink(missing_ok=True)
    if "error" in payload:
        raise RuntimeError(f"native baseline failed: {payload['error']}")
    index: dict[tuple[str, int], tuple[str, ...]] = {}
    for key, nodes in payload["hits"]:
        path, _, line = key.rpartition(":")
        index[(path, int(line))] = tuple(nodes)
    if owns_collection:
        release_native_collection(collection)
    return Baseline(
        passed=frozenset(payload["passed"]),
        failed=tuple(payload["failed"]),
        index=index,
        per_file={path: tuple(nodes) for path, nodes in payload["files"].items()},
        seconds=time.perf_counter() - started,
    )


def _retry_native_results(node_ids: Sequence[str]) -> dict[str, str]:
    files = sorted({node_id.split("::", 1)[0] for node_id in node_ids})
    script = """
import contextlib
import io
import json
import sys
from wreath._mutant.runner import prepare_native_collection
from wreath._native_test_runner import _test_import_paths, run_selected

payload = json.load(sys.stdin)
files = payload["files"]
node_ids = payload["node_ids"]
collection = prepare_native_collection(files)
fresh_node_ids = [case.node_id for case in collection.cases]
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    with _test_import_paths(collection.files):
        results = run_selected(collection, fresh_node_ids, max_failures=0)
fresh_results = dict(
    zip(fresh_node_ids, (result.outcome for result in results), strict=True)
)
fresh_families = {}
for node_id, outcome in fresh_results.items():
    path, separator, suffix = node_id.rpartition("::")
    name, parameter_separator, _parameter = suffix.partition("[")
    if separator and parameter_separator:
        fresh_families.setdefault(f"{path}::{name}", []).append(outcome)
reconciled = []
for node_id in node_ids:
    outcome = fresh_results.get(node_id)
    if outcome is None:
        path, separator, suffix = node_id.rpartition("::")
        name, parameter_separator, _parameter = suffix.partition("[")
        family = (
            fresh_families.get(f"{path}::{name}")
            if separator and parameter_separator
            else None
        )
        if family is None:
            raise RuntimeError(f"fresh native baseline did not collect {node_id!r}")
        outcome = next(
            (candidate for candidate in ("failed", "interrupted") if candidate in family),
            "passed" if "passed" in family else family[0],
        )
    reconciled.append([node_id, outcome])
print(json.dumps(reconciled))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"files": files, "node_ids": node_ids}),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "no output"
        raise RuntimeError(f"fresh native baseline retry failed: {detail}")
    return dict(json.loads(completed.stdout))


def prepare_native_collection(tests: Sequence[str]) -> Any:
    """Compile the immutable native case image before any test mutates state."""
    from wreath._native_test_runner import Options, _configured_markers, collect

    return collect(
        Options(
            tuple(Path(target) for target in tests),
            markers=_configured_markers(),
            mutation_mode=True,
        )
    )


def release_native_collection(collection: Any) -> None:
    """Release parent-owned fixture resources and imported test modules."""
    from wreath._native_test_runner import _forget_modules

    if collection.runtime is not None:
        collection.runtime.close()
    _forget_modules(collection.modules)


def combine_native_collections(collections: Sequence[Any]) -> Any:
    """Join operation-owned case images without importing their modules again."""
    from wreath._native_test_runner import Collection

    cases = tuple(itertools.chain.from_iterable(collection.cases for collection in collections))
    modules = tuple(
        dict.fromkeys(
            itertools.chain.from_iterable(collection.modules for collection in collections)
        )
    )
    files = tuple(
        dict.fromkeys(itertools.chain.from_iterable(collection.files for collection in collections))
    )
    index: dict[str, Any] = {}
    for collection in collections:
        for node_id, case in collection.index.items():
            index[node_id] = case
    return Collection(cases, modules, files=files, runtime=None, index=index)


def unique_native_collections(collections: Iterable[Any]) -> tuple[Any, ...]:
    """Retain one owner for each case image referenced by one or more files."""
    seen: set[int] = set()
    unique: list[Any] = []
    for collection in collections:
        identity = id(collection)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(collection)
    return tuple(unique)


def pooled_native_collection(files: Sequence[str], pool: dict[str, Any]) -> Any:
    """Compile only files absent from this operation's native case-image pool."""
    missing = tuple(path for path in files if path not in pool)
    if missing:
        collection = prepare_native_collection(missing)
        for path in missing:
            pool[path] = collection
    return combine_native_collections(unique_native_collections(pool.values()))


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
    # A direct ``tests/test_feature.py`` is normally the focused contract;
    # deeper integration and corpus suites are the wider net. Pytest stops on
    # the first objection, so put the direct contract first without excluding
    # a single candidate. This is ordering only -- the verdict set is unchanged.
    source_stem = site.stem.removeprefix("_")
    ordered = tuple(
        sorted(
            (n for n in found if n in baseline.passed),
            key=lambda nodeid: (
                source_stem not in Path(nodeid.split("::", 1)[0]).stem,
                len(Path(nodeid.split("::", 1)[0]).parts),
                nodeid,
            ),
        )
    )
    probe = _focused_probe(mutation, ordered)
    if not probe:
        return ordered
    return (*probe, *(nodeid for nodeid in ordered if nodeid != probe[0]))


@dataclass(slots=True)
class RunningMutant:
    """A forked mutant child that the parent can reap without blocking."""

    pid: int
    target: Path
    started: float
    timeout: float
    read_fd: int | None = None
    pid_fd: int | None = None


def _write_mutant_payload(
    target: Path,
    descriptor: int | None,
    payload: dict[str, object],
) -> None:
    encoded = json.dumps(payload).encode()
    if descriptor is None:
        target.write_bytes(encoded)
        return
    view = memoryview(encoded)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _wait_for_mutants(
    running: Sequence[RunningMutant],
    *,
    ceiling: float | None = None,
) -> None:
    """Sleep until one child exits or the nearest timeout becomes observable."""
    if not running:
        return
    now = time.perf_counter()
    delay = max(0.0, min(item.started + item.timeout for item in running) - now)
    if ceiling is not None:
        delay = min(delay, ceiling)
    descriptors = [item.pid_fd for item in running if item.pid_fd is not None]
    if descriptors:
        select.select(descriptors, (), (), delay)
    elif delay:
        time.sleep(min(delay, 0.002))


_PROBE_NOISE = frozenset(
    {
        "always",
        "branch",
        "choice",
        "clause",
        "compound",
        "condition",
        "control",
        "else",
        "fires",
        "from",
        "guarded",
        "into",
        "never",
        "operand",
        "statement",
        "take",
        "than",
        "that",
        "then",
        "this",
        "when",
        "with",
    }
)


def _focused_probe(mutation: Mutation, tests: Sequence[str]) -> tuple[str, ...]:
    """The one candidate whose name best describes the removed control.

    This is only a fast first objection. A pass still runs the complete
    candidate set with normal pytest session semantics, so a heuristic miss can
    cost one tiny invocation but can never turn a survivor into a kill or hide
    a candidate. A hit avoids importing dozens of unrelated candidate files.
    """
    if len(tests) < 2:
        return ()

    def words(value: str) -> set[str]:
        separated = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value).replace("_", " ")
        return {
            term
            for term in re.findall(r"[a-z0-9]+", separated.lower())
            if len(term) >= 3 and term not in _PROBE_NOISE
        }

    control_terms = words(mutation.control)
    context_terms = words(f"{mutation.site.scope} {Path(mutation.site.path).stem}")
    scored: list[tuple[int, int, str]] = []
    for nodeid in tests:
        node_terms = words(nodeid)
        score = 4 * len(control_terms & node_terms) + len(context_terms & node_terms)
        scored.append((score, -len(nodeid), nodeid))
    score, _length, nodeid = max(scored)
    return (nodeid,) if score >= 2 else ()


def start_mutant(
    mutation: Mutation,
    tests: Sequence[str],
    *,
    extra: Sequence[str],
    workdir: Path,
    timeout: float,
    ordinal: int,
    maxfail: int = DEFAULT_MAXFAIL,
    native_collection: Any | None = None,
) -> RunningMutant:
    """Fork a child that installs one mutation and runs its candidates.

    `maxfail` bounds how many failures the child collects before pytest stops.
    Zero -- the default -- runs the whole candidate set, which is what the
    verdict never needed: `KILLED` is decided by whether *anything* failed.
    """
    if native_collection is None:
        import pytest

    target = workdir / f"m{ordinal}.json"
    read_fd: int | None = None
    write_fd: int | None = None
    if native_collection is not None and maxfail == DEFAULT_MAXFAIL:
        read_fd, write_fd = os.pipe()
    started = time.perf_counter()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            if read_fd is not None:
                os.close(read_fd)
            patch = mutation.patch
            if patch is None:  # pragma: no cover - never planned
                _write_mutant_payload(target, write_fd, {"error": "no patch"})
                os._exit(0)
            try:
                patch.apply()
            except Exception as error:
                # to report why it could not run; re-raising loses the reason.
                _write_mutant_payload(
                    target,
                    write_fd,
                    {"error": f"{type(error).__name__}: {error}"},
                )
                os._exit(0)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            if native_collection is not None:
                from wreath._native_test_runner import run_selected

                results = run_selected(
                    native_collection,
                    tests,
                    max_failures=maxfail,
                )
                failed = sorted(result.node_id for result in results if result.outcome == "failed")
                _write_mutant_payload(
                    target,
                    write_fd,
                    {
                        "code": 1 if failed else 0,
                        "failed": failed,
                        "ran": len(results),
                    },
                )
                os._exit(0)

            recorder = OutcomeRecorder()
            child_extra = list(extra)
            if maxfail:
                # The mutant child only, never the baseline: the baseline's
                # whole product is the full pass/fail set and the line index
                # under it, and stopping it early would silently shrink the
                # candidate sets every later mutant is selected from.
                child_extra.append(f"--maxfail={maxfail}")
            ran = 0
            probe = _focused_probe(mutation, tests)
            if probe:
                code = int(pytest.main(_pytest_argv(probe, child_extra), plugins=[recorder]))
                ran += len(recorder.passed | recorder.failed)
            else:
                code = 0
            if not recorder.failed and code in (0, 5):
                # A focused pass proves only that one candidate did not object.
                # Run the untouched full set to decide the verdict.
                recorder = OutcomeRecorder()
                code = int(pytest.main(_pytest_argv(tests, child_extra), plugins=[recorder]))
                ran += len(recorder.passed | recorder.failed)
            _write_mutant_payload(
                target,
                write_fd,
                {
                    "code": code,
                    "failed": sorted(recorder.failed),
                    # How many tests actually executed. A counter rather
                    # than a stopwatch, so it survives a loaded machine.
                    "ran": ran,
                },
            )
        finally:
            os._exit(0)

    if write_fd is not None:
        os.close(write_fd)
    try:
        pid_fd = os.pidfd_open(pid)
    except AttributeError, OSError:
        pid_fd = None
    return RunningMutant(
        pid=pid,
        target=target,
        started=started,
        timeout=timeout,
        read_fd=read_fd,
        pid_fd=pid_fd,
    )


def poll_mutant(
    running: RunningMutant,
) -> tuple[Outcome, tuple[str, ...], float, str] | None:
    """Return a completed child's verdict, or ``None`` while it is running."""
    done, status = os.waitpid(running.pid, os.WNOHANG)
    if not done:
        if time.perf_counter() <= running.started + running.timeout:
            return None
        os.kill(running.pid, signal.SIGKILL)
        os.waitpid(running.pid, 0)
        if running.read_fd is not None:
            os.close(running.read_fd)
            running.read_fd = None
        if running.pid_fd is not None:
            os.close(running.pid_fd)
            running.pid_fd = None
        return (
            Outcome.TIMEOUT,
            (),
            time.perf_counter() - running.started,
            f"exceeded {running.timeout:g}s; undecided",
        )
    elapsed = time.perf_counter() - running.started
    if running.pid_fd is not None:
        os.close(running.pid_fd)
        running.pid_fd = None
    encoded = b""
    if running.read_fd is not None:
        chunks: list[bytes] = []
        while chunk := os.read(running.read_fd, 65536):
            chunks.append(chunk)
        encoded = b"".join(chunks)
        os.close(running.read_fd)
        running.read_fd = None
    elif running.target.exists():
        encoded = running.target.read_bytes()
        running.target.unlink(missing_ok=True)
    if not encoded:
        signalled = os.WIFSIGNALED(status)
        note = "the child died before reporting"
        if signalled:
            return (
                Outcome.KILLED,
                (),
                elapsed,
                f"the interpreter took signal {os.WTERMSIG(status)} with the control removed",
            )
        return (Outcome.ERROR, (), elapsed, note)
    payload = json.loads(encoded)
    TESTS_RUN.append(int(payload.get("ran", 0)))
    if "error" in payload:
        return (Outcome.ERROR, (), elapsed, payload["error"])
    failed = tuple(payload["failed"])
    if failed:
        return (Outcome.KILLED, failed, elapsed, "")
    if payload["code"] not in (0, 5):
        return (
            Outcome.KILLED,
            (),
            elapsed,
            f"pytest exited {payload['code']} with the control removed",
        )
    return (Outcome.SURVIVED, (), elapsed, "")


def run_mutant(
    mutation: Mutation,
    tests: Sequence[str],
    *,
    extra: Sequence[str],
    workdir: Path,
    timeout: float,
    ordinal: int,
    maxfail: int = DEFAULT_MAXFAIL,
    native_collection: Any | None = None,
) -> tuple[Outcome, tuple[str, ...], float, str]:
    """Fork one mutant and block until it reports (the compatibility helper)."""
    running = start_mutant(
        mutation,
        tests,
        extra=extra,
        workdir=workdir,
        timeout=timeout,
        ordinal=ordinal,
        maxfail=maxfail,
        native_collection=native_collection,
    )
    while True:
        result = poll_mutant(running)
        if result is not None:
            return result
        _wait_for_mutants((running,))


def _live_trace_events(directory: Path, positions: dict[Path, int]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for path in sorted(directory.glob("live-*.jsonl")):
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        lines = raw.splitlines()
        if raw and not raw.endswith("\n"):
            lines = lines[:-1]
        start = positions.get(path, 0)
        for line in lines[start:]:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        positions[path] = len(lines)
    return events


def _progressive_live_jobs(
    capacity: int,
    completed: int,
    total: int,
    *,
    max_live: int | None = None,
) -> int:
    """Ramp live mutation without serialising the ordinary test tail.

    The first slot opens when ten percent of the per-file activity blocks are
    complete. Further slots open linearly through the next eighty percent, but
    ``max_live`` keeps the measured test-worker floor intact until baseline
    seal; only the sealed scheduler inherits every CPU slot.
    """
    if capacity < 2 or total <= 0 or completed * 10 < total:
        return 0
    live_capacity = capacity - 1
    if max_live is not None:
        live_capacity = min(live_capacity, max_live)
    if live_capacity == 1:
        return 1
    progress_after_gate = min(8 * total, completed * 10 - total)
    additional = progress_after_gate * (live_capacity - 1) // (8 * total)
    return min(live_capacity, 1 + additional)


def _run_live_mutants(
    plan: Plan,
    repo: Path,
    stream_dir: Path,
    baseline_wait: Path,
    *,
    extra: Sequence[str],
    workdir: Path,
    timeout: float,
    maxfail: int,
    jobs: int,
    origin: float,
    emit: Callable[..., None],
    reclaim_jobs: int | None = None,
    progressive: bool = False,
    native_collection: Any | None = None,
    native_engine: bool = False,
    native_collections: dict[str, Any] | None = None,
) -> tuple[dict[int, Verdict], int, int, int, float | None]:
    """Try one completed green candidate while the baseline is still running.

    An early failure is conclusive: that exact test has already passed without
    the mutation and now fails with it. An early pass is deliberately only a
    probe; the sealed baseline may name later candidates, so the ordinary final
    scheduler runs that mutant again. This asymmetry lets gold appear early
    without ever turning partial evidence into a survivor.
    """
    watched: dict[tuple[str, int], list[tuple[int, Mutation]]] = {}
    for ordinal, mutation in enumerate(plan.mutations):
        if isinstance(mutation.patch, ValuePatch):
            continue
        site = Path(mutation.site.path)
        absolute = str(site if site.is_absolute() else (repo / site).resolve())
        for line in plan.watch.get(mutation.identifier, (mutation.site.line,)):
            watched.setdefault((absolute, line), []).append((ordinal, mutation))

    positions: dict[Path, int] = {}
    queued: deque[tuple[int, Mutation, str]] = deque()
    tried: set[int] = set()
    active: dict[int, tuple[RunningMutant, Mutation, str]] = {}
    owns_live_collections = native_collections is None
    live_collections = {} if native_collections is None else native_collections
    killed: dict[int, Verdict] = {}
    probes = 0
    completed_probes = 0
    cancelled_at_seal = 0
    first_started: float | None = None
    finished_suite_workers = 0
    total_blocks = 0
    completed_blocks: set[str] = set()
    reported_live_jobs = -1
    if reclaim_jobs is None:
        reclaim_jobs = jobs

    def record_completed(
        ordinal: int,
        mutation: Mutation,
        nodeid: str,
        result: tuple[Outcome, tuple[str, ...], float, str],
    ) -> None:
        nonlocal completed_probes
        completed_probes += 1
        outcome, killers, seconds, note = result
        if outcome == Outcome.KILLED:
            killed[ordinal] = Verdict(
                mutation,
                outcome,
                candidates=(nodeid,),
                killers=killers,
                seconds=seconds,
                note=note,
            )
            emit(
                "finished",
                ordinal=ordinal,
                outcome=outcome.value,
                killers=list(killers),
            )
        else:
            emit("finished", ordinal=ordinal, outcome="retry", killers=[])

    while not baseline_wait.exists() or active:
        if baseline_wait.exists() and active:
            # The complete candidate index is now available. Do not let a
            # speculative child extend the tail; the final scheduler will run
            # it against all sealed candidates instead. A child may have
            # completed between the preceding poll and the atomic seal. Its
            # result is conclusive and must be consumed before cancellation.
            for ordinal, (running, mutation, nodeid) in tuple(active.items()):
                result = poll_mutant(running)
                if result is not None:
                    del active[ordinal]
                    record_completed(ordinal, mutation, nodeid, result)
                    continue
                os.kill(running.pid, signal.SIGKILL)
                os.waitpid(running.pid, 0)
                if running.read_fd is not None:
                    os.close(running.read_fd)
                if running.pid_fd is not None:
                    os.close(running.pid_fd)
                running.target.unlink(missing_ok=True)
                emit("finished", ordinal=ordinal, outcome="retry", killers=[])
                cancelled_at_seal += 1
            active.clear()
            break

        if not baseline_wait.exists():
            for event in _live_trace_events(stream_dir, positions):
                if event.get("event") == "worker_finished":
                    finished_suite_workers += 1
                    continue
                if event.get("event") == "suite_started":
                    value = event.get("total_blocks")
                    if isinstance(value, int):
                        total_blocks = max(total_blocks, value)
                    continue
                if event.get("event") == "block_finished":
                    path = event.get("path")
                    if isinstance(path, str):
                        completed_blocks.add(path)
                    continue
                nodeid = event.get("nodeid")
                hits = event.get("hits")
                if not isinstance(nodeid, str) or not isinstance(hits, list):
                    continue
                if native_engine:
                    test_file = nodeid.split("::", 1)[0]
                    if live_collections.get(test_file) is None:
                        live_collections[test_file] = prepare_native_collection((test_file,))
                candidates: dict[int, Mutation] = {}
                for hit in hits:
                    if not isinstance(hit, list) or len(hit) != 2:
                        continue
                    path, line = hit
                    if not isinstance(path, str) or not isinstance(line, int):
                        continue
                    for ordinal, mutation in watched.get((path, line), ()):
                        candidates[ordinal] = mutation
                for ordinal, mutation in sorted(candidates.items()):
                    if ordinal in tried:
                        continue
                    tried.add(ordinal)
                    queued.append((ordinal, mutation, nodeid))

        if progressive:
            if total_blocks:
                live_jobs = _progressive_live_jobs(
                    reclaim_jobs,
                    len(completed_blocks),
                    total_blocks,
                    max_live=jobs,
                )
            else:
                live_jobs = _progressive_live_jobs(
                    reclaim_jobs,
                    finished_suite_workers,
                    reclaim_jobs,
                    max_live=jobs,
                )
        else:
            live_jobs = jobs
        if progressive and live_jobs != reported_live_jobs:
            emit(
                "capacity",
                test_workers=max(1, reclaim_jobs - live_jobs),
                mutant_workers=live_jobs,
            )
            reported_live_jobs = live_jobs
        while queued and len(active) < live_jobs and not baseline_wait.exists():
            ordinal, mutation, nodeid = queued.popleft()
            probes += 1
            if first_started is None:
                first_started = time.perf_counter() - origin
            emit(
                "started",
                ordinal=ordinal,
                phase="live",
                tests=[nodeid.split("::", 1)[0]],
            )
            selected_collection = native_collection
            if selected_collection is None and native_engine:
                test_file = nodeid.split("::", 1)[0]
                selected_collection = live_collections.get(test_file)
                if selected_collection is None:
                    selected_collection = prepare_native_collection((test_file,))
                    live_collections[test_file] = selected_collection
            active[ordinal] = (
                start_mutant(
                    mutation,
                    (nodeid,),
                    extra=extra,
                    workdir=workdir,
                    timeout=timeout,
                    ordinal=ordinal,
                    maxfail=maxfail,
                    native_collection=selected_collection,
                ),
                mutation,
                nodeid,
            )

        completed = False
        for ordinal, (running, mutation, nodeid) in tuple(active.items()):
            result = poll_mutant(running)
            if result is None:
                continue
            completed = True
            del active[ordinal]
            record_completed(ordinal, mutation, nodeid, result)
        if not active and not completed:
            # A live stream can exist before any watched green test completes.
            # Polling it flat-out stole a full logical CPU from the suite whose
            # idle slots this scheduler is meant to consume.
            time.sleep(0.01)
        elif not completed:
            _wait_for_mutants(
                tuple(item[0] for item in active.values()),
                ceiling=0.01,
            )
    if owns_live_collections:
        for collection in live_collections.values():
            release_native_collection(collection)
    if progressive:
        emit("capacity", test_workers=0, mutant_workers=reclaim_jobs)
    return (
        killed,
        probes,
        completed_probes,
        cancelled_at_seal,
        first_started,
    )


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
    sample: int = 0,
    changed: str | None = None,
    progress: bool = True,
    maxfail: int = DEFAULT_MAXFAIL,
    baseline: Baseline | None = None,
    baseline_wait: Path | None = None,
    baseline_stream: Path | None = None,
    budget: float = 0.0,
    jobs: int = DEFAULT_JOBS,
    reclaim_workers: bool = False,
    suite_workers: int = 0,
    preselected: frozenset[str] | None = None,
    activity_file: Path | None = None,
    test_engine: str = "native",
    differential_fuzz: DifferentialFuzzConfig | None = None,
) -> Report:
    started = time.perf_counter()
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    if suite_workers < 0:
        raise ValueError("suite workers must be non-negative")
    if test_engine not in {"pytest", "native"}:
        raise ValueError(f"unknown mutation test engine {test_engine!r}")
    if test_engine == "native" and extra:
        raise ValueError(
            "native mutation execution does not accept --pytest-arg; "
            "remove it or use --test-engine pytest"
        )
    selected_ids = preselected
    selection: SampleSelection | None = None
    if selected_ids is None and sample:
        selection = select_sample(
            roots,
            repo,
            sample,
            operators=operators,
            only=only,
            changed=changed,
        )
        selected_ids = frozenset(selection.identifiers)
    plan = build_plan(
        roots,
        repo,
        operators=operators,
        only=only,
        changed=changed,
        selected_ids=selected_ids,
    )
    if selection is not None:
        plan.errors[:0] = selection.errors
    if limit:
        plan.mutations = plan.mutations[:limit]
    report = Report(sources=tuple(plan.sources))
    if selection is not None:
        report.selection = selection.as_dict()

    def emit(event: str, **values: object) -> None:
        if activity_file is None:
            return
        with activity_file.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "event": event,
                        "at_seconds": round(time.perf_counter() - started, 3),
                        **values,
                    }
                )
                + "\n"
            )

    emit("planned", total=len(plan.mutations))

    if progress:
        print(
            f"wreath mutant: {len(plan.mutations)} mutation(s) in "
            f"{len(plan.sources)} file(s); {len(plan.errors)} declined.",
            file=sys.stderr,
        )

    reused_baseline = baseline is not None or baseline_wait is not None
    native_collection: Any | None = None
    live_native_collections: dict[str, Any] = {}
    if baseline_wait is None and test_engine == "pytest":
        import pytest

        # A known baseline narrows warmup to candidate-bearing files. The live
        # path deliberately does not collect the whole suite before it knows a
        # candidate: that repeated every import graph beside xdist and cost ten
        # CPU-seconds for a three-control sample. Its forked children import the
        # one completed green test they are actually handed instead.
        warm_targets = tests
        if baseline is not None:
            nodes = baseline.passed | frozenset(baseline.failed)
            warm_targets = tuple(sorted({node.split("::", 1)[0] for node in nodes}))
        with contextlib.redirect_stdout(io.StringIO()):
            pytest.main([*_PYTEST_BASE, "--collect-only", *extra, *warm_targets])
    if test_engine == "native":
        selected_node = next((target for target in tests if "::" in target), None)
        if selected_node is not None:
            raise ValueError(
                f"native mutation collection does not accept {selected_node!r}; "
                "pass its test file and let reachability select node ids"
            )
        if baseline_wait is None:
            native_collection = prepare_native_collection(tests)

    live_verdicts: dict[int, Verdict] = {}
    if baseline is None and baseline_wait is not None and baseline_stream is not None:
        (
            live_verdicts,
            report.live_probes,
            report.live_completed,
            report.live_cancelled_at_seal,
            report.live_first_started_seconds,
        ) = _run_live_mutants(
            plan,
            repo,
            baseline_stream,
            baseline_wait,
            extra=extra,
            workdir=workdir,
            timeout=timeout,
            maxfail=maxfail,
            jobs=jobs,
            reclaim_jobs=(
                max(jobs, min(suite_workers, os.cpu_count() or 1)) if reclaim_workers else jobs
            ),
            progressive=reclaim_workers,
            origin=started,
            emit=emit,
            native_collection=native_collection,
            native_engine=test_engine == "native",
            native_collections=(live_native_collections if test_engine == "native" else None),
        )
        report.live_kills = len(live_verdicts)
    if baseline is None and baseline_wait is not None:
        # ``wreath test`` starts this process beside the ordinary pytest
        # workers.  Planning, compilation and collection happen while their
        # tiles turn green; the atomic baseline appears only once the whole
        # run has sealed its pass/fail and trace evidence.
        while not baseline_wait.exists():
            time.sleep(0.01)
        baseline = read_baseline(baseline_wait)
    if baseline is None:
        baseline = (
            run_native_baseline(
                tests,
                plan,
                workdir=workdir,
                native_collection=native_collection,
            )
            if test_engine == "native"
            else run_baseline(tests, plan, extra=extra, workdir=workdir)
        )
    report.baseline_tests = len(baseline.passed) + len(baseline.failed)
    report.baseline_failures = baseline.failed
    report.baseline_seconds = baseline.seconds
    if progress:
        print(
            f"wreath mutant: baseline{' reused' if reused_baseline else ''} "
            f"{len(baseline.passed)} passed, "
            f"{len(baseline.failed)} failed, {baseline.seconds:.1f}s.",
            file=sys.stderr,
        )

    # Live probes are bounded by the ordinary suite's own window and are killed
    # at its seal, so charging them against this deadline only suppresses free
    # overlap. The explicit budget is the additional post-suite tail ceiling.
    remaining_budget = budget
    mutation_deadline = time.perf_counter() + remaining_budget if budget else None
    verdicts: dict[int, Verdict] = dict(live_verdicts)
    runnable: list[tuple[int, Mutation, tuple[str, ...]]] = []
    for ordinal, mutation in enumerate(plan.mutations):
        if ordinal in verdicts:
            continue
        selected = candidates_for(mutation, plan, baseline, repo)
        if not selected:
            verdicts[ordinal] = Verdict(
                mutation, Outcome.UNREACHED, note="no test executed this line"
            )
            emit(
                "finished",
                ordinal=ordinal,
                outcome=Outcome.UNREACHED.value,
                killers=[],
            )
            continue
        if len(selected) > max_candidates:
            verdicts[ordinal] = Verdict(
                mutation,
                Outcome.ERROR,
                candidates=selected,
                note=f"{len(selected)} candidate tests exceeds --max-candidates",
            )
            emit(
                "finished",
                ordinal=ordinal,
                outcome=Outcome.ERROR.value,
                killers=[],
            )
            continue
        runnable.append((ordinal, mutation, selected))

    # Under a bounded tail, finishing three small controls is strictly more
    # informative than timing all three out behind one broad control. The
    # original ordinal still owns the report/grid tile; only launch order moves.
    runnable.sort(key=lambda item: (len(item[2]), item[0]))
    if test_engine == "native" and runnable and native_collection is None:
        selected_nodes = itertools.chain.from_iterable(item[2] for item in runnable)
        candidate_files = tuple(sorted({node_id.split("::", 1)[0] for node_id in selected_nodes}))
        native_collection = pooled_native_collection(
            candidate_files,
            live_native_collections,
        )
    # During the semantic suite, the per-file completion ramp reserves CPU for
    # native mutation only after ten percent of the visible blocks are green.
    # Once the baseline seals all measured runner slots can be reclaimed without
    # adding idle CPU to the pipeline tail.
    # An explicit --mutant-workers value does not opt in, so resource ceilings
    # remain literal when a caller names one.
    scheduler_jobs = jobs
    if reclaim_workers:
        scheduler_jobs = max(jobs, min(suite_workers, os.cpu_count() or 1))

    active: dict[int, tuple[RunningMutant, Mutation, tuple[str, ...]]] = {}
    next_runnable = 0
    while next_runnable < len(runnable) or active:
        while next_runnable < len(runnable) and len(active) < scheduler_jobs:
            ordinal, mutation, selected = runnable[next_runnable]
            next_runnable += 1
            remaining = timeout
            if mutation_deadline is not None:
                remaining = min(remaining, mutation_deadline - time.perf_counter())
            if remaining <= 0:
                verdicts[ordinal] = Verdict(
                    mutation,
                    Outcome.TIMEOUT,
                    candidates=selected,
                    note=f"total mutation budget of {budget:g}s was exhausted; undecided",
                )
                emit(
                    "finished",
                    ordinal=ordinal,
                    outcome=Outcome.TIMEOUT.value,
                    killers=[],
                )
                continue
            emit(
                "started",
                ordinal=ordinal,
                tests=sorted({nodeid.split("::", 1)[0] for nodeid in selected}),
            )
            active[ordinal] = (
                start_mutant(
                    mutation,
                    selected,
                    extra=extra,
                    workdir=workdir,
                    timeout=remaining,
                    ordinal=ordinal,
                    maxfail=maxfail,
                    native_collection=native_collection,
                ),
                mutation,
                selected,
            )
        completed = False
        for ordinal, (running, mutation, selected) in tuple(active.items()):
            result = poll_mutant(running)
            if result is None:
                continue
            completed = True
            del active[ordinal]
            outcome, killers, seconds, note = result
            verdicts[ordinal] = Verdict(
                mutation,
                outcome,
                candidates=selected,
                killers=killers,
                seconds=seconds,
                note=note,
            )
            emit(
                "finished",
                ordinal=ordinal,
                outcome=outcome.value,
                killers=list(killers),
            )
            if progress:
                print(
                    f"  [{ordinal + 1}/{len(plan.mutations)}] {outcome.value:<10} "
                    f"{mutation.identifier} ({len(selected)} test(s), {seconds:.2f}s)",
                    file=sys.stderr,
                )
        if active and not completed:
            _wait_for_mutants(tuple(item[0] for item in active.values()))

    report.verdicts.extend(verdicts[ordinal] for ordinal in range(len(plan.mutations)))

    for identifier, reason in plan.errors:
        report.verdicts.append(
            Verdict(
                Mutation(identifier, "-", reason, Site("-", 0, "-"), "-"),
                Outcome.EQUIVALENT if "same bytecode" in reason else Outcome.ERROR,
                note=reason,
            )
        )
    if differential_fuzz is not None:
        apply_differential_fuzz(report, differential_fuzz, workdir=workdir)
    report.total_seconds = time.perf_counter() - started
    if live_native_collections:
        for collection in unique_native_collections(live_native_collections.values()):
            release_native_collection(collection)
    elif native_collection is not None:
        release_native_collection(native_collection)
    return report
