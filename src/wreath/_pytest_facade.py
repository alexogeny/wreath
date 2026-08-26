"""The import-compatible pytest surface used by Wreath's native test engine.

This module deliberately does not collect or schedule tests. Decorators attach
inert metadata which :mod:`wreath._native_test_runner` validates and compiles
into calls for the native core.
"""

from __future__ import annotations

import builtins
import importlib
import math
import os
import re
import sys
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, cast


class Skipped(BaseException):
    """The native engine's explicit non-failure outcome."""


class Failed(AssertionError):
    """An explicit failure raised by :func:`fail`."""


def skip(reason: str = "") -> None:
    """Stop the current native test with a skipped outcome."""
    raise Skipped(reason)


def fail(reason: str = "", pytrace: bool = True) -> None:
    """Fail the current native test.

    ``pytrace`` is accepted for source compatibility; native rendering owns
    traceback presentation and therefore does not use it.
    """
    del pytrace
    raise Failed(reason)


class ExceptionInfo[ExceptionT: BaseException]:
    """The captured exception exposed by ``pytest.raises(...).value``."""

    def __init__(self) -> None:
        self._value: ExceptionT | None = None

    @property
    def value(self) -> ExceptionT:
        if self._value is None:
            raise AssertionError("exception value is unavailable before the context exits")
        return self._value

    @property
    def type(self) -> builtins.type[ExceptionT]:
        return type(self.value)

    @property
    def traceback(self) -> TracebackType | None:
        return self.value.__traceback__


class _RaisesContext[ExceptionT: BaseException](
    AbstractContextManager[ExceptionInfo[ExceptionT]]
):
    def __init__(
        self,
        expected: type[ExceptionT] | tuple[type[ExceptionT], ...],
        match: str | re.Pattern[str] | None,
        check: Callable[[ExceptionT], bool] | None,
    ) -> None:
        self.expected = expected
        self.match = match
        self.check = check
        self.info: ExceptionInfo[ExceptionT] = ExceptionInfo()

    def __enter__(self) -> ExceptionInfo[ExceptionT]:
        return self.info

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del traceback
        expected_name = _exception_name(self.expected)
        if exception_type is None or exception is None:
            raise AssertionError(f"DID NOT RAISE {expected_name}")
        if not isinstance(exception, self.expected):
            return False
        typed_exception = exception
        if self.match is not None and re.search(self.match, str(typed_exception)) is None:
            raise AssertionError(
                f"Regex pattern {self.match!r} does not match {str(typed_exception)!r}"
            )
        if self.check is not None and not self.check(typed_exception):
            raise AssertionError("check did not return True")
        self.info._value = typed_exception
        return True


def _exception_name(
    expected: type[BaseException] | tuple[type[BaseException], ...],
) -> str:
    if isinstance(expected, tuple):
        return " or ".join(item.__name__ for item in expected)
    return expected.__name__


def raises[ExceptionT: BaseException](
    expected_exception: type[ExceptionT] | tuple[type[ExceptionT], ...],
    *,
    match: str | re.Pattern[str] | None = None,
    check: Callable[[ExceptionT], bool] | None = None,
) -> _RaisesContext[ExceptionT]:
    """Assert that a block raises the requested exception."""
    return _RaisesContext(expected_exception, match, check)


class _WarnsContext(AbstractContextManager[list[warnings.WarningMessage]]):
    def __init__(
        self,
        expected: type[Warning] | tuple[type[Warning], ...],
        match: str | re.Pattern[str] | None,
    ) -> None:
        self.expected = expected
        self.match = match
        self._manager = warnings.catch_warnings(record=True)
        self.captured: list[warnings.WarningMessage] = []

    def __enter__(self) -> list[warnings.WarningMessage]:
        self.captured = self._manager.__enter__()
        warnings.simplefilter("always")
        return self.captured

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        result = self._manager.__exit__(exception_type, exception, traceback)
        if exception_type is not None:
            return bool(result)
        matching = [
            item
            for item in self.captured
            if issubclass(item.category, self.expected)
            and (self.match is None or re.search(self.match, str(item.message)) is not None)
        ]
        if not matching:
            expected_name = _exception_name(self.expected)
            raise AssertionError(f"DID NOT WARN {expected_name}")
        return bool(result)


