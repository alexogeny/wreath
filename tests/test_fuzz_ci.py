import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import pytest
import yaml

from wreath._fuzz import corpus_ci

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/fuzz.yml"
GUIDE = ROOT / "docs/guides/fuzzing.md"


class _Named(Protocol):
    name: str


class _CorpusConfig(Protocol):
    corpus_root: Path


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _commands(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_fuzz_workflow_is_least_privilege_and_bounds_concurrency() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    paths = workflow[True]["pull_request"]["paths"]
    assert "src/wreath/**" in paths
    assert "tests/test_fuzz_*.py" in paths
    assert "tools/fuzz_native/**" in paths
    assert workflow[True]["schedule"] == [{"cron": "17 3 * * *"}]
    assert workflow["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' }}"
    )
    assert workflow["jobs"]["campaign"]["strategy"]["max-parallel"] == 2
    assert workflow["jobs"]["pr-smoke"]["timeout-minutes"] == 20
    assert workflow["jobs"]["campaign"]["timeout-minutes"] == 15
    assert workflow["jobs"]["merge-corpus"]["timeout-minutes"] == 30


def test_pull_request_smoke_is_change_aware_and_cannot_write_the_corpus_cache() -> None:
    workflow = _workflow()
    smoke = workflow["jobs"]["pr-smoke"]
    commands = _commands(smoke)
    uses = [step.get("uses", "") for step in smoke["steps"]]
    assert smoke["if"] == "github.event_name == 'pull_request'"
    assert "--mutant changed" in commands
    assert '--mutant-changed "$BASE_SHA"' in commands
    assert "--mutant sample --mutant-samples 8" in commands
    assert 'if [ "$status" -ne 2 ]' in commands
    assert 'if [ "$status" -eq 0 ]; then' in commands
    assert "grep -Fq 'matched no mutations'" in commands
    assert "--fuzz-replay-only" in commands
    assert "--fuzz-backend all" in commands
    assert "actions/cache/restore@v5" in uses
    assert all("actions/cache/save@" not in use for use in uses)


def test_scheduled_campaigns_are_target_and_seed_sharded() -> None:
    workflow = _workflow()
    campaign = workflow["jobs"]["campaign"]
    matrix = campaign["strategy"]["matrix"]
    assert matrix == {
        "target": [
            "graphql-parser",
            "h2-frames",
            "http-replay-codec",
            "http1-parser",
            "multipart-parser",
            "xml-parser",
        ],
        "shard": [0, 1, 2, 3],
    }
    commands = _commands(campaign)
    assert "--mutant sample" in commands
    assert "--mutant off" not in commands
    assert '--fuzz-target "$TARGET"' in commands
    assert "--fuzz-backend all" in commands
    assert '--fuzz-seed "$SEED"' in commands
    assert "--fuzz-cases 50000" in commands
    assert "--fuzz-budget 240" in commands


def test_merge_validates_content_addresses_and_only_prunes_exact_duplicates() -> None:
    workflow = _workflow()
    merge = workflow["jobs"]["merge-corpus"]
    commands = _commands(merge)
    uses = [step.get("uses", "") for step in merge["steps"]]
    assert merge["needs"] == ["campaign"]
    assert "python -m wreath._fuzz.corpus_ci merge" in commands
    assert "python -m wreath._fuzz.corpus_ci finalize" in commands
    assert "python - <<'PY'" not in commands
    assert "actions/cache/save@v5" in uses
    assert "actions/download-artifact@v8" in uses


def _write_input(directory: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{digest}.input").write_bytes(data)
    return digest


def test_merge_shards_deduplicates_and_records_validated_inputs(tmp_path: Path) -> None:
    shards = tmp_path / "shards"
    output = tmp_path / "corpus"
    first = _write_input(shards / "one" / "graphql-parser", b"same")
    _write_input(shards / "two" / "graphql-parser", b"same")
    second = _write_input(shards / "one" / "xml-parser", b"other")

    manifest = corpus_ci.merge_shards(shards, output, run_id="17")

    assert manifest == {
        "duplicates_pruned": 1,
        "inputs": 2,
        "run_id": "17",
        "targets": list(corpus_ci.TARGET_NAMES),
    }
    assert sorted(path.stem for path in output.rglob("*.input")) == sorted([first, second])
    assert json.loads((output / "manifest.json").read_text()) == manifest


def test_merge_shards_refuses_a_false_content_address(tmp_path: Path) -> None:
    source = tmp_path / "shards" / "one" / "xml-parser" / f"{'0' * 64}.input"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not zero")

    with pytest.raises(SystemExit, match="not named by its SHA-256"):
        corpus_ci.merge_shards(tmp_path / "shards", tmp_path / "corpus", run_id="1")


def test_merge_shards_refuses_symlinked_input(tmp_path: Path) -> None:
    data = b"outside corpus"
    digest = hashlib.sha256(data).hexdigest()
    payload = tmp_path / "payload"
    payload.write_bytes(data)
    source = tmp_path / "shards" / "one" / "xml-parser" / f"{digest}.input"
    source.parent.mkdir(parents=True)
    source.symlink_to(payload)

    with pytest.raises(SystemExit, match="must be a non-symlink regular file"):
        corpus_ci.merge_shards(tmp_path / "shards", tmp_path / "corpus", run_id="1")


def test_finalize_manifest_describes_pruned_native_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shards = tmp_path / "shards"
    output = tmp_path / "corpus"
    initial: dict[str, list[str]] = {}
    for target in corpus_ci.TARGET_NAMES:
        initial[target] = []
        for index in range(3):
            initial[target].append(
                _write_input(shards / "one" / target, f"{target}-{index}".encode())
            )
    corpus_ci.merge_shards(shards, output, run_id="29")

    def fake_python_prune(target: _Named, corpus_root: Path) -> SimpleNamespace:
        directory = corpus_root / target.name
        paths = sorted(directory.glob("*.input"))
        for path in paths[1:]:
            path.unlink()
        return SimpleNamespace(target=target.name, before=3, after=1)

    def fake_native_merge(target: _Named, config: _CorpusConfig) -> SimpleNamespace:
        directory = config.corpus_root / target.name
        paths = sorted(directory.glob("*.input"))
        paths[0].unlink()
        paths[2].unlink()
        return SimpleNamespace(target=target.name, before=3, after=1)

    monkeypatch.setattr(corpus_ci, "prune_corpus", fake_python_prune)
    monkeypatch.setattr(corpus_ci, "merge_native_corpus", fake_native_merge)

    manifest = corpus_ci.finalize_corpus(
        output,
        native_input=tmp_path / "native-input",
        build_root=tmp_path / "native-build",
        artifact_root=tmp_path / "artifacts",
        project_root=tmp_path,
    )

    inventory = {
        target: sorted(path.stem for path in (output / target).glob("*.input"))
        for target in corpus_ci.TARGET_NAMES
    }
    expected = {target: sorted(digests)[:2] for target, digests in initial.items()}
    assert inventory == expected
    assert manifest["inputs"] == 2 * len(corpus_ci.TARGET_NAMES)
    assert manifest["target_inputs"] == {target: 2 for target in corpus_ci.TARGET_NAMES}
    assert manifest["sha256"] == inventory
    assert json.loads((output / "manifest.json").read_text()) == manifest


def test_corpora_reports_and_crashes_are_downloadable_with_declared_retention() -> None:
    rendered = WORKFLOW.read_text(encoding="utf-8")
    assert "retention-days: 90" in rendered
    assert "fuzz-corpus-" in rendered
    assert "fuzz-results-" in rendered
    assert "fuzz-corpus-merged-" in rendered
    guide = GUIDE.read_text(encoding="utf-8")
    assert "Caches are continuity hints, not durable storage" in guide
    assert "90 days" in guide
    assert "SHA-256" in guide
    assert "diagnostic.log" in guide
