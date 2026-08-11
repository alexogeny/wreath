"""Walking a source tree: which directories are worth reading, how a file is
parsed, and why one was skipped."""

from __future__ import annotations

import ast
import os
from pathlib import Path

# What a real tree throws at a reader, and the stable code each is reported under.
# Order matters: UnicodeDecodeError is a ValueError, so it must be tested first.
_SKIP_REASONS: tuple[tuple[type[BaseException], str], ...] = (
    (RecursionError, "too-deep"),          # nesting past the parser's stack budget
    (MemoryError, "out-of-memory"),        # a generated or pathological module
    (SyntaxError, "syntax-error"),         # py2, a template, a partial checkout
    (UnicodeDecodeError, "undecodable"),   # not UTF-8 (latin-1 source, or binary)
    (OSError, "unreadable"),               # broken symlink, permissions, deleted mid-walk
    (ValueError, "invalid-source"),        # e.g. embedded NUL bytes
)
# Everything above, as one except-clause. Deliberately *not* BaseException:
# KeyboardInterrupt and SystemExit must end the run.
_SKIPPABLE = tuple({cls for cls, _ in _SKIP_REASONS})


def _skip_reason(exc: BaseException) -> str:
    for cls, reason in _SKIP_REASONS:
        if isinstance(exc, cls):
            return reason
    return "error"  # pragma: no cover - unreachable while _SKIPPABLE mirrors the table


def _skip_detail(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _parse_file(path: Path) -> ast.Module:
    """Read and parse one module. Raises; callers decide whether that is fatal."""
    return ast.parse(path.read_text(encoding="utf-8"))


def _relative_to(path: Path, root: Path) -> str:
    """How a path is spelled in the report: relative to the root when it is under it."""
    return str(path.relative_to(root)) if root == path or root in path.parents else str(path)


# Directory names that are never the application being ported. Everything whose
# name begins with "." is pruned as well — the convention ruff, black and pytest
# already use — which is what removes `.git`, `.tox`, `.nox`, `.venv`, `.eggs`,
# `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.direnv` and `.idea` without
# enumerating them. So this list only carries the undotted names.
_PRUNED_DIRS = frozenset({
    "__pycache__",     # compiled bytecode; never source
    "node_modules",    # a JS dependency tree, frequently vendored beside a Python app
    "site-packages",   # installed third-party code, venv marker present or not
    "venv",            # the undotted spelling of the convention below
    "build",           # a *copy* of the source tree; counting it double-counts
    "dist",            # unpacked sdists/wheels, same problem
})
_PRUNED_SUFFIXES = (".egg-info",)


def _is_pruned_dir(dirpath: str, name: str) -> bool:
    """Is `dirpath/name` infrastructure rather than application source?

    A virtualenv is detected by its **marker**, `pyvenv.cfg`, not by its name:
    `.venv` is a convention and nothing more, and a venv walked as app code both
    inflates the coverage denominator with libraries the user is not porting and
    drags a few thousand unrelated files into a run they did not ask for.
    """
    if name.startswith(".") or name in _PRUNED_DIRS or name.endswith(_PRUNED_SUFFIXES):
        return True
    try:
        return (Path(dirpath) / name / "pyvenv.cfg").is_file()
    except OSError:  # pragma: no cover - unreadable directory; the walk reports it
        return False


def _iter_py(root: Path, on_error=None):
    """Yield every application `.py` under `root`, pruning infrastructure.

    `on_error` receives the `OSError` for any directory that could not be
    listed (`os.walk` swallows those by default, which would silently shrink
    the tree). Symlinked directories are not followed — `os.walk`'s default —
    so a link out of the tree cannot widen the walk beyond what was named.
    """
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = sorted(d for d in dirnames if not _is_pruned_dir(dirpath, d))
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name
