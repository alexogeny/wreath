"""Deterministic environment configuration with constrained dotenv semantics.

The dotenv dialect is deliberately narrow. A line is `KEY=value` and the value
is the literal rest of the line: quotes stay as characters, `$VAR` is not
expanded, there is no command substitution, and `#` starts nothing. A comment
line, an `export KEY=value` line, and any line without an `=` are *rejected*
rather than skipped -- a config file that quietly means something other than what
it reads is worse than one that refuses to parse. Blank lines are ignored, a
repeated key takes its last value, and a value must be valid UTF-8. Every refusal
is a `ValueError` naming the line number.

The process environment wins over the file, so a deployment overrides a
checked-in dotenv without editing it; `override=True` inverts that. Nothing
mutates `os.environ` unless you pass `apply=True`.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import os
import types
import typing
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

from ._native import _core

#: `parse_dotenv(data)` -- the strict dialect to a mapping. No interpolation,
#: no `export`, no comments; a malformed line raises `ValueError` naming it.
parse_dotenv: Callable[[bytes], dict[str, str]] = _core.parse_dotenv

#: `read_osenv()` -- the process environment as one mapping, read once.
read_osenv: Callable[[], dict[str, str]] = _core.read_osenv


_MISSING = dataclasses.MISSING
_NONE_TYPE = type(None)


@dataclasses.dataclass(frozen=True, slots=True)
class Secret[SecretT]:
    """A configuration value that must be revealed explicitly.

    `repr` and `str` are always redacted, including inside a dataclass
    representation or traceback. `reveal()` is the only way to recover the
    wrapped value; this makes a secret reaching a log an explicit action rather
    than an accidental formatting side effect.
    """

    _value: SecretT

    def reveal(self) -> SecretT:
        """Return the wrapped value."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"


@dataclasses.dataclass(frozen=True, slots=True)
class Env:
    """Override the environment key used for one settings field.

    Write this inside `Annotated`. An explicit name is absolute and is not
    prefixed, which is useful when several settings models share one credential.
    """

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Env name must not be empty")


class SettingsError(ValueError):
    """All missing or invalid values found while binding one settings model."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} settings error(s)")


def _unwrap_env(annotation: Any) -> tuple[Any, Env | None]:
    if typing.get_origin(annotation) is not typing.Annotated:
        return annotation, None
    base, *metadata = typing.get_args(annotation)
    marker = next((item for item in metadata if isinstance(item, Env)), None)
    return base, marker


def _settings_error(key: str, message: str, kind: str) -> dict[str, str]:
    return {"key": key, "msg": message, "type": kind}


def _convert_setting(annotation: Any, raw: str, key: str) -> Any:
    annotation, _marker = _unwrap_env(annotation)
    origin = typing.get_origin(annotation)
    if origin is Secret:
        value_type = typing.get_args(annotation)[0]
        return Secret(_convert_setting(value_type, raw, key))
    if annotation is str or annotation is Any:
        return raw
    if annotation is bool:
        lowered = raw.casefold()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError("value is not a boolean")
    if annotation is int:
        try:
            return int(raw)
        except ValueError:
            raise ValueError("value is not an integer") from None
    if annotation is float:
        try:
            return float(raw)
        except ValueError:
            raise ValueError("value is not a number") from None
    if annotation is Decimal:
        try:
            return Decimal(raw)
        except InvalidOperation:
            raise ValueError("value is not a decimal") from None
    if annotation is UUID:
        try:
            return UUID(raw)
        except ValueError:
            raise ValueError("value is not a UUID") from None
    if annotation is Path:
        return Path(raw)
    if annotation is datetime.datetime:
        try:
            return datetime.datetime.fromisoformat(raw)
        except ValueError:
            raise ValueError("value is not an ISO-8601 datetime") from None
    if annotation is datetime.date:
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            raise ValueError("value is not an ISO-8601 date") from None
    if annotation is datetime.time:
        try:
            return datetime.time.fromisoformat(raw)
        except ValueError:
            raise ValueError("value is not an ISO-8601 time") from None
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        try:
            return annotation(raw)
        except ValueError:
            raise ValueError("value is not an allowed enum member") from None
    if origin is typing.Literal:
        for choice in typing.get_args(annotation):
            choice_type = type(choice)
            try:
                converted = _convert_setting(choice_type, raw, key)
            except ValueError:
                continue
            if converted == choice:
                return choice
        raise ValueError("value is not one of the allowed literals")
    if origin in (types.UnionType, typing.Union):
        options = typing.get_args(annotation)
        if raw == "" and _NONE_TYPE in options:
            return None
        for option in options:
            if option is _NONE_TYPE:
                continue
            try:
                return _convert_setting(option, raw, key)
            except ValueError:
                continue
        raise ValueError("value matches no union member")
    if origin in (list, tuple, set, frozenset):
        item_type = typing.get_args(annotation)[0]
        values = [_convert_setting(item_type, item.strip(), key) for item in raw.split(",")]
        if origin is tuple:
            return tuple(values)
        if origin is set:
            return set(values)
        if origin is frozenset:
            return frozenset(values)
        return values
    raise TypeError(f"unsupported settings annotation {annotation!r}")


def find_dotenv(
    path: str | os.PathLike[str], *, start: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve an explicit dotenv path, optionally searching parent directories.

    An absolute `path` is used as given. A relative one is joined against
    `start` -- the current working directory when omitted -- and then against
    each parent in turn, up to the filesystem root, so a test or a subpackage
    finds the project's file without knowing how deep it sits.

    Args:
        start: directory the upward search begins in, resolved before walking

    Returns:
        the absolute path of the first file that exists

    Raises:
        FileNotFoundError: no file at an absolute path, or the search reached the root
    """
    candidate = Path(path)
    if candidate.is_absolute():
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        return candidate
    current = Path.cwd() if start is None else Path(start)
    current = current.resolve()
    while True:
        resolved = current / candidate
        if resolved.is_file():
            return resolved
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(candidate)
        current = parent


