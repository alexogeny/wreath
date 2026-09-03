"""Collection and rendering for Wreath's native pytest-compatible engine."""

from __future__ import annotations

import ast
import asyncio
import configparser
import contextvars
import enum
import fnmatch
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import select
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import traceback
import warnings
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from . import _pytest_facade as facade
from ._native import _testrunner

_NATIVE_OPTIONS = "-k, -m, -q, -x/--exitfirst, --maxfail, and --collect-only"
_NATIVE_REPORTING_HOOKS = frozenset(
    {
        "pytest_deselected",
        "pytest_runtest_logreport",
        "pytest_sessionfinish",
        "pytest_terminal_summary",
        "pytest_testnodedown",
    }
)


@dataclass(frozen=True, slots=True)
class Options:
    paths: tuple[Path, ...]
    keyword: str | None = None
    markers: str | None = None
    quiet: bool = False
    max_failures: int = 0
    collect_only: bool = False
    mutation_mode: bool = False


@dataclass(frozen=True, slots=True)
class Case:
    node_id: str
    function: Callable[..., Any]
    arguments: tuple[Any, ...]
    skip_exception: facade.Skipped | None
    marks: frozenset[str]

    def native(self) -> tuple[str, Callable[..., Any], tuple[Any, ...], Any]:
        return self.node_id, self.function, self.arguments, self.skip_exception

    def has_mark(self, name: str) -> bool:
        return name in self.marks


@dataclass(frozen=True, slots=True)
class Collection:
    cases: tuple[Case, ...]
    modules: tuple[str, ...]
    files: tuple[Path, ...] = ()
    runtime: _FixtureRuntime | None = None
    index: dict[str, Case] = field(default_factory=dict)


def _schedule_fuzz_cases(cases: Iterable[Case], seed: str) -> tuple[Case, ...]:
    """Return one stable, seed-derived order for execution-condition fuzzing."""
    return tuple(
        sorted(
            cases,
            key=lambda case: hashlib.sha256(f"{seed}\0{case.node_id}".encode()).digest(),
        )
    )


@dataclass(frozen=True, slots=True)
class FixtureDef:
    name: str
    function: Callable[..., Any]
    dependencies: tuple[str, ...]
    autouse: bool
    scope: str
    params: tuple[Any, ...] | None
    param_ids: Any
    source: str


@dataclass(frozen=True, slots=True)
class Result:
    node_id: str
    outcome: str
    duration_ns: int
    exception: BaseException | None


type _RawResult = tuple[str, str, int, BaseException | None]


@dataclass(frozen=True, slots=True)
class _FixtureNode:
    node_id: str
    marks: tuple[facade.Mark, ...]

    def get_closest_marker(self, name: str) -> facade.Mark | None:
        return next((mark for mark in reversed(self.marks) if mark.name == name), None)


class _FixtureRequest:
    __slots__ = ("_has_param", "_param", "_resolve", "_stack", "node")

    def __init__(
        self,
        node: _FixtureNode,
        resolve: Callable[[str], Any],
        stack: Any,
        param: Any = None,
        has_param: bool = False,
    ) -> None:
        self.node = node
        self._resolve = resolve
        self._stack = stack
        self._param = param
        self._has_param = has_param

    @property
    def param(self) -> Any:
        if not self._has_param:
            raise AttributeError("request.param is available only in a parametrized fixture")
        return self._param

    def getfixturevalue(self, name: str) -> Any:
        return self._resolve(name)

    def addfinalizer(self, finalizer: Callable[[], Any]) -> None:
        self._stack.callback(finalizer)


class _TempPathFactory:
    def __init__(self, root: Path) -> None:
        self._root = root

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        del numbered
        return Path(tempfile.mkdtemp(prefix=f"{basename}-", dir=self._root))


@dataclass(frozen=True, slots=True)
class _Captured:
    out: str
    err: str


class _CaptureFixture:
    def __init__(self, stack: Any) -> None:
        self.stdout = StringIO()
        self.stderr = StringIO()
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = self.stdout
        sys.stderr = self.stderr
        stack.callback(self._restore)

    def _restore(self) -> None:
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr

    def readouterr(self) -> _Captured:
        captured = _Captured(self.stdout.getvalue(), self.stderr.getvalue())
        self.stdout.seek(0)
        self.stdout.truncate()
        self.stderr.seek(0)
        self.stderr.truncate()
        return captured

    @contextmanager
    def disabled(self) -> Iterator[None]:
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        try:
            yield
        finally:
            sys.stdout = self.stdout
            sys.stderr = self.stderr


class _ListLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _LogCaptureFixture:
    def __init__(self, stack: Any) -> None:
        self.handler = _ListLogHandler()
        logging.getLogger().addHandler(self.handler)
        stack.callback(logging.getLogger().removeHandler, self.handler)

    @property
    def records(self) -> list[logging.LogRecord]:
        return self.handler.records

    @property
    def text(self) -> str:
        return "\n".join(self.handler.format(record) for record in self.records)

    @contextmanager
    def at_level(self, level: int, logger: str | None = None) -> Iterator[None]:
        selected = logging.getLogger(logger)
        previous = selected.level
        selected.setLevel(level)
        try:
            yield
        finally:
            selected.setLevel(previous)


class _OutputLines:
    def __init__(self, value: str) -> None:
        self.value = value

    def fnmatch_lines(self, patterns: Sequence[str]) -> None:
        lines = self.value.splitlines()
        missing = next(
            (
                pattern
                for pattern in patterns
                if not any(fnmatch.fnmatch(line, pattern) for line in lines)
            ),
            None,
        )
        if missing is not None:
            raise AssertionError(f"pattern {missing!r} not found in output:\n{self.value}")


class _PytesterResult:
    def __init__(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.ret = completed.returncode
        self.stdout = _OutputLines(completed.stdout)
        self.stderr = _OutputLines(completed.stderr)
        self._combined = completed.stdout + completed.stderr

    def assert_outcomes(self, **expected: int) -> None:
        observed = {
            match.group("outcome"): int(match.group("count"))
            for match in re.finditer(
                r"(?P<count>\d+)\s+(?P<outcome>passed|failed|skipped|errors?)\b",
                self._combined,
            )
        }
        for outcome, count in expected.items():
            label = "error" if outcome == "errors" else outcome
            if observed.get(label, 0) != count:
                raise AssertionError(
                    f"expected {count} {outcome}, got subprocess output:\n{self._combined}"
                )


class _Pytester:
    """The subprocess-oriented pytester slice used by Wreath's plugin contract."""

    def __init__(self, stack: ExitStack) -> None:
        directory = tempfile.TemporaryDirectory(prefix="wreath-pytester-")
        stack.callback(directory.cleanup)
        self.path = Path(directory.name)
        self._next_file = 0

    def makeini(self, source: str) -> Path:
        target = self.path / "pytest.ini"
        target.write_text(textwrap.dedent(source), encoding="utf-8")
        return target

    def makeconftest(self, source: str) -> Path:
        target = self.path / "conftest.py"
        target.write_text(textwrap.dedent(source), encoding="utf-8")
        return target

    def makepyfile(self, source: str) -> Path:
        self._next_file += 1
        target = self.path / f"test_generated_{self._next_file}.py"
        target.write_text(textwrap.dedent(source), encoding="utf-8")
        return target

    def runpytest_subprocess(self, *arguments: str) -> _PytesterResult:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *arguments],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
        )
        return _PytesterResult(completed)


class _FixtureRuntime:
    def __init__(self) -> None:
        self.session_values: dict[str, Any] = {}
        self.module_values: dict[tuple[str, str], Any] = {}
        self.session_stack = ExitStack()
        self.module_stacks: dict[str, ExitStack] = {}
        self.async_session_values: dict[str, Any] = {}
        self.async_module_values: dict[tuple[str, str], Any] = {}
        self.async_session_stack = AsyncExitStack()
        self.async_module_stacks: dict[str, AsyncExitStack] = {}
        self.async_runner = asyncio.Runner()
        self.base_context = contextvars.copy_context()
        self._temp_directory: tempfile.TemporaryDirectory[str] | None = None

    def temp_root(self) -> Path:
        if self._temp_directory is None:
            self._temp_directory = tempfile.TemporaryDirectory(prefix="wreath-native-tmp-")
        return Path(self._temp_directory.name)

    def storage(self, scope: str, module_id: str) -> tuple[dict[Any, Any], Any, ExitStack]:
        if scope == "session":
            return self.session_values, "session", self.session_stack
        stack = self.module_stacks.setdefault(module_id, ExitStack())
        return self.module_values, (module_id,), stack

    def async_storage(
        self, scope: str, module_id: str
    ) -> tuple[dict[Any, Any], Any, AsyncExitStack]:
        if scope == "session":
            return self.async_session_values, "session", self.async_session_stack
        stack = self.async_module_stacks.setdefault(module_id, AsyncExitStack())
        return self.async_module_values, (module_id,), stack

    def run_async(self, awaitable: Any) -> Any:
        return self.async_runner.run(awaitable, context=self.base_context.copy())

    def close(self) -> None:
        for stack in reversed(tuple(self.module_stacks.values())):
            stack.close()
        self.session_stack.close()
        for stack in reversed(tuple(self.async_module_stacks.values())):
            self.async_runner.run(stack.aclose())
        self.async_runner.run(self.async_session_stack.aclose())
        self.async_runner.close()
        if self._temp_directory is not None:
            self._temp_directory.cleanup()


