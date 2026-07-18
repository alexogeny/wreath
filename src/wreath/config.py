"""Deterministic environment configuration with constrained dotenv semantics."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ._native import _core

if _core is None or not hasattr(_core, "parse_dotenv"):
    from ._pure.env import parse_dotenv, read_osenv
else:
    parse_dotenv = _core.parse_dotenv
    read_osenv = _core.read_osenv


def find_dotenv(
    path: str | os.PathLike[str], *, start: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve an explicit dotenv path, optionally searching parent directories."""
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

    The parser accepts only ``KEY=value`` records. Values are literal: quoting,
    variable expansion, command substitution, and ``export`` have no special
    meaning. Existing process values win unless ``override=True``.
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
    """Immutable-by-convention snapshot returned from process and dotenv state."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

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
        return cls(
            load_env(
                path,
                search=search,
                start=start,
                override=override,
                apply=apply,
            )
        )

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Environment(keys={tuple(self._values)})"


__all__ = ["Environment", "find_dotenv", "load_env", "parse_dotenv", "read_osenv"]