def load_env(
    path: str | os.PathLike[str],
    *,
    search: bool = True,
    start: str | os.PathLike[str] | None = None,
    override: bool = False,
    apply: bool = False,
) -> dict[str, str]:
    """Load one explicitly named dotenv file and merge it with process state.

    The parser accepts only `KEY=value` records with literal values, and rejects
    a comment, an `export` prefix, or a line without an `=` outright; the
    module docstring gives the whole dialect.

    The result is the *whole* process environment merged with the file, not just
    the file's keys, so it can be handed to something that expects an environment.
    Existing process values win unless `override=True`.

    `apply=True` also writes the file's values into `os.environ` -- with
    `setdefault`, or unconditionally when `override=True` -- for code that
    reads `os.environ` directly instead of taking the returned mapping. It is
    the only argument here that mutates anything.

    Args:
        search: walk parent directories for a relative `path` before giving up
        start: directory the search begins in, defaulting to the current one
        override: let the file's values replace process values instead of losing
        apply: also write the file's values into `os.environ`

    Returns:
        every key of the process environment and the file, merged

    Raises:
        FileNotFoundError: the file is absent, or the search reached the root
        ValueError: a line is not a valid record, naming the line number
    """
    dotenv_path = find_dotenv(path, start=start) if search else Path(path)
    file_values = parse_dotenv(dotenv_path.read_bytes())
    process_values = read_osenv()
    merged = process_values | file_values if override else file_values | process_values
    if apply:
        if override:
            os.environ.update(file_values)
        else:
            for key, value in file_values.items():
                os.environ.setdefault(key, value)
    return merged


