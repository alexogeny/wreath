"""The ``wreath typegen`` command: build the model and emit consumer files.

Generation renders every file in memory first, then writes through temporary
files replaced atomically, so a failure never leaves a half-written tree. Only
paths recorded in ``wreath-typegen.json`` are ever removed, and only inside the
selected output directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .inspect import build_api_model
from .model import TypegenError
from .targets.typescript import MANIFEST_NAME, render_typescript


@dataclass(frozen=True, slots=True)
class TypegenOptions:
    target: str
    output: str
    react_query: bool = False
    base_url_env: str | None = None
    check: bool = False
    allow_unknown: bool = False
    pure: bool = False
    factory: bool = False
    title: str = "Wreath"
    version: str = "0.1.0"


class TypegenCliError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _generate(app: object, options: TypegenOptions) -> dict[str, str]:
    if options.target != "typescript":
        raise TypegenCliError(f"unknown typegen target {options.target!r}", exit_code=2)
    try:
        api = build_api_model(
            app,
            title=options.title,
            version=options.version,
            allow_unknown=options.allow_unknown,
        )
    except TypegenError as error:
        raise TypegenCliError(str(error)) from error
    return render_typescript(
        api,
        react_query=options.react_query,
        base_url_env=options.base_url_env,
        pure=options.pure,
    )


def _safe_target(output_dir: Path, name: str) -> Path:
    # Generated names are fixed, but validate anyway: no path can escape output.
    candidate = (output_dir / name).resolve()
    if output_dir.resolve() not in candidate.parents and candidate != output_dir.resolve():
        raise TypegenCliError(f"generated path {name!r} escapes the output directory")
    return output_dir / name


def _previous_owned(output_dir: Path) -> set[str]:
    manifest = output_dir / MANIFEST_NAME
    if not manifest.exists():
        return set()
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    files = document.get("files", [])
    if not isinstance(files, list):
        return set()
    return {name for name in files if isinstance(name, str)}


def check(files: dict[str, str], output_dir: Path) -> list[str]:
    """Return the reasons ``--check`` should fail; empty means up to date."""
    problems: list[str] = []
    for name, contents in files.items():
        path = output_dir / name
        if not path.exists():
            problems.append(f"missing generated file: {name}")
        elif path.read_bytes() != contents.encode("utf-8"):
            problems.append(f"stale generated file: {name}")
    stale = _previous_owned(output_dir) - set(files)
    for name in sorted(stale):
        problems.append(f"owned file no longer generated: {name}")
    return problems


def write(files: dict[str, str], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Remove previously-owned files that are no longer generated (only ours).
    for name in sorted(_previous_owned(output_dir) - set(files)):
        target = _safe_target(output_dir, name)
        if target.exists():
            target.unlink()
    for name, contents in files.items():
        target = _safe_target(output_dir, name)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(contents.encode("utf-8"))
        os.replace(temporary, target)


def run(app: object, options: TypegenOptions) -> int:
    files = _generate(app, options)
    output_dir = Path(options.output)
    if options.check:
        problems = check(files, output_dir)
        if problems:
            for problem in problems:
                print(f"wreath typegen --check: {problem}")
            return 1
        print(f"wreath typegen --check: {output_dir} is up to date ({len(files)} files)")
        return 0
    write(files, output_dir)
    print(f"wreath typegen: wrote {len(files)} files to {output_dir}")
    return 0


__all__ = ["TypegenCliError", "TypegenOptions", "check", "run", "write"]
