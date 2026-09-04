"""The `wreath typegen` command: build the model and emit consumer files.

Generation renders every file in memory first, then writes through temporary
files replaced atomically, so a failure never leaves a half-written tree. Only
paths recorded in `wreath-typegen.json` are ever removed, and only inside the
selected output directory.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._fsguard import ContainmentError, open_beneath, open_root
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
            f"unknown typegen target {options.target!r}; known targets are {', '.join(TARGETS)}",
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
        try:
            return render_python(api, document=document, class_name=options.class_name)
        except TypegenError as error:
            raise TypegenCliError(str(error)) from error
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
    )


def _safe_target(output_dir: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
    ):
        raise TypegenCliError(f"generated path {name!r} escapes the output directory")
    return output_dir / name


def _read_target(root_fd: int, output_dir: Path, name: str) -> bytes | None:
    _safe_target(output_dir, name)
    try:
        descriptor, metadata = open_beneath(root_fd, name)
    except FileNotFoundError:
        return None
    except ContainmentError as error:
        raise TypegenCliError(
            f"generated path {name!r} escapes the output directory"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise TypegenCliError(f"generated path {name!r} is not a regular file")
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def _previous_owned_at(root_fd: int, output_dir: Path) -> set[str]:
    try:
        raw = _read_target(root_fd, output_dir, MANIFEST_NAME)
        if raw is None:
            return set()
        document = json.loads(raw)
    except OSError, ValueError:
        return set()
    files = document.get("files", [])
    if not isinstance(files, list):
        return set()
    return {name for name in files if isinstance(name, str)}


def _previous_owned(output_dir: Path) -> set[str]:
    try:
        root_fd = open_root(output_dir)
    except FileNotFoundError:
        return set()
    try:
        return _previous_owned_at(root_fd, output_dir)
    finally:
        os.close(root_fd)


def check(files: dict[str, str], output_dir: Path) -> list[str]:
    """Return the reasons `--check` should fail; empty means up to date."""
    problems: list[str] = []
    for name in files:
        _safe_target(output_dir, name)
    try:
        root_fd = open_root(output_dir)
    except FileNotFoundError:
        return [f"missing generated file: {name}" for name in files]
    try:
        for name, contents in files.items():
            current = _read_target(root_fd, output_dir, name)
            if current is None:
                problems.append(f"missing generated file: {name}")
            elif current != contents.encode("utf-8"):
                problems.append(f"stale generated file: {name}")
        stale = _previous_owned_at(root_fd, output_dir) - set(files)
    finally:
        os.close(root_fd)
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

    try:
        root_fd = open_root(output_dir)
    except FileNotFoundError:
        root_fd = -1
    try:
        raw = None if root_fd < 0 else _read_target(root_fd, output_dir, SPEC_FILE)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    pinned = _safe_target(output_dir, SPEC_FILE)
    if raw is None:
        raise TypegenCliError(
            f"no pinned document at {pinned}: generate the client first, so the "
            "gate has a baseline to compare against",
            exit_code=2,
        )
    try:
        previous = json.loads(raw)
    except (OSError, ValueError) as error:
        raise TypegenCliError(f"pinned document at {pinned} is unreadable") from error
    return compare_openapi(previous, current)


def write(files: dict[str, str], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    root_fd = open_root(output_dir)
    try:
        stale = _previous_owned_at(root_fd, output_dir) - set(files)
        names = sorted(stale | set(files))
        for name in names:
            _safe_target(output_dir, name)
            try:
                metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise TypegenCliError(
                    f"generated path {name!r} escapes the output directory"
                )
        for name in sorted(stale):
            try:
                os.unlink(name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
        for name, contents in files.items():
            temporary = f".{name}.{os.urandom(8).hex()}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(contents.encode("utf-8"))
                os.replace(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(root_fd)


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