class Environment(Mapping[str, str]):
    """An immutable snapshot of merged process and dotenv state.

    Read-only, and a copy: the constructor duplicates `values`, so later mutation
    of the source mapping -- including `os.environ` -- is invisible here, and the
    `Mapping` surface offers no setter. A snapshot taken at startup therefore
    still describes startup.

    Every stored value remains the exact `str` the environment or file supplied.
    `bind()` performs startup-only coercion into a dataclass and reports every
    missing or malformed value together, without mutating the snapshot.

    `repr` prints the key names and no values, deliberately: an environment
    snapshot holds credentials, and the usual way a credential reaches a log is a
    traceback frame that repr'd the object holding it.

    Args:
        values: the pairs to snapshot, copied rather than referenced
    """

    __slots__ = ("_sources", "_values")

    def __init__(
        self,
        values: Mapping[str, str],
        *,
        sources: Mapping[str, str] | None = None,
    ) -> None:
        self._values = dict(values)
        self._sources = {
            key: (sources[key] if sources is not None and key in sources else "provided")
            for key in self._values
        }

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        search: bool = True,
        start: str | os.PathLike[str] | None = None,
        override: bool = False,
        apply: bool = False,
    ) -> Environment:
        """Snapshot a dotenv file merged over the process environment.

        The arguments and the precedence are `load_env`'s: process values win
        unless `override=True`, and `os.environ` is untouched unless
        `apply=True`. The snapshot holds the merged result, not the file alone.

        Args:
            search: walk parent directories for a relative `path` before giving up
            start: directory the search begins in, defaulting to the current one
            override: let the file's values replace process values instead of losing
            apply: also write the file's values into `os.environ`

        Raises:
            FileNotFoundError: the file is absent, or the search reached the root
            ValueError: a line is not a valid record, naming the line number
        """
        dotenv_path = find_dotenv(path, start=start) if search else Path(path)
        file_values = parse_dotenv(dotenv_path.read_bytes())
        process_values = read_osenv()
        values = process_values | file_values if override else file_values | process_values
        file_source = str(dotenv_path)
        sources = {key: file_source for key in file_values}
        if override:
            for key in process_values:
                sources.setdefault(key, "process")
        else:
            sources.update({key: "process" for key in process_values})
        if apply:
            if override:
                os.environ.update(file_values)
            else:
                for key, value in file_values.items():
                    os.environ.setdefault(key, value)
        return cls(values, sources=sources)

    def source(self, key: str) -> str:
        """Return the winning source label for `key`.

        Directly constructed snapshots use `"provided"`; process values use
        `"process"` and dotenv values use the resolved file path.
        """
        if key not in self._values:
            raise KeyError(key)
        return self._sources[key]

    def bind[SettingsT](
        self, settings: type[SettingsT], *, prefix: str = ""
    ) -> SettingsT:
        """Construct a typed dataclass from this environment snapshot.

        Field names become uppercase keys. At the root, `prefix="APP"` maps
        `debug` to `APP_DEBUG`; nested dataclasses use a double underscore,
        so `database.host` maps to `APP_DATABASE__HOST`. An
        `Annotated[T, Env("EXACT_NAME")]` marker supplies an absolute alias.
        Defaults and default factories are used when their key is absent.

        Conversion supports strings, booleans, numbers, Decimal, UUID, Path,
        ISO date/time values, enums, Literal, optional/union values, comma-
        separated containers, and `Secret[T]`. Every problem is collected
        before `SettingsError` is raised.
        """
        if not dataclasses.is_dataclass(settings) or not isinstance(settings, type):
            raise TypeError("settings must be a dataclass type")
        errors: list[dict[str, str]] = []
        value = self._bind_dataclass(settings, prefix.rstrip("_"), False, errors)
        if errors:
            raise SettingsError(errors)
        return value

    def _bind_dataclass[SettingsT](
        self,
        settings: type[SettingsT],
        prefix: str,
        nested: bool,
        errors: list[dict[str, str]],
    ) -> SettingsT:
        try:
            hints = typing.get_type_hints(settings, include_extras=True)
        except NameError as error:
            raise TypeError(
                f"settings model {settings.__qualname__} has an unresolvable annotation: "
                f"{error}"
            ) from error
        kwargs: dict[str, Any] = {}
        for field in dataclasses.fields(settings):
            annotation = hints.get(field.name, Any)
            base_annotation, marker = _unwrap_env(annotation)
            separator = "__" if nested else "_"
            conventional = (
                f"{prefix}{separator}{field.name.upper()}" if prefix else field.name.upper()
            )
            key = marker.name if marker is not None else conventional
            if dataclasses.is_dataclass(base_annotation) and isinstance(base_annotation, type):
                before = len(errors)
                nested_value = self._bind_dataclass(base_annotation, key, True, errors)
                if len(errors) == before:
                    kwargs[field.name] = nested_value
                continue
            if key not in self._values:
                if field.default is not _MISSING:
                    kwargs[field.name] = field.default
                elif field.default_factory is not _MISSING:
                    kwargs[field.name] = field.default_factory()
                else:
                    errors.append(_settings_error(key, "value is required", "missing"))
                continue
            try:
                kwargs[field.name] = _convert_setting(base_annotation, self._values[key], key)
            except (TypeError, ValueError) as error:
                errors.append(_settings_error(key, str(error), "invalid"))
        if errors:
            # Never instantiate a partial model: __post_init__ may have effects
            # and later fields may still be invalid.
            return typing.cast(SettingsT, None)
        return settings(**kwargs)

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Environment(keys={tuple(self._values)})"


__all__ = [
    "Env",
    "Environment",
    "Secret",
    "SettingsError",
    "find_dotenv",
    "load_env",
    "parse_dotenv",
    "read_osenv",
]
