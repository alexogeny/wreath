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

    Every value is the `str` the environment or the file supplied. There is no
    typed accessor and no coercion; a missing key raises `KeyError` like any
    mapping, and parsing an integer or a boolean is the caller's job.

    `repr` prints the key names and no values, deliberately: an environment
    snapshot holds credentials, and the usual way a credential reaches a log is a
    traceback frame that repr'd the object holding it.

    Args:
        values: the pairs to snapshot, copied rather than referenced
    """

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
