from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from wreath._fuzz import prune_corpus
from wreath._fuzz.native import (
    NATIVE_TARGETS,
    NativeCampaignConfig,
    merge_native_corpus,
    native_target,
)
from wreath._fuzz_targets import TARGETS

TARGET_NAMES = tuple(sorted(target.name for target in TARGETS))
_NATIVE_NAMES = tuple(sorted(target.name for target in NATIVE_TARGETS))

if _NATIVE_NAMES != TARGET_NAMES:
    raise RuntimeError("CI corpus merging requires one native harness per fuzz target")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_input(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"corpus input {path} must be a non-symlink regular file")
    data = path.read_bytes()
    digest = _digest(data)
    if path.suffix != ".input" or path.stem != digest:
        raise SystemExit(f"{path} is not named by its SHA-256 {digest}")
    return data


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def merge_shards(source_root: Path, output_root: Path, *, run_id: str) -> dict[str, Any]:
    sources = sorted(Path(source_root).rglob("*.input"))
    if not sources:
        raise SystemExit("no shard corpus inputs were downloaded")
    output = Path(output_root)
    unique = 0
    duplicates = 0
    for source in sources:
        matches = set(TARGET_NAMES).intersection(source.parts)
        if len(matches) != 1:
            raise SystemExit(f"cannot identify one fuzz target for {source}")
        target = matches.pop()
        data = _read_input(source)
        destination = output / target / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != data:
                raise SystemExit(f"content collision at {destination}")
            duplicates += 1
            continue
        destination.write_bytes(data)
        unique += 1
    manifest = {
        "duplicates_pruned": duplicates,
        "inputs": unique,
        "run_id": str(run_id),
        "targets": list(TARGET_NAMES),
    }
    _write_manifest(output, manifest)
    return manifest


def _validated_target(root: Path, target: str) -> list[str]:
    paths = sorted((root / target).glob("*.input"))
    for path in paths:
        _read_input(path)
    return [path.stem for path in paths]


def _validated_inventory(root: Path) -> dict[str, list[str]]:
    inventory = {target: _validated_target(root, target) for target in TARGET_NAMES}
    known_paths = {
        root / target / f"{digest}.input"
        for target, digests in inventory.items()
        for digest in digests
    }
    unexpected = set(root.rglob("*.input")) - known_paths
    if unexpected:
        rendered = ", ".join(str(path) for path in sorted(unexpected))
        raise SystemExit(f"corpus inputs must be direct children of a declared target: {rendered}")
    return inventory


def _load_merge_manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"merge manifest {path} is missing or invalid JSON") from error
    if not isinstance(manifest, dict) or manifest.get("targets") != list(TARGET_NAMES):
        raise SystemExit(
            f"merge manifest {path} must declare exactly these targets: {', '.join(TARGET_NAMES)}"
        )
    return manifest


def _check_report(report: object, target: str, actual: int, phase: str) -> None:
    if getattr(report, "target", None) != target or getattr(report, "after", None) != actual:
        raise RuntimeError(
            f"{phase} prune report for {target} must match its final {actual} inputs"
        )


def finalize_corpus(
    corpus_root: Path,
    *,
    native_input: Path,
    build_root: Path,
    artifact_root: Path,
    project_root: Path,
) -> dict[str, Any]:
    root = Path(corpus_root)
    manifest = _load_merge_manifest(root)
    initial_inventory = _validated_inventory(root)
    native_root = Path(native_input)
    if native_root.exists():
        raise ValueError(f"native merge input {native_root} must not already exist")
    for name in _NATIVE_NAMES:
        shutil.copytree(root / name, native_root / name)

    python_reports = []
    for target in TARGETS:
        report = prune_corpus(target, root)
        actual = len(_validated_target(root, target.name))
        _check_report(report, target.name, actual, "Python feedback")
        python_reports.append(report)

    native_reports = []
    for name in _NATIVE_NAMES:
        report = merge_native_corpus(
            native_target(name),
            NativeCampaignConfig(
                project_root=Path(project_root),
                build_root=Path(build_root),
                corpus_root=native_root,
                artifact_root=Path(artifact_root),
                max_seconds=120,
                rebuild=True,
            ),
        )
        native_sources = sorted((native_root / name).glob("*.input"))
        for source in native_sources:
            data = _read_input(source)
            destination = root / name / source.name
            if destination.exists() and destination.read_bytes() != data:
                raise SystemExit(f"content collision at {destination}")
            if not destination.exists():
                destination.write_bytes(data)
        _check_report(report, name, len(native_sources), "native feedback")
        native_reports.append(report)

    inventory = _validated_inventory(root)
    manifest.update(
        {
            "initial_inputs": sum(len(digests) for digests in initial_inventory.values()),
            "inputs": sum(len(digests) for digests in inventory.values()),
            "native_feedback_prune": {
                report.target: {"before": report.before, "after": report.after}
                for report in native_reports
            },
            "python_feedback_prune": {
                report.target: {"before": report.before, "after": report.after}
                for report in python_reports
            },
            "sha256": inventory,
            "target_inputs": {target: len(digests) for target, digests in inventory.items()},
        }
    )
    _write_manifest(root, manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    merge = commands.add_parser("merge")
    merge.add_argument("--source", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--run-id", required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--corpus", type=Path, required=True)
    finalize.add_argument("--native-input", type=Path, required=True)
    finalize.add_argument("--build-root", type=Path, required=True)
    finalize.add_argument("--artifact-root", type=Path, required=True)
    finalize.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "merge":
        merge_shards(arguments.source, arguments.output, run_id=arguments.run_id)
    else:
        finalize_corpus(
            arguments.corpus,
            native_input=arguments.native_input,
            build_root=arguments.build_root,
            artifact_root=arguments.artifact_root,
            project_root=arguments.project_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