class _FixtureCall:
    """One immutable fixture plan executed with per-case values and cleanup.

    None of Wreath's reusable declarative owners describe test-resource
    lifetime or dependency teardown, so this deliberately remains local to the
    runner instead of overloading an application, lease, or cache primitive.
    """

    def __init__(
        self,
        function: Callable[..., Any],
        parameter_names: tuple[str, ...],
        assigned: dict[str, Any],
        fixtures: dict[str, FixtureDef],
        autouse: tuple[str, ...],
        node_id: str,
        marks: tuple[facade.Mark, ...],
        runtime: _FixtureRuntime,
        module_id: str,
        fixture_params: dict[str, tuple[int, Any]],
        async_mode: bool,
    ) -> None:
        self.function = function
        self.parameter_names = parameter_names
        self.assigned = dict(assigned)
        self.fixtures = dict(fixtures)
        self.autouse = autouse
        self.node = _FixtureNode(node_id, marks)
        self.runtime = runtime
        self.module_id = module_id
        self.fixture_params = dict(fixture_params)
        self.async_mode = async_mode

    def __call__(self) -> Any:
        if isinstance(self.function, _TestMethod):
            self.function.begin()
        if self.async_mode:
            return self.runtime.run_async(self._call_async())
        values = dict(self.assigned)
        resolving: list[str] = []
        with ExitStack() as stack:

            def resolve(name: str, owner_stack: ExitStack = stack) -> Any:
                if name in values:
                    return values[name]
                if name == "request":
                    return _FixtureRequest(self.node, resolve, owner_stack)
                if name == "monkeypatch":
                    monkeypatch = facade.MonkeyPatch()
                    owner_stack.callback(monkeypatch.undo)
                    if owner_stack is stack:
                        values[name] = monkeypatch
                    return monkeypatch
                if name == "tmp_path_factory":
                    factory = _TempPathFactory(self.runtime.temp_root())
                    if owner_stack is stack:
                        values[name] = factory
                    return factory
                if name == "tmp_path":
                    path = _TempPathFactory(self.runtime.temp_root()).mktemp("test")
                    if owner_stack is stack:
                        values[name] = path
                    return path
                if name == "capsys":
                    capture = _CaptureFixture(owner_stack)
                    if owner_stack is stack:
                        values[name] = capture
                    return capture
                if name == "caplog":
                    capture = _LogCaptureFixture(owner_stack)
                    # ``caplog`` is function-scoped. Collection rejects a
                    # broader fixture that asks for it, so the owner is always
                    # this case's stack at every reachable call.
                    values[name] = capture
                    return capture
                if name == "recwarn":
                    captured = owner_stack.enter_context(warnings.catch_warnings(record=True))
                    warnings.simplefilter("always")
                    if owner_stack is stack:
                        values[name] = captured
                    return captured
                if name == "pytester":
                    pytester = _Pytester(owner_stack)
                    if owner_stack is stack:
                        values[name] = pytester
                    return pytester
                definition = self.fixtures[name]
                fixture_stack: Any = owner_stack
                cache: dict[Any, Any] = values
                cache_key: Any = name
                if definition.scope != "function":
                    cache, prefix, fixture_stack = self.runtime.storage(
                        definition.scope, self.module_id
                    )
                    cache_key = (*prefix, name) if isinstance(prefix, tuple) else name
                    if cache_key in cache:
                        return cache[cache_key]
                resolving.append(name)
                try:
                    selected_param = self.fixture_params.get(name)
                    arguments = tuple(
                        _FixtureRequest(
                            self.node,
                            resolve,
                            fixture_stack,
                            None if selected_param is None else selected_param[1],
                            selected_param is not None,
                        )
                        if item == "request"
                        else resolve(item, fixture_stack)
                        for item in definition.dependencies
                    )
                    if inspect.isgeneratorfunction(definition.function):
                        value = fixture_stack.enter_context(_yield_fixture(definition, arguments))
                    else:
                        value = definition.function(*arguments)
                finally:
                    resolving.pop()
                cache[cache_key] = value
                return value

            for name in self.autouse:
                resolve(name)
            arguments = tuple(resolve(name) for name in self.parameter_names)
            result = self.function(*arguments)
            if inspect.isawaitable(result):
                return asyncio.run(result)
            return result

    async def _call_async(self) -> Any:
        values = dict(self.assigned)
        resolving: list[str] = []
        async with AsyncExitStack() as stack:

            async def resolve(name: str, owner_stack: Any = stack) -> Any:
                if name in values:
                    return values[name]
                if name == "request":
                    return _FixtureRequest(
                        self.node,
                        _async_fixture_lookup,
                        owner_stack,
                    )
                if name == "monkeypatch":
                    monkeypatch = facade.MonkeyPatch()
                    owner_stack.callback(monkeypatch.undo)
                    values[name] = monkeypatch
                    return monkeypatch
                if name == "tmp_path_factory":
                    factory = _TempPathFactory(self.runtime.temp_root())
                    values[name] = factory
                    return factory
                if name == "tmp_path":
                    path = _TempPathFactory(self.runtime.temp_root()).mktemp("test")
                    values[name] = path
                    return path
                if name == "capsys":
                    capture = _CaptureFixture(owner_stack)
                    values[name] = capture
                    return capture
                if name == "caplog":
                    capture = _LogCaptureFixture(owner_stack)
                    values[name] = capture
                    return capture
                if name == "recwarn":
                    captured = owner_stack.enter_context(warnings.catch_warnings(record=True))
                    warnings.simplefilter("always")
                    values[name] = captured
                    return captured
                if name == "pytester":
                    pytester = _Pytester(cast("ExitStack", owner_stack))
                    values[name] = pytester
                    return pytester
                definition = self.fixtures[name]
                fixture_stack: Any = owner_stack
                cache: dict[Any, Any] = values
                cache_key: Any = name
                is_async_fixture = inspect.iscoroutinefunction(
                    definition.function
                ) or inspect.isasyncgenfunction(definition.function)
                if definition.scope != "function":
                    if is_async_fixture:
                        cache, prefix, fixture_stack = self.runtime.async_storage(
                            definition.scope, self.module_id
                        )
                    else:
                        cache, prefix, fixture_stack = self.runtime.storage(
                            definition.scope, self.module_id
                        )
                    cache_key = (*prefix, name) if isinstance(prefix, tuple) else name
                    if cache_key in cache:
                        return cache[cache_key]
                resolving.append(name)
                try:
                    selected_param = self.fixture_params.get(name)
                    arguments = tuple(
                        [
                            _FixtureRequest(
                                self.node,
                                _async_fixture_lookup,
                                fixture_stack,
                                None if selected_param is None else selected_param[1],
                                selected_param is not None,
                            )
                            if item == "request"
                            else await resolve(item, fixture_stack)
                            for item in definition.dependencies
                        ]
                    )
                    if inspect.isasyncgenfunction(definition.function):
                        value = await cast(
                            "AsyncExitStack[Any]", fixture_stack
                        ).enter_async_context(_async_yield_fixture(definition, arguments))
                    elif inspect.iscoroutinefunction(definition.function):
                        value = await definition.function(*arguments)
                    elif inspect.isgeneratorfunction(definition.function):
                        value = fixture_stack.enter_context(_yield_fixture(definition, arguments))
                    else:
                        value = definition.function(*arguments)
                finally:
                    resolving.pop()
                cache[cache_key] = value
                return value

            for name in self.autouse:
                await resolve(name)
            arguments = tuple([await resolve(name) for name in self.parameter_names])
            result = self.function(*arguments)
            if inspect.isawaitable(result):
                return await result
            return result


def _async_fixture_lookup(name: str) -> Any:
    raise ValueError(
        f"async fixture request.getfixturevalue({name!r}) is not supported; "
        "declare the fixture as a function argument"
    )


class _TestMethod:
    def __init__(self, owner: type[Any], name: str) -> None:
        self.owner = owner
        self.name = name
        self._instance: Any = None

    def begin(self) -> None:
        self._instance = self.owner()

    def instance(self) -> Any:
        if self._instance is None:
            raise RuntimeError("test class instance is unavailable before case setup")
        return self._instance

    def __call__(self, *arguments: Any) -> Any:
        return getattr(self.instance(), self.name)(*arguments)


def _bound_fixture_callable(
    method_call: _TestMethod, function: Callable[..., Any]
) -> Callable[..., Any]:
    if inspect.isasyncgenfunction(function):

        async def async_generator(*arguments: Any) -> Any:
            async for value in function(method_call.instance(), *arguments):
                yield value

        return async_generator
    if inspect.iscoroutinefunction(function):

        async def coroutine(*arguments: Any) -> Any:
            return await function(method_call.instance(), *arguments)

        return coroutine
    if inspect.isgeneratorfunction(function):

        def generator(*arguments: Any) -> Any:
            yield from function(method_call.instance(), *arguments)

        return generator

    def call(*arguments: Any) -> Any:
        return function(method_call.instance(), *arguments)

    return call


@contextmanager
def _yield_fixture(definition: FixtureDef, arguments: tuple[Any, ...]) -> Iterator[Any]:
    generator = definition.function(*arguments)
    try:
        value = next(generator)
    except StopIteration as error:
        raise ValueError(
            f"{definition.source}: fixture {definition.name!r} did not yield a value"
        ) from error
    try:
        yield value
    finally:
        try:
            next(generator)
        except StopIteration:
            pass
        else:
            generator.close()
            raise ValueError(
                f"{definition.source}: fixture {definition.name!r} yielded more than once"
            )


@asynccontextmanager
async def _async_yield_fixture(definition: FixtureDef, arguments: tuple[Any, ...]) -> Any:
    generator = definition.function(*arguments)
    try:
        value = await anext(generator)
    except StopAsyncIteration as error:
        raise ValueError(
            f"{definition.source}: fixture {definition.name!r} did not yield a value"
        ) from error
    try:
        yield value
    finally:
        try:
            await anext(generator)
        except StopAsyncIteration:
            pass
        else:
            await generator.aclose()
            raise ValueError(
                f"{definition.source}: fixture {definition.name!r} yielded more than once"
            )


@contextmanager
def _facade_import() -> Iterator[None]:
    had_previous = "pytest" in sys.modules
    previous = sys.modules.get("pytest")
    sys.modules["pytest"] = facade
    try:
        yield
    finally:
        if had_previous and previous is not None:
            sys.modules["pytest"] = previous
        else:
            sys.modules.pop("pytest", None)


@contextmanager
def _test_import_paths(files: Sequence[Path]) -> Iterator[None]:
    roots = {str(Path.cwd().resolve())}
    for path in files:
        roots.add(str(path.parent))
        roots.update(str(conftest.parent) for conftest in _conftest_paths(path.parent))
    current = set(sys.path)
    inserted = sorted(root for root in roots if root not in current)
    sys.path[:0] = inserted
    try:
        yield
    finally:
        inserted_set = set(inserted)
        sys.path[:] = [root for root in sys.path if root not in inserted_set]


def _parse(arguments: Sequence[str]) -> Options:
    paths: list[Path] = []
    keyword: str | None = None
    markers: str | None = None
    quiet = False
    max_failures = 0
    collect_only = False
    index = 0
    values = list(arguments)
    if values[:1] == ["--"]:
        values.pop(0)
    while index < len(values):
        argument = values[index]
        if argument in {"-q", "--quiet"}:
            quiet = True
        elif argument in {"-x", "--exitfirst"}:
            max_failures = 1
        elif argument == "--collect-only":
            collect_only = True
        elif argument in {"-k", "-m", "--maxfail"}:
            if index + 1 >= len(values):
                raise ValueError(f"{argument} needs a value")
            index += 1
            value = values[index]
            if argument == "-k":
                keyword = value
            elif argument == "-m":
                markers = value
            else:
                max_failures = _non_negative_int("--maxfail", value)
        elif argument.startswith("--maxfail="):
            max_failures = _non_negative_int("--maxfail", argument.partition("=")[2])
        elif argument.startswith("-k") and argument != "-k":
            keyword = argument[2:]
        elif argument.startswith("-m") and argument != "-m":
            markers = argument[2:]
        elif argument.startswith("-"):
            raise ValueError(
                f"{argument} is not supported by the native engine; "
                f"native options are {_NATIVE_OPTIONS}"
            )
        else:
            if "::" in argument:
                raise ValueError(
                    f"native selection does not yet support {argument!r}; "
                    "select a test file or directory and use -k for test names"
                )
            paths.append(Path(argument))
        index += 1
    return Options(
        paths=tuple(paths or (Path("tests"),)),
        keyword=keyword,
        markers=markers,
        quiet=quiet,
        max_failures=max_failures,
        collect_only=collect_only,
    )


def _configured_markers(directory: Path | None = None) -> str | None:
    root = (directory or Path.cwd()).resolve()
    addopts: str | Sequence[str] | None = None
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            addopts = (
                document.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts")
            )
        except OSError, tomllib.TOMLDecodeError:
            addopts = None
    if addopts is None:
        for name in ("pytest.ini", "tox.ini", "setup.cfg"):
            candidate = root / name
            if not candidate.is_file():
                continue
            parser = configparser.ConfigParser()
            try:
                parser.read(candidate, encoding="utf-8")
            except configparser.Error, OSError:
                continue
            for section in ("pytest", "tool:pytest"):
                addopts = parser.get(section, "addopts", fallback=None)
                if addopts is not None:
                    break
            if addopts is not None:
                break
    values = shlex.split(addopts) if isinstance(addopts, str) else list(addopts or ())
    for index, argument in enumerate(values):
        if argument == "-m" and index + 1 < len(values):
            return values[index + 1]
        if argument.startswith("-m") and argument != "-m":
            return argument[2:]
    return None