def warns(
    expected_warning: type[Warning] | tuple[type[Warning], ...] = Warning,
    *,
    match: str | re.Pattern[str] | None = None,
) -> _WarnsContext:
    """Assert that a block emits the requested warning."""
    return _WarnsContext(expected_warning, match)


@dataclass(frozen=True, slots=True)
class Parameter:
    values: tuple[Any, ...]
    id: str | None = None
    marks: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class Parametrize:
    names: tuple[str, ...]
    values: tuple[Parameter, ...]


@dataclass(frozen=True, slots=True)
class Mark:
    name: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]


def param(*values: Any, id: str | None = None, marks: Any = ()) -> Parameter:
    """Declare one parametrized case."""
    if marks == ():
        normalized_marks: tuple[Any, ...] = ()
    elif isinstance(marks, Sequence) and not isinstance(marks, (str, bytes)):
        normalized_marks = tuple(marks)
    else:
        normalized_marks = (marks,)
    return Parameter(tuple(values), id=id, marks=normalized_marks)


def _append_metadata(function: Callable[..., Any], name: str, value: Any) -> None:
    current = tuple(getattr(function, name, ()))
    setattr(function, name, (*current, value))


class _MarkDecorator:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) == 1 and callable(args[0]) and not kwargs:
            function = args[0]
            _append_metadata(function, "__wreath_marks__", Mark(self.name, (), {}))
            return function

        return _ConfiguredMark(Mark(self.name, tuple(args), dict(kwargs)))


class _ConfiguredMark:
    def __init__(self, configured: Mark) -> None:
        self.mark = configured
        self.name = configured.name

    def __call__(self, function: Callable[..., Any]) -> Callable[..., Any]:
        _append_metadata(function, "__wreath_marks__", self.mark)
        return function


