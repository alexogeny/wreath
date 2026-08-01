"""The `wreath typegen` command: build the model and emit consumer files.

Generation renders every file in memory first, then writes through temporary
files replaced atomically, so a failure never leaves a half-written tree. Only
paths recorded in `wreath-typegen.json` are ever removed, and only inside the
selected output directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .inspect import build_api_model
from .model import TypegenError
from .targets.typescript import MANIFEST_NAME, render_typescript


@dataclass(frozen=True, slots=True)
class TypegenOptions:
    target: str
    output: str
    react_query: bool = False
    base_url_env: str | None = None
    #: Files on disk are stale — regenerate and diff. A build hygiene check.
    check: bool = False
    #: The *provider* has changed incompatibly — semantic, compared against the
    #: document the package was generated from. A different question from
    #: `check`, so a different flag: one asks whether you forgot to regenerate,
    #: the other whether regenerating would break you.
    check_contract: bool = False
    allow_unknown: bool = False
    pure: bool = False
    factory: bool = False
    title: str = "Wreath"
    version: str = "0.1.0"
    #: Python target only: the name of the generated `ServiceClient` subclass.
    class_name: str = "GeneratedServiceClient"


class TypegenCliError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


#: Targets this CLI can emit. Named here so an unknown one is refused with the
#: list rather than a bare "unknown target".
TARGETS = ("typescript", "python", "proto")


def _generate(app: object, options: TypegenOptions) -> dict[str, str]:
    if options.target not in TARGETS:
        raise TypegenCliError(
            f"unknown typegen target {options.target!r}; "
            f"known targets are {', '.join(TARGETS)}",
            exit_code=2,
        )
    try:
        api = build_api_model(
            app,
            title=options.title,
            version=options.version,
            allow_unknown=options.allow_unknown,
        )
    except TypegenError as error:
        raise TypegenCliError(str(error)) from error
    if options.target == "python":
        from ..openapi import generate_openapi
        from .targets.python import render_python

        # The digest pins the *document*, not the model, because the document
        # is what `compare_openapi` reasons about.
        document = generate_openapi(app, title=options.title, version=options.version)
        return render_python(
            api, document=document, class_name=options.class_name
        )
    if options.target == "proto":
        from .targets.proto import ProtoTargetError, render_proto

        try:
            return render_proto(api)
        except ProtoTargetError as error:
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
    """Return the reasons `--check` should fail; empty means up to date."""
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


def check_contract(current: dict[str, Any], output_dir: Path) -> tuple[Any, ...]:
    """Backwards-incompatible changes between the pinned document and `current`.

    Empty means the provider is still compatible; additions are compatible and
    are not reported. Raises when nothing is pinned, rather than returning
    empty: a gate that passes because it found no baseline is a gate with
    nothing to check, and it would read as a green build forever.
    """
    from ..openapi import compare_openapi
    from .targets.python import SPEC_FILE

    pinned = output_dir / SPEC_FILE
    if not pinned.exists():
        raise TypegenCliError(
            f"no pinned document at {pinned}: generate the client first, so the "
            "gate has a baseline to compare against",
            exit_code=2,
        )
    try:
        previous = json.loads(pinned.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TypegenCliError(f"pinned document at {pinned} is unreadable") from error
    return compare_openapi(previous, current)


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
    output_dir = Path(options.output)
    if options.check_contract:
        if options.target != "python":
            raise TypegenCliError(
                "--check-contract needs a pinned document, which only the "
                f"python target emits; {options.target!r} does not",
                exit_code=2,
            )
        from ..openapi import generate_openapi

        current = generate_openapi(app, title=options.title, version=options.version)
        changes = check_contract(current, output_dir)
        if changes:
            for change in changes:
                print(f"wreath typegen --check-contract: {change.kind}: {change.detail}")
            print(
                f"wreath typegen --check-contract: {len(changes)} breaking change(s); "
                "regenerate the client and fix the call sites"
            )
            return 1
        print("wreath typegen --check-contract: the provider is still compatible")
        return 0
    files = _generate(app, options)
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


__all__ = [
    "TypegenCliError",
    "TypegenOptions",
    "check",
    "check_contract",
    "run",
    "write",
]