def _non_negative_int(option: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{option} must be a non-negative integer") from error
    if parsed < 0:
        raise ValueError(f"{option} must be a non-negative integer")
    return parsed


def _validate_runner_options(namespace: Any) -> None:
    if str(namespace.collection) == "replicated":
        raise ValueError(
            "--engine native does not replicate collection; use --collection auto or sharded"
        )


def _selected_files(
    options: Options,
    conftest_path_cache: dict[Path, tuple[Path, ...]],
) -> tuple[Path, ...]:
    files: set[Path] = set()
    for selected in options.paths:
        path = selected.resolve()
        if not path.exists():
            raise ValueError(f"test path does not exist: {selected}")
        if path.is_file():
            if path.name == "conftest.py":
                raise ValueError(f"{_display_path(path)} is not supported by the native engine")
            if not path.name.startswith("test_") or path.suffix != ".py":
                raise ValueError(f"native test file {selected} must be named test_*.py")
            files.add(path)
            continue
        files.update(candidate.resolve() for candidate in path.rglob("test_*.py"))
    ignore_cache: dict[Path, tuple[tuple[str, tuple[str, ...]], ...]] = {}
    return tuple(
        sorted(
            (
                path
                for path in files
                if not _ignored_by_conftest(
                    path,
                    ignore_cache,
                    conftest_path_cache,
                )
            ),
            key=lambda item: _display_path(item),
        )
    )


def _ignored_by_conftest(
    path: Path,
    cache: dict[Path, tuple[tuple[str, tuple[str, ...]], ...]],
    conftest_path_cache: dict[Path, tuple[Path, ...]],
) -> bool:
    """Apply pytest's declarative ``collect_ignore(_glob)`` file contract.

    These are collection facts, not lifecycle hooks. Reading their literal
    declarations before import keeps foreign source corpora out of the import
    graph, which is exactly why pytest exposes the declarations.
    """
    for conftest in _cached_conftest_paths(path.parent, conftest_path_cache):
        declarations = cache.get(conftest)
        if declarations is None:
            declarations = _collect_ignore_declarations(conftest)
            cache[conftest] = declarations
        relative = path.relative_to(conftest.parent)
        for kind, patterns in declarations:
            for pattern in patterns:
                if kind == "collect_ignore_glob" and relative.match(pattern):
                    return True
                if kind == "collect_ignore" and relative == Path(pattern):
                    return True
    return False


def _collect_ignore_declarations(
    conftest: Path,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    try:
        tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))
    except OSError, SyntaxError:
        return ()
    declarations: list[tuple[str, tuple[str, ...]]] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        names: set[str] = set()
        for target in statement.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
        selected = names & {"collect_ignore", "collect_ignore_glob"}
        if not selected:
            continue
        kind = min(selected)
        try:
            patterns = ast.literal_eval(statement.value)
        except TypeError, ValueError:
            raise ValueError(
                f"{_display_path(conftest)}: {kind} must be a literal "
                "sequence of paths or glob patterns"
            ) from None
        if not isinstance(patterns, list | tuple):
            raise ValueError(
                f"{_display_path(conftest)}: collect ignore declaration must be a literal sequence"
            )
        if not all(isinstance(pattern, str) for pattern in patterns):
            raise ValueError(f"{_display_path(conftest)}: collect ignore entries must be strings")
        frozen_patterns = tuple(patterns)
        for name in sorted(selected):
            declarations.append((name, frozen_patterns))
    return tuple(declarations)


def _conftest_paths(directory: Path) -> tuple[Path, ...]:
    resolved = directory.resolve()
    cwd = Path.cwd().resolve()
    if resolved.is_relative_to(cwd):
        relative = resolved.relative_to(cwd)
        directories = [cwd]
        current = cwd
        for part in relative.parts:
            current /= part
            directories.append(current)
    else:
        directories = list(reversed((resolved, *resolved.parents)))
    return tuple(
        candidate for parent in directories if (candidate := parent / "conftest.py").is_file()
    )


def _cached_conftest_paths(
    directory: Path,
    cache: dict[Path, tuple[Path, ...]],
) -> tuple[Path, ...]:
    resolved = directory.resolve()
    paths = cache.get(resolved)
    if paths is None:
        paths = _conftest_paths(resolved)
        cache[resolved] = paths
    return paths


def collect(options: Options) -> Collection:
    """Collect every selected module before returning any executable cases."""
    conftest_path_cache: dict[Path, tuple[Path, ...]] = {}
    files = _selected_files(options, conftest_path_cache)
    cases: list[Case] = []
    modules: list[str] = []
    conftest_cache: dict[Path, tuple[str, tuple[FixtureDef, ...]]] = {}
    runtime = _FixtureRuntime()
    with _facade_import():
        try:
            for path in files:
                inherited: dict[str, FixtureDef] = {}
                for conftest_path in _cached_conftest_paths(
                    path.parent,
                    conftest_path_cache,
                ):
                    loaded = conftest_cache.get(conftest_path)
                    if loaded is None:
                        conftest_name = _module_name(conftest_path)
                        conftest = _load_module(conftest_path, conftest_name)
                        loaded = (
                            conftest_name,
                            _fixture_defs(
                                conftest_path,
                                conftest,
                                conftest=True,
                            ),
                        )
                        conftest_cache[conftest_path] = loaded
                        modules.append(conftest_name)
                    for definition in loaded[1]:
                        inherited[definition.name] = definition
                module_name = _module_name(path)
                module = _load_module(path, module_name)
                modules.append(module_name)
                local = _fixture_defs(path, module, conftest=False)
                fixtures = {**inherited, **{item.name: item for item in local}}
                cases.extend(_collect_module(path, module, fixtures, runtime))
        except BaseException:
            _forget_modules(modules)
            raise
    keyword_matcher = (
        _compile_matcher(options.keyword, exact_atoms=False)
        if options.keyword is not None and options.keyword.strip()
        else None
    )
    marker_matcher = (
        _compile_matcher(options.markers, exact_atoms=True)
        if options.markers is not None and options.markers.strip()
        else None
    )
    selected = tuple(
        case
        for case in cases
        if (keyword_matcher is None or keyword_matcher(case.node_id))
        and (marker_matcher is None or marker_matcher(case.marks))
    )
    return Collection(
        selected,
        tuple(modules),
        files,
        runtime,
        {case.node_id: case for case in selected},
    )


def _module_name(path: Path) -> str:
    resolved = path.resolve()
    cwd = Path.cwd().resolve()
    if resolved.is_relative_to(cwd):
        relative = resolved.relative_to(cwd)
        parents = tuple(resolved.parents[: len(relative.parts) - 1])
        if any((parent / "__init__.py").is_file() for parent in parents):
            return ".".join(relative.with_suffix("").parts)
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
    return f"_wreath_native_{digest}_{path.stem}"


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"could not load native test module {_display_path(path)}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    import_roots = [str(Path.cwd().resolve()), str(path.parent)]
    import_roots.extend(
        str(parent) for parent in path.parents if (parent / "conftest.py").is_file()
    )
    current_roots = set(sys.path)
    inserted = [root for root in dict.fromkeys(import_roots) if root not in current_roots]
    sys.path[:0] = inserted
    try:
        spec.loader.exec_module(module)
    except facade.Skipped as error:
        sys.modules.pop(module_name, None)
        raise ValueError(
            f"{_display_path(path)} called pytest.skip during import; "
            "use @pytest.mark.skip on each test"
        ) from error
    except (ImportError, SyntaxError) as error:
        sys.modules.pop(module_name, None)
        raise ValueError(
            f"{_display_path(path)} could not be imported: {type(error).__name__}: {error}"
        ) from error
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    finally:
        inserted_roots = set(inserted)
        sys.path[:] = [root for root in sys.path if root not in inserted_roots]
    return module


def _fixture_defs(
    path: Path,
    module: ModuleType,
    *,
    conftest: bool,
) -> tuple[FixtureDef, ...]:
    display_path = _display_path(path)
    definitions: list[FixtureDef] = []
    for name, value in module.__dict__.items():
        if name == "pytest_plugins":
            _validate_pytest_plugins(display_path, value)
            continue
        if (
            conftest
            and name.startswith("pytest_")
            and callable(value)
            and name not in _NATIVE_REPORTING_HOOKS
        ):
            raise ValueError(
                f"{display_path}: hook {name!r} is not supported by the native engine; "
                "use --engine pytest"
            )
        options = getattr(value, "__wreath_fixture__", None)
        if options is None or not callable(value):
            continue
        fixture_name = options["name"] or name
        scope = options["scope"]
        if scope not in {"function", "module", "session"}:
            raise ValueError(
                f"{display_path}: fixture {fixture_name!r} uses scope={scope!r}; "
                "the native engine supports scope='function', 'module', or 'session'"
            )
        if options["params"] is not None and scope != "function":
            raise ValueError(
                f"{display_path}: parametrized fixture {fixture_name!r} uses "
                f"scope={scope!r}; native parametrized fixtures require scope='function'"
            )
        dependencies = _positional_parameters(display_path, "fixture", fixture_name, value)
        definitions.append(
            FixtureDef(
                fixture_name,
                value,
                dependencies,
                bool(options["autouse"]),
                scope,
                options["params"],
                options["ids"],
                display_path,
            )
        )
    return tuple(definitions)


def _validate_pytest_plugins(display_path: str, value: Any) -> None:
    plugins = (value,) if isinstance(value, str) else tuple(value)
    unsupported = next((plugin for plugin in plugins if plugin != "pytester"), None)
    if unsupported is not None:
        raise ValueError(
            f"{display_path}: pytest plugin {unsupported!r} is not supported by the "
            "native engine; declare fixtures directly in conftest.py"
        )


def _positional_parameters(
    display_path: str,
    kind: str,
    name: str,
    function: Callable[..., Any],
) -> tuple[str, ...]:
    parameters = tuple(inspect.signature(function).parameters.values())
    for parameter in parameters:
        if parameter.kind not in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            raise ValueError(
                f"{display_path}: {kind} {name!r} parameter {parameter.name!r} "
                "must be an ordinary positional parameter"
            )
    return tuple(parameter.name for parameter in parameters)


_BUILTIN_FIXTURES = frozenset(
    {
        "capsys",
        "caplog",
        "monkeypatch",
        "pytester",
        "recwarn",
        "request",
        "tmp_path",
        "tmp_path_factory",
    }
)
_FIXTURE_SCOPE_RANK = {"function": 0, "module": 1, "session": 2}
_BUILTIN_SCOPE = {
    "capsys": "function",
    "caplog": "function",
    "monkeypatch": "function",
    "pytester": "function",
    "recwarn": "function",
    "request": "session",
    "tmp_path": "function",
    "tmp_path_factory": "session",
}


def _validate_fixture_graph(
    display_path: str,
    test_name: str,
    roots: Sequence[str],
    fixtures: dict[str, FixtureDef],
) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(name: str, parent: FixtureDef | None = None) -> None:
        if name in _BUILTIN_FIXTURES:
            if (
                parent is not None
                and _FIXTURE_SCOPE_RANK[_BUILTIN_SCOPE[name]] < _FIXTURE_SCOPE_RANK[parent.scope]
            ):
                raise ValueError(
                    f"{parent.source}: {parent.scope}-scoped fixture {parent.name!r} "
                    f"cannot request {name!r}, which is function-scoped"
                )
            return
        if name in visited:
            return
        if name in visiting:
            start = visiting.index(name)
            cycle = " -> ".join((*visiting[start:], name))
            raise ValueError(f"{display_path}: test {test_name!r} has fixture cycle {cycle}")
        definition = fixtures.get(name)
        if definition is None:
            raise ValueError(
                f"{display_path}: test {test_name!r} requests unknown fixture {name!r}; "
                "declare it with @pytest.fixture or parametrize that argument"
            )
        if (
            parent is not None
            and _FIXTURE_SCOPE_RANK[definition.scope] < _FIXTURE_SCOPE_RANK[parent.scope]
        ):
            raise ValueError(
                f"{parent.source}: {parent.scope}-scoped fixture {parent.name!r} "
                f"cannot request {definition.scope}-scoped fixture {name!r}"
            )
        visiting.append(name)
        for dependency in definition.dependencies:
            visit(dependency, definition)
        visiting.pop()
        visited.add(name)

    for root in roots:
        visit(root)


def _fixture_closure(
    roots: Sequence[str], fixtures: dict[str, FixtureDef]
) -> tuple[FixtureDef, ...]:
    names: set[str] = set()

    def add(name: str) -> None:
        if name in names or name in _BUILTIN_FIXTURES:
            return
        definition = fixtures[name]
        for dependency in definition.dependencies:
            add(dependency)
        names.add(name)

    for root in roots:
        add(root)
    return tuple(definition for name, definition in fixtures.items() if name in names)


def _fixture_parameter_ids(definition: FixtureDef) -> tuple[str, ...]:
    params = definition.params or ()
    declared = definition.param_ids
    if callable(declared):
        return _unique_ids(
            tuple(
                str(declared(value) or _value_id(value, definition.name, index))
                for index, value in enumerate(params)
            )
        )
    if declared is not None:
        ids = tuple(declared)
        if len(ids) != len(params):
            raise ValueError(
                f"{definition.source}: fixture {definition.name!r} ids must match "
                "the number of params"
            )
        return _unique_ids(
            tuple(
                _value_id(value, definition.name, index) if item is None else str(item)
                for index, (item, value) in enumerate(zip(ids, params, strict=True))
            )
        )
    return _unique_ids(
        tuple(_value_id(value, definition.name, index) for index, value in enumerate(params))
    )