class _MarkNamespace:
    def __getattr__(self, name: str) -> _MarkDecorator:
        return _MarkDecorator(name)

    def parametrize(
        self,
        argnames: str | Iterable[str],
        argvalues: Iterable[Any],
        *,
        indirect: bool | Sequence[str] = False,
        ids: Iterable[str | None] | Callable[[Any], str | None] | None = None,
        scope: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if indirect is not False:
            raise ValueError("native pytest.mark.parametrize requires indirect=False")
        if scope is not None:
            raise ValueError("native pytest.mark.parametrize does not support scope")
        names = _parameter_names(argnames)
        raw_values = tuple(argvalues)
        if callable(ids):
            id_factory = cast("Callable[[Any], str | None]", ids)
            explicit_ids = None
        else:
            id_factory = None
            explicit_ids = (
                tuple(cast("Iterable[str | None]", ids)) if ids is not None else None
            )
        if explicit_ids is not None and len(explicit_ids) != len(raw_values):
            raise ValueError("pytest.mark.parametrize ids must match the number of values")
        normalized: list[Parameter] = []
        for index, raw in enumerate(raw_values):
            item = raw if isinstance(raw, Parameter) else _parameter(raw, len(names))
            item_id = item.id
            if explicit_ids is not None and explicit_ids[index] is not None:
                item_id = explicit_ids[index]
            elif id_factory is not None:
                generated = id_factory(raw)
                if generated is not None:
                    item_id = generated
            normalized.append(Parameter(item.values, item_id, item.marks))
        declaration = Parametrize(names, tuple(normalized))

        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            _append_metadata(function, "__wreath_parametrize__", declaration)
            return function

        return decorate


def _parameter_names(argnames: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(argnames, str):
        names = tuple(part.strip() for part in argnames.split(","))
    else:
        names = tuple(str(part).strip() for part in argnames)
    if not names or any(not name for name in names):
        raise ValueError("pytest.mark.parametrize needs non-empty parameter names")
    if len(set(names)) != len(names):
        raise ValueError("pytest.mark.parametrize parameter names must be unique")
    return names


def _parameter(raw: Any, width: int) -> Parameter:
    if width == 1:
        return Parameter((raw,))
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"parametrized value must contain {width} values")
    values = tuple(raw)
    if len(values) != width:
        raise ValueError(f"parametrized value must contain {width} values")
    return Parameter(values)


mark = _MarkNamespace()


def hookimpl(function: Callable[..., Any] | None = None, **options: Any) -> Any:
    """Accept pluggy hook declarations imported by compatibility-test dependencies."""
    del options

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        return target

    return decorate(function) if function is not None else decorate


def fixture(
    function: Callable[..., Any] | None = None,
    *,
    scope: str = "function",
    params: Iterable[Any] | None = None,
    autouse: bool = False,
    ids: Iterable[str | None] | Callable[[Any], str | None] | None = None,
    name: str | None = None,
) -> Any:
    """Record a fixture declaration so native collection can refuse it early."""
    options = {
        "scope": scope,
        "params": None if params is None else tuple(params),
        "autouse": autouse,
        "ids": ids,
        "name": name,
    }

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        target.__dict__["__wreath_fixture__"] = options
        target.__dict__["_fixture_function_marker"] = options
        return target

    return decorate(function) if function is not None else decorate


class Approx:
    def __init__(
        self,
        expected: Any,
        rel: float | None,
        abs: float | None,
        nan_ok: bool,
    ) -> None:
        self.expected = expected
        self.rel = 1e-6 if rel is None else rel
        self.abs = 1e-12 if abs is None else abs
        self.nan_ok = nan_ok

    def __eq__(self, actual: Any) -> bool:
        return _approximately_equal(actual, self.expected, self.rel, self.abs, self.nan_ok)


def _approximately_equal(
    actual: Any, expected: Any, relative: float, absolute: float, nan_ok: bool
) -> bool:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        return expected.keys() == actual.keys() and all(
            _approximately_equal(actual[key], value, relative, absolute, nan_ok)
            for key, value in expected.items()
        )
    if (
        isinstance(expected, Sequence)
        and isinstance(actual, Sequence)
        and not isinstance(expected, (str, bytes))
        and not isinstance(actual, (str, bytes))
    ):
        return len(actual) == len(expected) and all(
            _approximately_equal(left, right, relative, absolute, nan_ok)
            for left, right in zip(actual, expected, strict=True)
        )
    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError):
        return actual == expected
    if nan_ok and math.isnan(left) and math.isnan(right):
        return True
    return math.isclose(left, right, rel_tol=relative, abs_tol=absolute)


def approx(
    expected: Any,
    rel: float | None = None,
    abs: float | None = None,
    nan_ok: bool = False,
) -> Approx:
    """Return an approximate comparison object for scalar and nested values."""
    return Approx(expected, rel, abs, nan_ok)


def importorskip(
    modname: str,
    minversion: str | None = None,
    reason: str | None = None,
    *,
    exc_type: type[ImportError] | None = None,
) -> Any:
    """Import a module or skip the current native test module."""
    caught = ImportError if exc_type is None else exc_type
    try:
        module = importlib.import_module(modname)
    except caught:
        skip(reason or f"could not import {modname}")
    if minversion is not None:
        found = getattr(module, "__version__", None)
        if found is None or _version_tuple(str(found)) < _version_tuple(minversion):
            skip(reason or f"module {modname} requires version {minversion}")
    return module


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