def _collect_module(
    path: Path,
    module: ModuleType,
    fixtures: dict[str, FixtureDef],
    runtime: _FixtureRuntime,
) -> list[Case]:
    display_path = _display_path(path)
    for name, value in module.__dict__.items():
        if name == "pytest_plugins":
            _validate_pytest_plugins(display_path, value)
            continue
        if name == "pytestmark":
            continue
        if name.startswith("pytest_") and callable(value):
            raise ValueError(
                f"{display_path}: hook {name!r} is not supported by the native engine; "
                "use a plain module-level test"
            )

    raw_module_marks = module.__dict__.get("pytestmark", ())
    if isinstance(raw_module_marks, Sequence) and not isinstance(raw_module_marks, (str, bytes)):
        module_marks = tuple(_as_mark(item) for item in raw_module_marks)
    elif raw_module_marks:
        module_marks = (_as_mark(raw_module_marks),)
    else:
        module_marks = ()
    collected: list[Case] = []
    for name, value in module.__dict__.items():
        if (
            inspect.isclass(value)
            and name.startswith("Test")
            and value.__module__ == module.__name__
        ):
            collected.extend(
                _collect_class(
                    display_path,
                    name,
                    value,
                    module_marks,
                    fixtures,
                    runtime,
                )
            )
            continue
        if (
            not name.startswith("test_")
            or not inspect.isfunction(value)
            or value.__module__ != module.__name__
        ):
            continue
        collected.extend(
            _expand_function(display_path, name, value, module_marks, fixtures, runtime)
        )
    return collected


def _collect_class(
    display_path: str,
    class_name: str,
    owner: type[Any],
    module_marks: tuple[facade.Mark, ...],
    fixtures: dict[str, FixtureDef],
    runtime: _FixtureRuntime,
) -> list[Case]:
    if owner.__dict__.get("__init__") is not None:
        raise ValueError(
            f"{display_path}: test class {class_name!r} defines __init__; "
            "pytest test classes must use the default constructor"
        )
    raw_class_marks = owner.__dict__.get("pytestmark", ())
    if isinstance(raw_class_marks, Sequence) and not isinstance(raw_class_marks, (str, bytes)):
        class_marks = tuple(_as_mark(item) for item in raw_class_marks)
    elif raw_class_marks:
        class_marks = (_as_mark(raw_class_marks),)
    else:
        class_marks = ()
    class_marks = (
        *class_marks,
        *tuple(getattr(owner, "__wreath_marks__", ())),
    )
    class_parametrize: tuple[facade.Parametrize, ...] = tuple(
        owner.__dict__.get("__wreath_parametrize__", ())
    )
    cases: list[Case] = []
    raw_class_fixtures = tuple(
        (name, value, value.__wreath_fixture__)
        for name, value in owner.__dict__.items()
        if inspect.isfunction(value) and getattr(value, "__wreath_fixture__", None) is not None
    )
    for method_name, method in owner.__dict__.items():
        if not method_name.startswith("test_") or not inspect.isfunction(method):
            continue
        signature_names = _positional_parameters(
            display_path, "test method", f"{class_name}.{method_name}", method
        )
        if not signature_names or signature_names[0] != "self":
            raise ValueError(
                f"{display_path}: test method {class_name}.{method_name} must "
                "declare self as its first parameter"
            )
        method_call = _TestMethod(owner, method_name)
        method_fixtures = fixtures
        if raw_class_fixtures:
            # complexity: allow SL-LINEAR-CALL -- bindings differ per test method
            method_fixtures = dict(fixtures)
        for declared_name, fixture_method, options in raw_class_fixtures:
            fixture_name = options["name"] or declared_name
            if options["scope"] != "function":
                raise ValueError(
                    f"{display_path}: class fixture {fixture_name!r} uses "
                    f"scope={options['scope']!r}; native class fixtures require "
                    "scope='function'"
                )
            dependencies = _positional_parameters(
                display_path, "class fixture", fixture_name, fixture_method
            )
            if not dependencies or dependencies[0] != "self":
                raise ValueError(
                    f"{display_path}: class fixture {fixture_name!r} must declare "
                    "self as its first parameter"
                )
            method_fixtures[fixture_name] = FixtureDef(
                fixture_name,
                _bound_fixture_callable(method_call, fixture_method),
                dependencies[1:],
                bool(options["autouse"]),
                "function",
                options["params"],
                options["ids"],
                display_path,
            )
        cases.extend(
            _expand_function(
                display_path,
                f"{class_name}::{method_name}",
                method_call,
                (*module_marks, *class_marks),
                method_fixtures,
                runtime,
                signature_names=signature_names[1:],
                metadata_source=method,
                inherited_parametrize=class_parametrize,
            )
        )
    return cases


def _expand_function(
    display_path: str,
    name: str,
    function: Callable[..., Any],
    module_marks: tuple[facade.Mark, ...],
    fixtures: dict[str, FixtureDef],
    runtime: _FixtureRuntime,
    *,
    signature_names: tuple[str, ...] | None = None,
    metadata_source: Callable[..., Any] | None = None,
    inherited_parametrize: tuple[facade.Parametrize, ...] = (),
) -> list[Case]:
    if signature_names is None:
        signature_names = _positional_parameters(display_path, "test", name, function)
    if metadata_source is None:
        metadata_source = function
    signature = inspect.signature(metadata_source)
    signature_name_set = set(signature_names)
    default_values = {
        parameter.name: parameter.default
        for parameter in signature.parameters.values()
        if parameter.name in signature_name_set and parameter.default is not inspect.Parameter.empty
    }
    declarations: tuple[facade.Parametrize, ...] = (
        *inherited_parametrize,
        *tuple(getattr(metadata_source, "__wreath_parametrize__", ())),
    )
    declared_names = [item for declaration in declarations for item in declaration.names]
    declared_name_set: set[str] = set()
    duplicate = None
    for declared_name in declared_names:
        if declared_name in declared_name_set:
            duplicate = declared_name
            break
        declared_name_set.add(declared_name)
    if duplicate is not None:
        raise ValueError(f"{display_path}: test {name!r} parametrizes {duplicate!r} more than once")
    unknown = next((item for item in declared_names if item not in signature_name_set), None)
    if unknown is not None:
        raise ValueError(f"{display_path}: test {name!r} parametrizes unknown argument {unknown!r}")
    fixture_names = tuple(
        item
        for item in signature_names
        if item not in declared_name_set and item not in default_values
    )
    autouse = tuple(item.name for item in fixtures.values() if item.autouse)
    _validate_fixture_graph(display_path, name, (*autouse, *fixture_names), fixtures)

    function_marks = (
        *module_marks,
        *tuple(getattr(metadata_source, "__wreath_marks__", ())),
    )
    combinations: list[tuple[dict[str, Any], list[str], tuple[Any, ...]]] = [
        (default_values, [], function_marks)
    ]
    for declaration in declarations:
        expanded: list[tuple[dict[str, Any], list[str], tuple[Any, ...]]] = []
        parameters = tuple(declaration.values)
        case_ids = _unique_ids(
            tuple(
                parameter.id
                if parameter.id is not None
                else _case_id(declaration.names, parameter.values, index)
                for index, parameter in enumerate(parameters)
            )
        )
        for assigned, ids, marks in combinations:
            for parameter, case_id in zip(parameters, case_ids, strict=True):
                if len(parameter.values) != len(declaration.names):
                    raise ValueError(
                        f"{display_path}: test {name!r} parametrized value must contain "
                        f"{len(declaration.names)} values"
                    )
                values = dict(zip(declaration.names, parameter.values, strict=True))
                expanded.append(
                    ({**assigned, **values}, [*ids, case_id], (*marks, *parameter.marks))
                )
        combinations = expanded

    if not declarations:
        combinations = [(default_values, [], function_marks)]
    fixture_combinations: list[tuple[dict[str, tuple[int, Any]], list[str]]] = [({}, [])]
    for definition in _fixture_closure((*autouse, *fixture_names), fixtures):
        if definition.params is None:
            continue
        parameter_ids = _fixture_parameter_ids(definition)
        expanded_fixtures: list[tuple[dict[str, tuple[int, Any]], list[str]]] = []
        for selected, selected_ids in fixture_combinations:
            for index, (value, parameter_id) in enumerate(
                zip(definition.params, parameter_ids, strict=True)
            ):
                expanded_fixtures.append(
                    (
                        {**selected, definition.name: (index, value)},
                        [*selected_ids, parameter_id],
                    )
                )
        fixture_combinations = expanded_fixtures
    reachable_fixtures = _fixture_closure((*autouse, *fixture_names), fixtures)
    async_mode = inspect.iscoroutinefunction(metadata_source) or any(
        inspect.iscoroutinefunction(definition.function)
        or inspect.isasyncgenfunction(definition.function)
        for definition in reachable_fixtures
    )
    cases: list[Case] = []
    expanded_cases = (
        (assigned, [*fixture_ids, *ids], raw_marks, fixture_params)
        for assigned, ids, raw_marks in combinations
        for fixture_params, fixture_ids in fixture_combinations
    )
    for assigned, ids, raw_marks, fixture_params in expanded_cases:
        node_id = f"{display_path}::{name}"
        if ids:
            node_id += f"[{'-'.join(ids)}]"
        marks = tuple(_as_mark(item) for item in raw_marks)
        unsupported_mark = next(
            (
                mark.name
                for mark in marks
                if mark.name in {"filterwarnings", "usefixtures", "xfail"}
            ),
            None,
        )
        if unsupported_mark is not None:
            raise ValueError(
                f"{display_path}: test {name!r} uses pytest.mark.{unsupported_mark}, "
                "which is not supported by the native engine; use --engine pytest"
            )
        skip_exception = _skip_exception(marks)
        cases.append(
            Case(
                node_id,
                _FixtureCall(
                    function,
                    signature_names,
                    assigned,
                    fixtures,
                    autouse,
                    node_id,
                    marks,
                    runtime,
                    display_path,
                    fixture_params,
                    async_mode,
                ),
                (),
                skip_exception,
                frozenset(mark.name for mark in marks),
            )
        )
    return cases


def _as_mark(value: Any) -> facade.Mark:
    if isinstance(value, facade.Mark):
        return value
    configured = getattr(value, "mark", None)
    if isinstance(configured, facade.Mark):
        return configured
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return facade.Mark(name, (), {})
    raise ValueError(f"pytest.param mark {value!r} is not supported by the native engine")


def _skip_exception(marks: Sequence[facade.Mark]) -> facade.Skipped | None:
    for mark in marks:
        if mark.name == "skip":
            reason = str(mark.kwargs.get("reason", mark.args[0] if mark.args else ""))
            return facade.Skipped(reason)
        if mark.name == "skipif":
            if not mark.args:
                raise ValueError("pytest.mark.skipif requires a boolean condition")
            if bool(mark.args[0]):
                reason = str(mark.kwargs.get("reason", "condition is true"))
                return facade.Skipped(reason)
    return None


def _case_id(names: Sequence[str], values: Sequence[Any], index: int) -> str:
    return "-".join(
        _value_id(value, name, index) for name, value in zip(names, values, strict=True)
    )


def _value_id(value: Any, name: str, index: int) -> str:
    if value is None:
        return "None"
    if isinstance(value, bytes):
        return value.decode("ascii", "backslashreplace")
    if isinstance(value, (str, int, float, bool, complex)):
        return str(value)
    if isinstance(value, re.Pattern):
        pattern = value.pattern
        return (
            pattern.decode("ascii", "backslashreplace") if isinstance(pattern, bytes) else pattern
        )
    if isinstance(value, enum.Enum):
        return str(value)
    declared_name = getattr(value, "__name__", None)
    if isinstance(declared_name, str):
        return declared_name
    return f"{name}{index}"


def _unique_ids(values: Sequence[str]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    if all(count == 1 for count in counts.values()):
        return tuple(values)
    resolved = list(values)
    occupied = set(values)
    suffixes: dict[str, int] = {}
    for index, value in enumerate(values):
        if counts[value] == 1:
            continue
        suffix = suffixes.get(value, 0)
        separator = "_" if value and value[-1].isdigit() else ""
        candidate = f"{value}{separator}{suffix}"
        while candidate in occupied:
            suffix += 1
            candidate = f"{value}{separator}{suffix}"
        resolved[index] = candidate
        occupied.add(candidate)
        suffixes[value] = suffix + 1
    return tuple(resolved)


def _display_path(path: Path) -> str:
    return os.path.relpath(path, Path.cwd()).replace(os.sep, "/")


_EXPRESSION_TOKEN = re.compile(r"\s*(\(|\)|\band\b|\bor\b|\bnot\b|[^\s()]+)")


def _compile_matcher(
    expression: str,
    *,
    exact_atoms: bool,
) -> Callable[[Any], bool]:
    """Compile one selection expression for every case in this collection.

    Route and model compilers own application declarations, not CLI boolean
    expressions. This operation-local closure is therefore the smallest owner
    that avoids reparsing the same expression once per collected test.
    """
    tokens = [match.group(1) for match in _EXPRESSION_TOKEN.finditer(expression)]
    position = 0

    def either(left: Callable[[Any], bool], right: Callable[[Any], bool]) -> Callable[[Any], bool]:
        return lambda value: left(value) or right(value)

    def both(left: Callable[[Any], bool], right: Callable[[Any], bool]) -> Callable[[Any], bool]:
        return lambda value: left(value) and right(value)

    def parse_or() -> Callable[[Any], bool]:
        nonlocal position
        matcher = parse_and()
        while position < len(tokens) and tokens[position] == "or":
            position += 1
            left = matcher
            right = parse_and()
            matcher = either(left, right)
        return matcher

    def parse_and() -> Callable[[Any], bool]:
        nonlocal position
        matcher = parse_not()
        while position < len(tokens) and tokens[position] == "and":
            position += 1
            left = matcher
            right = parse_not()
            matcher = both(left, right)
        return matcher

    def parse_not() -> Callable[[Any], bool]:
        nonlocal position
        if position < len(tokens) and tokens[position] == "not":
            position += 1
            operand = parse_not()
            return lambda value: not operand(value)
        return parse_atom()

    def parse_atom() -> Callable[[Any], bool]:
        nonlocal position
        if position >= len(tokens):
            raise ValueError(f"incomplete selection expression {expression!r}")
        token = tokens[position]
        position += 1
        if token == "(":
            matcher = parse_or()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError(f"unclosed selection expression {expression!r}")
            position += 1
            return matcher
        if token in {")", "and", "or"}:
            raise ValueError(f"invalid selection expression {expression!r}")
        if exact_atoms:
            return lambda atoms: token in atoms
        lowered = token.lower()
        return lambda value: lowered in value

    matcher = parse_or()
    if position != len(tokens):
        raise ValueError(f"invalid selection expression {expression!r}")
    if exact_atoms:
        return matcher
    return lambda haystack: matcher(haystack.lower())


def _matches(expression: str | None, haystack: str, *, exact_atoms: bool = False) -> bool:
    if expression is None or not expression.strip():
        return True
    matcher = _compile_matcher(expression, exact_atoms=exact_atoms)
    return matcher(frozenset(haystack.split()) if exact_atoms else haystack)


def _run(
    collection: Collection,
    max_failures: int,
    observer: Callable[[str, str | None], None] | None = None,
) -> tuple[Result, ...]:
    return tuple(Result(*record) for record in _run_raw(collection, max_failures, observer))


def _run_raw(
    collection: Collection,
    max_failures: int,
    observer: Callable[[str, str | None], None] | None = None,
) -> list[_RawResult]:
    try:
        return cast(
            "list[_RawResult]",
            _testrunner.run(
                tuple(case.native() for case in collection.cases),
                facade.Skipped,
                max_failures,
                observer,
            ),
        )
    finally:
        if collection.runtime is not None:
            collection.runtime.close()


def run_selected(
    collection: Collection,
    node_ids: Sequence[str],
    *,
    max_failures: int,
) -> tuple[Result, ...]:
    """Run an exact pytest-shaped node-id subset from an inherited collection."""
    by_id = collection.index
    missing = set(node_ids).difference(by_id)
    if missing:
        missing_node = min(missing)
        raise ValueError(
            f"native mutation selection names uncollected test {missing_node!r}; "
            "collect its test path before forking"
        )
    selected = Collection(
        tuple(by_id[node_id] for node_id in node_ids),
        (),
        runtime=collection.runtime,
    )
    with _facade_import():
        return _run(selected, max_failures)


def _render(results: Sequence[Result], *, quiet: bool, slowest: int) -> None:
    if not quiet:
        for result in results:
            print(f"{result.node_id} {result.outcome.upper()}")
            if result.outcome == "failed" and result.exception is not None:
                rendered = "".join(
                    traceback.TracebackException.from_exception(result.exception).format()
                ).rstrip()
                print(rendered)
    counts = {outcome: 0 for outcome in ("passed", "failed", "skipped", "interrupted")}
    for result in results:
        counts[result.outcome] += 1
    parts = [f"{count} {name}" for name, count in counts.items() if count and name != "interrupted"]
    if counts["interrupted"]:
        parts.append(f"{counts['interrupted']} interrupted")
    print(", ".join(parts) if parts else "no tests ran")
    if slowest > 0 and results and not quiet:
        tail = sorted(results, key=lambda item: item.duration_ns, reverse=True)[:slowest]
        print("slowest native tests")
        for result in tail:
            print(f"  {result.duration_ns / 1_000_000:.3f}ms {result.node_id}")


def _native_shards(
    collection: Collection,
    workers: int,
    history: Path | None,
) -> tuple[tuple[Case, ...], ...]:
    """Balance whole test modules, preserving module fixture ownership."""
    from ._test_runner import _recent_file_weights

    grouped: dict[str, list[Case]] = {}
    for case in collection.cases:
        grouped.setdefault(case.node_id.split("::", 1)[0], []).append(case)
    historical = _recent_file_weights(history) if history is not None else {}
    weighted: list[tuple[float, str, list[Case]]] = []
    for display_path, cases in grouped.items():
        path = (Path.cwd() / display_path).resolve()
        fallback = 0.001 + len(cases) * 0.0001
        weighted.append((max(historical.get(path, 0.0), fallback), display_path, cases))
    count = min(workers, max(1, len(weighted)))
    loads = [0.0] * count
    shards: list[list[Case]] = [[] for _ in range(count)]
    for weight, _path, cases in sorted(weighted, key=lambda item: (-item[0], item[1])):
        owner = min(range(count), key=loads.__getitem__)
        loads[owner] += weight
        shards[owner].extend(cases)
    return tuple(tuple(shard) for shard in shards if shard)


def _child_payload(
    results: Sequence[Result],
) -> tuple[tuple[str, str, int, str | None], ...]:
    return tuple(
        (
            result.node_id,
            result.outcome,
            result.duration_ns,
            (
                None
                if result.exception is None
                else "".join(traceback.TracebackException.from_exception(result.exception).format())
            ),
        )
        for result in results
    )


def _raw_child_payload(
    results: Sequence[_RawResult],
) -> tuple[tuple[str, str, int, str | None], ...]:
    return tuple(
        (
            node_id,
            outcome,
            duration_ns,
            (
                None
                if exception is None
                else "".join(traceback.TracebackException.from_exception(exception).format())
            ),
        )
        for node_id, outcome, duration_ns, exception in results
    )


def _json_worker_payload(
    rows: tuple[tuple[str, str, int, str | None], ...],
    hits: Sequence[tuple[str, Sequence[str]]],
    error: str | None,
) -> bytes:
    compact_hits = tuple((location, tuple(node_ids)) for location, node_ids in hits)
    return json.dumps(
        (rows, compact_hits, error),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _encode_worker_payload(
    results: Sequence[Result],
    hits: Sequence[tuple[str, Sequence[str]]],
    error: str | None = None,
) -> bytes:
    """Encode same-interpreter worker output without JSON object materialisation."""
    return _json_worker_payload(_child_payload(results), hits, error)


def _encode_raw_worker_payload(
    results: Sequence[_RawResult],
    hits: Sequence[tuple[str, Sequence[str]]],
) -> bytes:
    return _json_worker_payload(_raw_child_payload(results), hits, None)


def _decode_worker_payload(
    value: bytes,
) -> tuple[list[Any], list[Any], str | None]:
    payload = json.loads(value)
    if not isinstance(payload, list) or len(payload) != 3:
        raise ValueError("native worker payload must contain results, hits, and error")
    rows, hits, error = payload
    if not isinstance(rows, list) or not isinstance(hits, list):
        raise ValueError("native worker payload results and hits must be sequences")
    if error is not None and not isinstance(error, str):
        raise ValueError("native worker payload error must be text or None")
    return rows, hits, error


class _TraceObserver:
    def __init__(self, tracer: Any, output: Path) -> None:
        self.tracer = tracer
        self.output = output
        self.stream = output.open("a", encoding="utf-8", buffering=1)

    def __call__(self, node_id: str, outcome: str | None) -> None:
        if outcome is None:
            self.tracer.begin(node_id)
        else:
            self.tracer.end()
            if outcome != "passed":
                return
            hits = self.tracer.hits.get(node_id, set())
            if hits:
                self.stream.write(
                    json.dumps(
                        {
                            "nodeid": node_id,
                            "hits": [[path, line] for path, line in hits],
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                self.stream.flush()

    def finish(self) -> None:
        self.stream.write('{"event":"worker_finished"}\n')
        self.stream.flush()
        self.stream.close()


class _WorkerProgressObserver:
    """Stream the one case actually owned by a native worker to its controller."""

    def __init__(
        self,
        descriptor: int,
        trace: _TraceObserver | None,
        *,
        control_descriptor: int | None = None,
        case_count: int = 0,
    ) -> None:
        self.descriptor: int | None = descriptor
        self.trace = trace
        self.control_descriptor: int | None = control_descriptor
        self.remaining_cases = case_count
        self.started_ns = 0

    def __call__(self, node_id: str, outcome: str | None) -> None:
        if self.trace is not None:
            self.trace(node_id, outcome)
        if outcome is None:
            self.started_ns = time.perf_counter_ns()
            duration_ns = 0
        else:
            duration_ns = max(0, time.perf_counter_ns() - self.started_ns)
        encoded = (
            json.dumps([node_id, outcome, duration_ns], separators=(",", ":")).encode() + b"\n"
        )
        descriptor = self.descriptor
        if descriptor is None:
            return
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        if outcome is not None:
            self.remaining_cases -= 1
            control = self.control_descriptor
            if control is not None and self.remaining_cases > 0:
                while True:
                    try:
                        permit = os.read(control, 1)
                    except InterruptedError:
                        continue
                    if not permit:
                        self.control_descriptor = None
                    break

    def finish(self) -> None:
        error: OSError | None = None
        for attribute in ("descriptor", "control_descriptor"):
            descriptor = getattr(self, attribute)
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError as cleanup_error:
                if error is None:
                    error = cleanup_error
            else:
                setattr(self, attribute, None)
        if error is not None:
            raise error


def _reap_owned_worker(pids: Iterable[int]) -> tuple[int, int]:
    """Reap one finished native worker without consuming another subprocess."""
    for pid in pids:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            return finished, status
    return 0, 0


def _terminate_owned_worker(pid: int) -> None:
    if pid <= 0:
        raise ValueError(f"expected a positive worker PID, got {pid}")
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)


def _attempt_cleanup(
    error: BaseException | None, operation: Callable[[], Any]
) -> BaseException | None:
    try:
        operation()
    except BaseException as cleanup_error:
        return error if error is not None else cleanup_error
    return error


@dataclass(frozen=True, slots=True)
class _ChildRun:
    shard: tuple[Case, ...]
    target: Path
    output: Path
    read_progress: int
    write_progress: int
    read_control: int | None
    write_control: int | None


@dataclass(slots=True)
class _ChildRuntime:
    observer: Any = None
    tracer: Any = None
    trace_observer: Any = None
    owns_progress: bool = False

    def finish(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        error: BaseException | None = None
        hits: tuple[tuple[str, tuple[str, ...]], ...] = ()
        tracer = self.tracer
        if tracer is not None:
            try:
                tracer.stop()
                hits = tuple(
                    (f"{path}:{line}", tuple(nodes))
                    for (path, line), nodes in tracer.index().items()
                )
            except BaseException as cleanup_error:
                error = cleanup_error
            else:
                self.tracer = None
        for attribute in ("trace_observer", "observer"):
            resource = getattr(self, attribute)
            if resource is None:
                continue
            try:
                resource.finish()
            except BaseException as cleanup_error:
                if error is None:
                    error = cleanup_error
            else:
                setattr(self, attribute, None)
        if error is not None:
            raise error
        return hits


@dataclass(slots=True)
class _ParallelRun:
    collection: Collection
    shards: tuple[tuple[Case, ...], ...]
    max_failures: int
    renderer: Any
    activity: Any
    trace_spec: Any | None = None
    stage_events: Path | None = None
    mutation_process: Any | None = None
    mutation_mode: str = "auto"
    adaptive_mutation: bool = False
    live_mutation_limit: int = 3
    fuzz_namespace: Any | None = None
    fuzz_directory: Path | None = None
    controller_pid: int = field(init=False)
    temporary: Any = field(init=False)
    directory: Path = field(init=False)
    append_stage_event: Any = field(init=False, default=None)
    children: dict[int, tuple[Path, Path, tuple[Case, ...], int, int | None]] = field(
        init=False, default_factory=dict
    )
    progress_buffers: dict[int, bytes] = field(init=False, default_factory=dict)
    progress_owners: dict[int, int] = field(init=False, default_factory=dict)
    completed_worker_cases: dict[int, int] = field(init=False, default_factory=dict)
    running_children: set[int] = field(init=False, default_factory=set)
    waiting_children: deque[int] = field(init=False, default_factory=deque)
    waiting_children_by_pid: dict[int, bool] = field(init=False, default_factory=dict)
    finished_stage_files: set[str] = field(init=False, default_factory=set)
    finished_progress_files: set[str] = field(init=False, default_factory=set)
    progress_stream: Any = field(init=False, default=None)
    live_fuzz: Any = field(init=False, default=None)
    live_fuzz_started: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.controller_pid = os.getpid()
        if self.stage_events is not None:
            from ._test_runner import _append_stage_event

            self.append_stage_event = _append_stage_event
        self.temporary = tempfile.TemporaryDirectory(prefix="wreath-native-workers-")
        self.directory = Path(self.temporary.name)
        if self.trace_spec is not None:
            self.trace_spec.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.trace_spec.output_dir / f"live-progress-{os.getpid()}.jsonl"
            self.progress_stream = path.open("w", encoding="utf-8", buffering=1)

    def sync_mutation_activity(self) -> None:
        if self.mutation_process is None:
            return
        from ._test_runner import (
            _consume_mutation_events,
            _live_fuzz_ready,
            _start_fuzz_process,
            _sync_fuzz_process,
        )

        state = self.mutation_process.event_state
        processed = state.processed
        _consume_mutation_events(self.mutation_process.activity_path, state)
        if state.processed != processed:
            self.renderer.mutation_progress(
                self.mutation_mode,
                state.total,
                mutating_files=frozenset(state.mutating_files),
                verified_files=frozenset(state.verified_files),
                failed_mutation_files=frozenset(),
                test_workers=state.test_workers,
                mutant_workers=state.mutant_workers,
            )
        passed_files = frozenset(
            path
            for path, file_state in self.activity.files.items()
            if file_state.outcome == "passed"
        )
        gold = tuple(sorted(state.verified_files.intersection(passed_files)))
        passed = len(passed_files)
        if (
            not self.live_fuzz_started
            and self.fuzz_namespace is not None
            and self.fuzz_directory is not None
            and _live_fuzz_ready(len(gold), passed)
        ):
            gold_lookup = dict.fromkeys(gold, True)
            self.live_fuzz = _start_fuzz_process(
                self.fuzz_namespace,
                gold,
                directory=self.fuzz_directory,
                workers="1",
                case_ids=tuple(
                    sorted(
                        nodeid
                        for nodeid in state.killer_tests
                        if gold_lookup.get(nodeid.split("::", 1)[0], False)
                    )
                ),
            )
            self.live_fuzz_started = True
        if self.live_fuzz is not None and self.renderer.mutation is not None:
            _sync_fuzz_process(
                self.live_fuzz,
                self.renderer.mutation,
                renderer=self.renderer,
            )

    def mutation_slots(self) -> int:
        if (
            not self.adaptive_mutation
            or self.mutation_process is None
            or self.mutation_process.event_state.total <= 0
        ):
            return 0
        from ._mutant.runner import _progressive_live_jobs

        return _progressive_live_jobs(
            len(self.shards),
            len(self.finished_progress_files),
            len(self.activity.files),
            max_live=self.live_mutation_limit,
        )

    def rebalance_test_workers(self) -> None:
        fuzz_slots = (
            1 if self.live_fuzz is not None and self.live_fuzz.process.poll() is None else 0
        )
        target = max(1, len(self.shards) - self.mutation_slots() - fuzz_slots)
        while self.waiting_children_by_pid and len(self.running_children) < target:
            pid = self.waiting_children.popleft()
            if not self.waiting_children_by_pid.pop(pid, False):
                continue
            child = self.children.get(pid)
            if child is None or child[4] is None:
                continue
            try:
                os.write(child[4], b"1")
            except BrokenPipeError:
                continue
            self.running_children.add(pid)

    def record_progress(self, row: Any, descriptor: int) -> None:
        if not isinstance(row, list) or len(row) != 3:
            return
        node_id, outcome, duration_ns = row
        if not isinstance(node_id, str) or not isinstance(duration_ns, int):
            return
        if outcome is None:
            self._record_test_started(node_id)
        elif isinstance(outcome, str):
            self._record_test_finished(node_id, outcome, duration_ns, descriptor)

    def _record_test_started(self, node_id: str) -> None:
        self.activity.start_test(node_id)
        test = self.activity.tests[node_id]
        if self.stage_events is not None and self.append_stage_event is not None:
            self.append_stage_event(
                self.stage_events,
                self.activity.files[test.path],
                outcome="running",
            )

    def _record_test_finished(
        self, node_id: str, outcome: str, duration_ns: int, descriptor: int
    ) -> None:
        report_outcome = "failed" if outcome == "interrupted" else outcome
        self.activity.add_native_result(node_id, report_outcome, duration_ns / 1_000_000_000)
        file_state = self.activity.files[self.activity.tests[node_id].path]
        pid = self.progress_owners[descriptor]
        self.completed_worker_cases[pid] += 1
        shard = self.children[pid][2]
        if self.completed_worker_cases[pid] >= len(shard):
            self.running_children.discard(pid)
        elif self.adaptive_mutation:
            self.running_children.discard(pid)
            self.waiting_children.append(pid)
            self.waiting_children_by_pid[pid] = True
        self._record_file_progress(file_state)
        self.sync_mutation_activity()
        self.rebalance_test_workers()

    def _record_file_progress(self, file_state: Any) -> None:
        if self.stage_events is not None and self.append_stage_event is not None:
            if file_state.path not in self.finished_stage_files and file_state.finished == len(
                file_state.nodeids
            ):
                self.append_stage_event(self.stage_events, file_state)
                self.finished_stage_files.add(file_state.path)
            else:
                self.append_stage_event(self.stage_events, file_state, outcome="idle")
        if (
            self.progress_stream is not None
            and file_state.path not in self.finished_progress_files
            and file_state.finished == len(file_state.nodeids)
        ):
            self.progress_stream.write(
                json.dumps(
                    {"event": "block_finished", "path": file_state.path},
                    separators=(",", ":"),
                )
                + "\n"
            )
            self.finished_progress_files.add(file_state.path)

    def consume_progress_chunk(self, descriptor: int, pending: bytes) -> tuple[bytes, bool]:
        try:
            chunk = os.read(descriptor, 65536)
        except BlockingIOError:
            return pending, False
        if not chunk:
            return pending, False
        rows = (pending + chunk).split(b"\n")
        for raw in rows[:-1]:
            try:
                self.record_progress(json.loads(raw), descriptor)
            except UnicodeDecodeError, ValueError:
                continue
        return rows[-1], True

    def consume_progress(self, descriptor: int, *, until_eof: bool = False) -> None:
        pending, consumed = self.consume_progress_chunk(
            descriptor, self.progress_buffers[descriptor]
        )
        while until_eof and consumed:
            pending, consumed = self.consume_progress_chunk(descriptor, pending)
        self.progress_buffers[descriptor] = pending

    def start_workers(self) -> None:
        self.activity.collect(tuple(case.node_id for case in self.collection.cases))
        if self.progress_stream is not None:
            self.progress_stream.write(
                json.dumps(
                    {"event": "suite_started", "total_blocks": len(self.activity.files)},
                    separators=(",", ":"),
                )
                + "\n"
            )
        for ordinal, shard in enumerate(self.shards):
            self._start_worker(ordinal, shard)

    def _start_worker(self, ordinal: int, shard: tuple[Case, ...]) -> None:
        target = self.directory / f"worker-{ordinal}.json"
        output = self.directory / f"worker-{ordinal}.log"
        read_progress, write_progress = os.pipe()
        if self.adaptive_mutation:
            read_control, write_control = os.pipe()
        else:
            read_control = None
            write_control = None
        pid = os.fork()
        if pid == 0:  # pragma: no cover - worker process
            self._run_child(
                _ChildRun(
                    shard,
                    target,
                    output,
                    read_progress,
                    write_progress,
                    read_control,
                    write_control,
                )
            )
        if os.getpid() != self.controller_pid:
            os.kill(os.getpid(), 9)
        self.children[pid] = (target, output, shard, read_progress, write_control)
        parent_descriptors = (
            (write_progress,) if read_control is None else (write_progress, read_control)
        )
        attempted_parent_descriptors = 0
        try:
            for descriptor in parent_descriptors:
                attempted_parent_descriptors += 1
                os.close(descriptor)
            os.set_blocking(read_progress, False)
        except BaseException as error:
            self.children.pop(pid, None)
            cleanup_error = _attempt_cleanup(error, lambda: _terminate_owned_worker(pid))
            cleanup_error = _attempt_cleanup(cleanup_error, lambda: os.close(read_progress))
            if write_control is not None:
                cleanup_error = _attempt_cleanup(cleanup_error, lambda: os.close(write_control))
            for descriptor in parent_descriptors[attempted_parent_descriptors:]:
                cleanup_error = _attempt_cleanup(
                    cleanup_error, lambda descriptor=descriptor: os.close(descriptor)
                )
            if cleanup_error is not None:
                raise cleanup_error from None
            raise
        self.progress_buffers[read_progress] = b""
        self.progress_owners[read_progress] = pid
        self.completed_worker_cases[pid] = 0
        self.running_children.add(pid)

    def _run_child(self, child: _ChildRun) -> None:
        if os.getpid() == self.controller_pid:
            os.kill(os.getpid(), 9)
        os.close(child.read_progress)
        if child.write_control is not None:
            os.close(child.write_control)
        runtime = _ChildRuntime()
        exit_code = 1
        try:
            results, hits = self._execute_child_cases(child, runtime)
            child.target.write_bytes(_encode_raw_worker_payload(results, hits))
            exit_code = 0
        except Exception:
            child.target.write_bytes(_encode_worker_payload((), (), traceback.format_exc()))
        finally:
            try:
                runtime.finish()
            finally:
                try:
                    if not runtime.owns_progress:
                        os.close(child.write_progress)
                finally:
                    os._exit(exit_code)

    def _execute_child_cases(
        self, child: _ChildRun, runtime: _ChildRuntime
    ) -> tuple[Sequence[_RawResult], tuple[tuple[str, tuple[str, ...]], ...]]:
        descriptor = os.open(child.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
        os.close(descriptor)
        worker_collection = Collection(child.shard, (), runtime=self.collection.runtime)
        if self.trace_spec is not None:
            from ._mutant.trace import LineTracer

            runtime.tracer = LineTracer(self.trace_spec.watched)
            runtime.trace_observer = _TraceObserver(
                runtime.tracer,
                self.trace_spec.output_dir / f"live-{os.getpid()}.jsonl",
            )
            runtime.tracer.start()
        runtime.observer = _WorkerProgressObserver(
            child.write_progress,
            runtime.trace_observer,
            control_descriptor=child.read_control,
            case_count=len(child.shard),
        )
        runtime.owns_progress = True
        try:
            results = _run_raw(worker_collection, self.max_failures, runtime.observer)
        finally:
            hits = runtime.finish()
        return results, hits

    def monitor_workers(self) -> tuple[Result, ...]:
        combined: list[Result] = []
        while self.children:
            ready, _, _ = select.select(self.progress_buffers, (), (), 0.05)
            for descriptor in ready:
                self.consume_progress(descriptor)
            self.sync_mutation_activity()
            self.rebalance_test_workers()
            pid, status = _reap_owned_worker(self.children)
            if pid:
                combined.extend(self._finish_worker(pid, status))
        return tuple(combined)

    def _finish_worker(self, pid: int, status: int) -> tuple[Result, ...]:
        child = self.children.get(pid)
        if child is None:
            return ()
        target, output, shard, progress_descriptor, control_descriptor = child
        self._release_worker(pid, progress_descriptor, control_descriptor)
        rows, hits, error = self._read_worker_payload(pid, status, target, output)
        shard_results = self._shard_results(shard, rows, error)
        if error is None and self.trace_spec is not None:
            self.trace_spec.output_dir.mkdir(parents=True, exist_ok=True)
            trace_target = self.trace_spec.output_dir / f"trace-{pid}.json"
            trace_target.write_text(json.dumps({"hits": hits}), encoding="utf-8")
        self._record_shard_results(shard, shard_results, replace_streamed_results=error is not None)
        return shard_results

    def _release_worker(
        self, pid: int, progress_descriptor: int, control_descriptor: int | None
    ) -> None:
        os.set_blocking(progress_descriptor, True)
        self.consume_progress(progress_descriptor, until_eof=True)
        self.children.pop(pid, None)
        os.close(progress_descriptor)
        self.progress_buffers.pop(progress_descriptor, None)
        self.progress_owners.pop(progress_descriptor, None)
        self.running_children.discard(pid)
        self.waiting_children_by_pid.pop(pid, None)
        if control_descriptor is not None:
            os.close(control_descriptor)
        self.rebalance_test_workers()

    @staticmethod
    def _read_worker_payload(
        pid: int, status: int, target: Path, output: Path
    ) -> tuple[Sequence[Any], Sequence[Any], str | None]:
        if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0 or not target.exists():
            log = output.read_text(encoding="utf-8", errors="replace") if output.exists() else ""
            return (), (), log or f"native worker {pid} exited with status {status}"
        try:
            return _decode_worker_payload(target.read_bytes())
        except (EOFError, TypeError, ValueError) as error:
            return (), (), f"native worker produced an invalid result: {error}"

    @staticmethod
    def _shard_results(
        shard: tuple[Case, ...], rows: Sequence[Any], error: str | None
    ) -> tuple[Result, ...]:
        if error is not None:
            return tuple(Result(case.node_id, "failed", 0, RuntimeError(error)) for case in shard)
        results = [
            Result(
                str(row[0]),
                str(row[1]),
                int(row[2]),
                RuntimeError(str(row[3])) if row[3] is not None else None,
            )
            for row in rows
        ]
        finished = {result.node_id for result in results}
        results.extend(
            Result(
                case.node_id,
                "interrupted",
                0,
                RuntimeError("not run after the native failure limit"),
            )
            for case in shard
            if case.node_id not in finished
        )
        return tuple(results)

    def _record_shard_results(
        self,
        shard: tuple[Case, ...],
        shard_results: tuple[Result, ...],
        *,
        replace_streamed_results: bool = False,
    ) -> None:
        for result in shard_results:
            report_outcome = "failed" if result.outcome == "interrupted" else result.outcome
            duration = result.duration_ns / 1_000_000_000
            test = self.activity.tests[result.node_id]
            if test.finished:
                if replace_streamed_results:
                    self.activity.reconcile_native_result(result.node_id, report_outcome, duration)
                else:
                    self.activity.reconcile_native_duration(result.node_id, duration)
            else:
                self.activity.add_native_result(result.node_id, report_outcome, duration)
        if self.stage_events is None or self.append_stage_event is None:
            return
        shard_paths = {case.node_id.split("::", 1)[0] for case in shard}
        for path in sorted(shard_paths):
            file_state = self.activity.files[path]
            if (
                replace_streamed_results or path not in self.finished_stage_files
            ) and file_state.finished == len(file_state.nodeids):
                self.append_stage_event(self.stage_events, file_state)
                self.finished_stage_files.add(path)

    def cleanup(self) -> None:
        error: BaseException | None = None
        if self.progress_stream is not None:
            error = _attempt_cleanup(error, self.progress_stream.close)
        for pid, (_, _, _, descriptor, control) in tuple(self.children.items()):
            error = _attempt_cleanup(error, lambda pid=pid: _terminate_owned_worker(pid))
            error = _attempt_cleanup(error, lambda descriptor=descriptor: os.close(descriptor))
            if control is not None:
                error = _attempt_cleanup(error, lambda control=control: os.close(control))
        error = _attempt_cleanup(error, self.temporary.cleanup)
        if error is not None:
            raise error


def _run_parallel(run: _ParallelRun) -> tuple[tuple[Result, ...], Any | None]:
    """Run module-local C dispatch loops in isolated fork workers."""
    try:
        run.start_workers()
        return run.monitor_workers(), run.live_fuzz
    finally:
        run.cleanup()


@dataclass(slots=True)
class _NativeRunState:
    temporary: Any
    directory: Path
    trace_spec: Any = None
    selection_path: Path | None = None
    baseline_wait_path: Path | None = None
    prepared_mutation: Any = None
    live_fuzz: Any = None
    live_fuzz_started: bool = False
    fuzz_case_ids: tuple[str, ...] = ()
    renderer: Any = None

    def cleanup(self) -> None:
        from ._test_runner import _stop_fuzz_process, _stop_mutation_process

        error: BaseException | None = None
        for attribute, finish in (
            ("renderer", lambda resource: resource.restore()),
            ("prepared_mutation", _stop_mutation_process),
            ("live_fuzz", _stop_fuzz_process),
        ):
            resource = getattr(self, attribute)
            if resource is None:
                continue
            try:
                finish(resource)
            except BaseException as cleanup_error:
                if error is None:
                    error = cleanup_error
            else:
                setattr(self, attribute, None)
        try:
            self.temporary.cleanup()
        except BaseException as cleanup_error:
            if error is None:
                error = cleanup_error
        if error is not None:
            raise error

    @classmethod
    def prepare(cls, namespace: Any, options: Options) -> _NativeRunState:
        temporary = tempfile.TemporaryDirectory(prefix="wreath-native-run-")
        state = cls(temporary, Path(temporary.name))
        if namespace.mutant not in {"auto", "sample"}:
            return state
        from ._test_runner import (
            _mutation_selection_payload,
            _prepare_mutation_trace,
            _start_mutation_process,
        )

        state.trace_spec = _prepare_mutation_trace(namespace, state.directory)
        if namespace.mutant == "sample" and state.trace_spec is None:
            temporary.cleanup()
            raise ValueError("--mutant sample found no eligible controls")
        if state.trace_spec is None:
            return state
        state.selection_path = state.directory / "mutation-selection.json"
        state.selection_path.write_text(
            json.dumps(_mutation_selection_payload(state.trace_spec)),
            encoding="utf-8",
        )
        if not options.collect_only:
            state.baseline_wait_path = state.directory / "mutation-baseline.json"
            state.prepared_mutation = _start_mutation_process(
                namespace,
                directory=state.directory,
                baseline_wait=state.baseline_wait_path,
                baseline_stream=state.trace_spec.output_dir,
                selection=state.selection_path,
            )
        return state


def _select_native_cases(
    namespace: Any, collection: Collection
) -> tuple[Collection, tuple[str, ...]]:
    case_selection = getattr(namespace, "case_selection", None)
    if case_selection is None:
        return collection, ()
    path = Path(str(case_selection))
    try:
        selected_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(
            f"--case-selection must name a readable JSON test list: {error}"
        ) from error
    if not isinstance(selected_value, list) or not all(
        isinstance(nodeid, str) for nodeid in selected_value
    ):
        raise ValueError("--case-selection must contain a JSON list of test node IDs")
    selected_ids = dict.fromkeys((str(node_id) for node_id in selected_value), True)
    missing = set(selected_ids).difference(collection.index)
    resolved_ids = set(selected_ids).intersection(collection.index)
    fresh_by_family: dict[str, list[str]] = {}
    for case in collection.cases:
        path, separator, suffix = case.node_id.rpartition("::")
        name, parameter_separator, _parameter = suffix.partition("[")
        if separator and parameter_separator:
            fresh_by_family.setdefault(f"{path}::{name}", []).append(case.node_id)
    unresolved: set[str] = set()
    for node_id in missing:
        path, separator, suffix = node_id.rpartition("::")
        name, parameter_separator, _parameter = suffix.partition("[")
        fresh = (
            fresh_by_family.get(f"{path}::{name}") if separator and parameter_separator else None
        )
        if fresh is None:
            unresolved.add(node_id)
        else:
            resolved_ids.update(fresh)
    if unresolved:
        raise ValueError(
            "--case-selection names uncollected test "
            f"{min(unresolved)!r}; select its test file and use its exact node ID"
        )
    fuzz_ids = tuple(sorted(case.node_id for case in collection.cases if case.has_mark("fuzz")))
    selected_cases = tuple(
        case for case in collection.cases if case.node_id in resolved_ids or case.has_mark("fuzz")
    )
    schedule_seed = os.environ.get("WREATH_FUZZ_SCHEDULE_SEED")
    if schedule_seed is not None:
        selected_cases = _schedule_fuzz_cases(selected_cases, schedule_seed)
    return (
        Collection(
            selected_cases,
            collection.modules,
            collection.files,
            collection.runtime,
            {case.node_id: case for case in selected_cases},
        ),
        fuzz_ids,
    )


@dataclass(frozen=True, slots=True)
class _NativeSuite:
    collection: Collection
    results: tuple[Result, ...]
    activity: Any
    history: Path | None


@dataclass(slots=True)
class _NativeOutcome:
    namespace: Any
    options: Options
    state: _NativeRunState
    suite: _NativeSuite
    status: int
    activity_path: Path
    user_report: Path | None

    @classmethod
    def prepare(
        cls,
        namespace: Any,
        options: Options,
        state: _NativeRunState,
        suite: _NativeSuite,
    ) -> _NativeOutcome:
        from ._test_runner import _atomic_json, _update_history

        if any(result.outcome == "interrupted" for result in suite.results):
            status = 2
        elif any(result.outcome == "failed" for result in suite.results):
            status = 1
        else:
            status = 0 if suite.collection.cases else 5
        suite.activity.finish(status)
        report = suite.activity.report(slowest=int(namespace.slowest))
        if getattr(namespace, "case_selection", None) is not None:
            report["fuzz_case_ids"] = list(state.fuzz_case_ids)
            schedule_seed = os.environ.get("WREATH_FUZZ_SCHEDULE_SEED")
            if schedule_seed is not None:
                report["fuzz_schedule_seed"] = schedule_seed
        activity_path = state.directory / "activity.json"
        _atomic_json(activity_path, report)
        user_report = Path(namespace.report) if namespace.report is not None else None
        if user_report is not None:
            _atomic_json(user_report, report)
        if suite.history is not None:
            _update_history(suite.history, report)
        if len(suite.collection.cases) <= 200:
            _render(suite.results, quiet=options.quiet, slowest=int(namespace.slowest))
        if status:
            cls._render_failures(suite.results)
        return cls(namespace, options, state, suite, status, activity_path, user_report)

    @staticmethod
    def _render_failures(results: Sequence[Result]) -> None:
        for result in results:
            if result.outcome not in {"failed", "interrupted"}:
                continue
            print(f"\n{result.node_id} {result.outcome.upper()}")
            if result.exception is not None:
                print(result.exception)

    def finish(self) -> int:
        from ._test_runner import MutationActivity, _no_gold_fuzz, _run_explicit_fuzz_replay

        renderer = self.state.renderer
        if renderer is None:
            raise RuntimeError("native activity renderer was not prepared")
        fuzz_mode = getattr(self.namespace, "fuzz", "off")
        if self.namespace.mutant == "off":
            if fuzz_mode == "on":
                fuzz, fuzz_activity, fuzz_status = _run_explicit_fuzz_replay(self.namespace)
                self._attach_fuzz(fuzz)
                renderer.finish_pipeline(None, fuzz_activity)
                return self.status if self.status != 0 else fuzz_status
            renderer.finish()
            return self.status
        if not self.suite.activity.counts()["passed"]:
            mutation_activity = MutationActivity(mode=str(self.namespace.mutant), state="no_green")
            if fuzz_mode == "on":
                fuzz, fuzz_activity = _no_gold_fuzz()
                self._attach_fuzz(fuzz)
                renderer.finish_pipeline(mutation_activity, fuzz_activity)
            else:
                renderer.finish_with_mutation(mutation_activity)
            return self.status
        if self.namespace.mutant == "auto" and self.state.trace_spec is None:
            mutation_activity = MutationActivity(mode="auto", state="unrated")
            if fuzz_mode == "on":
                fuzz, fuzz_activity = _no_gold_fuzz()
                self._attach_fuzz(fuzz)
                renderer.finish_pipeline(mutation_activity, fuzz_activity)
            else:
                renderer.finish_with_mutation(mutation_activity)
            return self.status
        mutation, mutation_status, mutation_activity = self._finish_mutation(renderer)
        if fuzz_mode == "off":
            renderer.finish_with_mutation(mutation_activity)
            return self.status if self.status != 0 else mutation_status
        fuzz_status = self._finish_fuzz(mutation, mutation_activity, renderer)
        if self.status != 0:
            return self.status
        return max(mutation_status, fuzz_status)

    def _attach_fuzz(self, fuzz: dict[str, Any]) -> None:
        if self.user_report is None:
            return
        from ._test_runner import _attach_fuzz_report

        _attach_fuzz_report(self.user_report, fuzz)

    def _finish_mutation(self, renderer: Any) -> tuple[dict[str, Any], int, Any]:
        from ._test_runner import (
            _attach_mutation_report,
            _finish_mutation_process,
            _mutation_activity_from_report,
            _mutation_confidence,
            _stop_mutation_process,
            _write_reused_baseline,
        )

        baseline_path = None
        trace = self.state.trace_spec
        prepared = self.state.prepared_mutation
        if trace is not None:
            baseline_wait = self.state.baseline_wait_path
            if baseline_wait is None:
                raise RuntimeError("native mutation baseline path was not prepared")
            if _write_reused_baseline(trace, self.activity_path, baseline_wait):
                baseline_path = baseline_wait
            elif prepared is not None:
                _stop_mutation_process(prepared)
                self.state.prepared_mutation = None
                prepared = None
        if prepared is not None:
            mutation, status = _finish_mutation_process(
                self.namespace,
                prepared,
                renderer=renderer,
                live_fuzz=self.state.live_fuzz,
            )
            self.state.prepared_mutation = None
        else:
            mutation, status = _mutation_confidence(
                self.namespace,
                baseline=baseline_path,
                selection=self.state.selection_path,
                renderer=renderer,
            )
        activity = _mutation_activity_from_report(str(self.namespace.mutant), mutation)
        if self.user_report is not None:
            _attach_mutation_report(self.user_report, mutation)
        return mutation, status, activity

    def _finish_fuzz(self, mutation: dict[str, Any], mutation_activity: Any, renderer: Any) -> int:
        from ._test_runner import (
            _attach_fuzz_campaign,
            _finish_fuzz_process,
            _fuzz_confidence,
            _merge_fuzz_batches,
            _mutation_gold_files,
            _no_gold_fuzz,
            _run_fuzz_campaigns,
        )

        final_gold = _mutation_gold_files(mutation)
        live_fuzz = self.state.live_fuzz
        early_selected = frozenset() if live_fuzz is None else frozenset(live_fuzz.selected)
        early_batch = None
        if live_fuzz is not None:
            early_batch = _finish_fuzz_process(live_fuzz, mutation_activity, renderer=renderer)
            self.state.live_fuzz = None
        final_gold_set = frozenset(final_gold)
        batches = []
        if early_batch is not None and early_selected <= final_gold_set:
            batches.append(early_batch)
            remaining = tuple(sorted(final_gold_set.difference(early_selected)))
        else:
            remaining = final_gold
        if remaining:
            batches.append(
                _fuzz_confidence(
                    self.namespace,
                    mutation,
                    mutation_activity,
                    renderer=renderer,
                    selected=remaining,
                    campaigns=False,
                )
            )
        if batches:
            fuzz, fuzz_activity, status = _merge_fuzz_batches(batches, final_gold)
        else:
            fuzz, fuzz_activity = _no_gold_fuzz()
            status = 0
        campaign, campaign_status = _run_fuzz_campaigns(self.namespace, mutation)
        fuzz_activity = _attach_fuzz_campaign(fuzz, fuzz_activity, campaign)
        status = max(status, campaign_status)
        fuzz["live_started"] = self.state.live_fuzz_started
        self._attach_fuzz(fuzz)
        renderer.finish_pipeline(mutation_activity, fuzz_activity)
        return status


def _run_native_suite(
    namespace: Any, options: Options, workers: int, state: _NativeRunState
) -> _NativeSuite | int:
    from ._test_runner import (
        ActivityRenderer,
        FuzzActivity,
        RunActivity,
        _resolve_mutant_workers,
        _stop_fuzz_process,
    )

    with _facade_import():
        collection = collect(options)
        try:
            if options.collect_only:
                for case in collection.cases:
                    print(case.node_id)
                return 0 if collection.cases else 5
            collection, state.fuzz_case_ids = _select_native_cases(namespace, collection)
            history = None if namespace.no_history else Path(namespace.history)
            shards = _native_shards(collection, workers, history)
            if options.max_failures and len(shards) > 1:
                shards = (tuple(collection.cases),)
            activity = RunActivity(workers=len(shards))
            state.renderer = ActivityRenderer(
                activity,
                stream=sys.stderr,
                mode=str(namespace.grid),
                slowest=int(namespace.slowest),
            )
            with _test_import_paths(collection.files):
                results, state.live_fuzz = _run_parallel(
                    _ParallelRun(
                        collection,
                        shards=shards,
                        max_failures=options.max_failures,
                        renderer=state.renderer,
                        activity=activity,
                        trace_spec=state.trace_spec,
                        mutation_process=state.prepared_mutation,
                        mutation_mode=str(namespace.mutant),
                        adaptive_mutation=(
                            state.prepared_mutation is not None
                            and str(namespace.mutant_workers) == "auto"
                        ),
                        live_mutation_limit=max(
                            1,
                            _resolve_mutant_workers("auto")
                            - (1 if getattr(namespace, "fuzz", "off") == "on" else 0),
                        ),
                        fuzz_namespace=(
                            namespace if getattr(namespace, "fuzz", "off") == "on" else None
                        ),
                        fuzz_directory=state.directory / "live-fuzz",
                        stage_events=(
                            Path(namespace.stage_events)
                            if getattr(namespace, "stage_events", None) is not None
                            else None
                        ),
                    )
                )
            state.live_fuzz_started = state.live_fuzz is not None
            if state.live_fuzz is not None and state.live_fuzz.process.poll() is None:
                _stop_fuzz_process(state.live_fuzz)
                state.renderer.fuzz = FuzzActivity(
                    state="running",
                    selected_files=frozenset(state.live_fuzz.selected),
                )
                state.live_fuzz = None
            return _NativeSuite(collection, results, activity, history)
        finally:
            if collection.runtime is not None:
                collection.runtime.close()
            _forget_modules(collection.modules)


def _execute_native(namespace: Any) -> int:
    """Collect once and execute module shards through native C worker loops."""
    _validate_runner_options(namespace)
    from ._test_runner import _mutation_arguments, _resolve_workers

    if int(namespace.slowest) < 0:
        raise ValueError("--slowest must be a non-negative integer")
    if namespace.mutant != "off":
        _mutation_arguments(namespace)
    workers = _resolve_workers(str(namespace.workers))
    options = _parse(tuple(getattr(namespace, "pytest_args", ())))
    if options.markers is None:
        options = replace(options, markers=_configured_markers())
    state = _NativeRunState.prepare(namespace, options)
    try:
        suite = _run_native_suite(namespace, options, workers, state)
        if isinstance(suite, int):
            return suite
        return _NativeOutcome.prepare(namespace, options, state, suite).finish()
    finally:
        state.cleanup()


def execute(namespace: Any) -> int:
    """Collect once and execute module shards through native C worker loops."""
    return _execute_native(namespace)


class _PytestOracle:
    def __init__(self) -> None:
        self.node_ids: list[str] = []
        self.outcomes: dict[str, str] = {}

    def pytest_collection_finish(self, session: Any) -> None:
        self.node_ids = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when == "setup" and report.outcome != "passed":
            self.outcomes[report.nodeid] = report.outcome
        elif report.when == "call":
            self.outcomes[report.nodeid] = report.outcome
        elif report.when == "teardown" and report.outcome == "failed":
            self.outcomes[report.nodeid] = "failed"


def _pytest_collect(arguments: Sequence[str]) -> tuple[int, tuple[str, ...]]:
    pytest = _load_pytest()
    oracle = _PytestOracle()
    status = int(pytest.main([*arguments, "--collect-only", "-q"], plugins=[oracle]))
    return status, tuple(oracle.node_ids)


def _pytest_execute(arguments: Sequence[str]) -> tuple[int, tuple[tuple[str, str], ...]]:
    pytest = _load_pytest()
    oracle = _PytestOracle()
    status = int(pytest.main(list(arguments), plugins=[oracle]))
    outcomes = tuple((node_id, oracle.outcomes[node_id]) for node_id in oracle.node_ids)
    return status, outcomes


def _load_pytest() -> Any:
    try:
        import pytest
    except ModuleNotFoundError as error:
        if error.name != "pytest":
            raise
        raise ValueError(
            "--engine dual needs pytest; install Wreath's dev group or pytest>=8.4"
        ) from error
    return pytest


def execute_dual(namespace: Any) -> int:
    """Execute a hermetic corpus twice and compare pytest with native outcomes."""
    _validate_runner_options(namespace)
    arguments = tuple(str(item) for item in getattr(namespace, "pytest_args", ()))
    options = _parse(arguments)
    collection = collect(options)
    native_ids = tuple(case.node_id for case in collection.cases)
    pytest_collection_status, pytest_ids = _pytest_collect(arguments)
    if pytest_collection_status not in {0, 5}:
        _forget_modules(collection.modules)
        raise ValueError(
            f"pytest collection failed with status {pytest_collection_status}; "
            "dual execution stopped"
        )
    files = collection.files
    path_index = _node_path_index(files)
    native_identities = tuple(_node_identity(item, path_index) for item in native_ids)
    pytest_identities = tuple(_node_identity(item, path_index) for item in pytest_ids)
    if native_identities != pytest_identities:
        _forget_modules(collection.modules)
        native_id_set = set(native_ids)
        pytest_id_set = set(pytest_ids)
        native_only = next((item for item in native_ids if item not in pytest_id_set), None)
        pytest_only = next((item for item in pytest_ids if item not in native_id_set), None)
        raise ValueError(
            "native/pytest collection differs; "
            f"native-only={native_only!r}, pytest-only={pytest_only!r}"
        )
    if options.collect_only:
        _forget_modules(collection.modules)
        for node_id in native_ids:
            print(node_id)
        return 0 if native_ids else 5
    with _facade_import():
        try:
            native_results = _run(collection, options.max_failures)
        finally:
            _forget_modules(collection.modules)
    pytest_status, pytest_outcomes = _pytest_execute(arguments)
    native_outcomes = tuple((item.node_id, item.outcome) for item in native_results)
    native_outcome_identities = tuple(
        (_node_identity(node_id, path_index), outcome) for node_id, outcome in native_outcomes
    )
    pytest_outcome_identities = tuple(
        (_node_identity(node_id, path_index), outcome) for node_id, outcome in pytest_outcomes
    )
    if native_outcome_identities != pytest_outcome_identities:
        raise ValueError(
            f"native/pytest outcomes differ; native={native_outcomes!r}, pytest={pytest_outcomes!r}"
        )
    _render(native_results, quiet=options.quiet, slowest=int(namespace.slowest))
    return pytest_status


def _node_path_index(files: Sequence[Path]) -> dict[str, Path]:
    candidates: dict[str, Path | None] = {}
    for path in files:
        parts = path.as_posix().split("/")
        spellings = {path.as_posix(), _display_path(path)}
        spellings.update("/".join(parts[index:]) for index in range(1, len(parts)))
        for spelling in spellings:
            normalized = spelling.lstrip("./")
            previous = candidates.get(normalized, path)
            candidates[normalized] = path if previous == path else None
    return {spelling: path for spelling, path in candidates.items() if path is not None}


def _node_identity(node_id: str, path_index: dict[str, Path]) -> str:
    """Anchor an engine-relative node path to its selected absolute file."""
    path_text, separator, remainder = node_id.partition("::")
    direct = (Path.cwd() / path_text).resolve()
    normalized = path_text.replace("\\", "/").lstrip("./")
    resolved = path_index.get(normalized, direct)
    return f"{resolved.as_posix()}{separator}{remainder}"


def _forget_modules(modules: Sequence[str]) -> None:
    for module in modules:
        sys.modules.pop(module, None)


__all__ = [
    "Case",
    "Collection",
    "Options",
    "Result",
    "collect",
    "execute",
    "execute_dual",
    "run_selected",
]