class MonkeyPatch:
    """Small compatible mutation helper with deterministic undo."""

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []

    _missing = object()

    def setattr(
        self,
        target: Any,
        name: Any,
        value: Any = _missing,
        raising: bool = True,
    ) -> None:
        if isinstance(target, str):
            if value is not self._missing:
                raise TypeError("string monkeypatch.setattr accepts target and value")
            parts = target.split(".")
            if len(parts) < 2:
                raise TypeError("string monkeypatch target must be 'module.attribute'")
            imported = None
            remainder: list[str] = []
            for width in range(len(parts) - 1, 0, -1):
                try:
                    imported = importlib.import_module(".".join(parts[:width]))
                except ModuleNotFoundError as error:
                    if error.name != ".".join(parts[:width]):
                        raise
                else:
                    remainder = parts[width:]
                    break
            if imported is None or not remainder:
                raise ModuleNotFoundError(target)
            target = imported
            for component in remainder[:-1]:
                target = getattr(target, component)
            value = name
            name = remainder[-1]
        elif value is self._missing:
            raise TypeError("monkeypatch.setattr needs target, name, and value")
        if raising and not hasattr(target, name):
            raise AttributeError(name)
        existed = hasattr(target, name)
        previous = getattr(target, name, None)
        builtins.setattr(target, name, value)
        self._undo.append(
            lambda: builtins.setattr(target, name, previous)
            if existed
            else builtins.delattr(target, name)
        )

    def setitem(self, mapping: Any, name: Any, value: Any) -> None:
        existed = name in mapping
        previous = mapping.get(name)
        mapping[name] = value

        def restore() -> None:
            if existed:
                mapping[name] = previous
            else:
                mapping.pop(name, None)

        self._undo.append(restore)

    def delitem(self, mapping: Any, name: Any, raising: bool = True) -> None:
        if name not in mapping:
            if raising:
                raise KeyError(name)
            return
        previous = mapping.pop(name)
        self._undo.append(lambda: mapping.__setitem__(name, previous))

    def syspath_prepend(self, path: Any) -> None:
        value = os.fspath(path)
        sys.path.insert(0, value)
        importlib.invalidate_caches()

        def restore() -> None:
            sys.path.remove(value)
            importlib.invalidate_caches()

        self._undo.append(restore)

    def chdir(self, path: Any) -> None:
        previous = os.getcwd()
        os.chdir(path)
        self._undo.append(lambda: os.chdir(previous))

    def setenv(self, name: str, value: str, prepend: str | None = None) -> None:
        existed = name in os.environ
        previous = os.environ.get(name)
        os.environ[name] = f"{value}{prepend}{previous}" if prepend and previous else value

        def restore() -> None:
            if existed and previous is not None:
                os.environ[name] = previous
            else:
                os.environ.pop(name, None)

        self._undo.append(restore)

    def delenv(self, name: str, raising: bool = True) -> None:
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        previous = os.environ.pop(name)
        self._undo.append(lambda: os.environ.__setitem__(name, previous))

    def undo(self) -> None:
        while self._undo:
            self._undo.pop()()

    def __enter__(self) -> MonkeyPatch:
        return self

    def __exit__(self, *unused: Any) -> None:
        del unused
        self.undo()


class CaptureFixture:  # pragma: no cover - annotation-only compatibility
    pass


class LogCaptureFixture:  # pragma: no cover - annotation-only compatibility
    pass


class TempPathFactory:  # pragma: no cover - annotation-only compatibility
    pass


class FixtureRequest:  # pragma: no cover - annotation-only compatibility
    pass


class Pytester:  # pragma: no cover - annotation-only compatibility
    pass


__all__ = [
    "Approx",
    "CaptureFixture",
    "ExceptionInfo",
    "Failed",
    "FixtureRequest",
    "LogCaptureFixture",
    "Mark",
    "MonkeyPatch",
    "Parameter",
    "Parametrize",
    "Pytester",
    "Skipped",
    "TempPathFactory",
    "approx",
    "fail",
    "fixture",
    "hookimpl",
    "importorskip",
    "mark",
    "param",
    "raises",
    "skip",
    "warns",
]
