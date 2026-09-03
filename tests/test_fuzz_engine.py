from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from wreath import xml
from wreath._fuzz import (
    CampaignConfig,
    FuzzTarget,
    prune_corpus,
    publish_crash_finding,
    run_campaign,
)
from wreath._fuzz import engine as fuzz_engine
from wreath._fuzz.structured import StructuredStrategy


def _run_abrupt_campaign(root: str, journal: str) -> None:
    def terminate(_data: bytes) -> None:
        os._exit(91)

    base = Path(root)
    run_campaign(
        FuzzTarget("abrupt", terminate, seeds=(b"exact crash bytes",)),
        CampaignConfig(
            corpus_root=base / "corpus",
            artifact_root=base / "artifacts",
            journal_path=Path(journal),
            max_cases=1,
        ),
    )


def _run_shrink_crash_campaign(root: str, journal: str) -> None:
    original = b"LEFT-RIGHT"

    def terminate_shrink_candidate(data: bytes) -> None:
        if data == b"RIGHT":
            os._exit(92)
        if data == original:
            raise ValueError("shrink me")

    base = Path(root)
    run_campaign(
        FuzzTarget("shrink-crash", terminate_shrink_candidate, seeds=(original,)),
        CampaignConfig(
            corpus_root=base / "corpus",
            artifact_root=base / "artifacts",
            journal_path=Path(journal),
            max_cases=1,
            max_shrink_cases=16,
        ),
    )


def _write_journal(path: Path, *, target: str = "fatal", seed: int = 73) -> bytes:
    data = b"fatal input"
    path.write_text(
        json.dumps(
            {
                "campaign_seed": seed,
                "case_ordinal": 4,
                "input_hex": data.hex(),
                "input_sha256": hashlib.sha256(data).hexdigest(),
                "target": target,
                "version": 1,
            }
        )
    )
    return data


def _config(tmp_path: Path, **overrides: object) -> CampaignConfig:
    values: dict[str, Any] = {
        "seed": 73,
        "max_cases": 40,
        "max_seconds": 2.0,
        "max_input_size": 64,
        "max_shrink_cases": 128,
        "max_findings": 4,
        "corpus_root": tmp_path / "corpus",
        "artifact_root": tmp_path / "artifacts",
    }
    values.update(overrides)
    return CampaignConfig(**values)


def test_campaign_persists_content_addressed_corpus_atomically(tmp_path: Path) -> None:
    def target(data: bytes) -> tuple[str, ...]:
        return (f"length:{len(data)}", f"first:{data[:1].hex()}")

    report = run_campaign(
        FuzzTarget("bytes", target, seeds=(b"", b"seed")),
        _config(tmp_path),
    )

    files = sorted((tmp_path / "corpus" / "bytes").glob("*.input"))
    assert report.corpus_added > 0
    assert len(files) == report.corpus_size
    assert all(path.stem == hashlib.sha256(path.read_bytes()).hexdigest() for path in files)
    assert not list((tmp_path / "corpus" / "bytes").glob("*.tmp"))


def test_corpus_pruning_keeps_the_smallest_union_of_observed_features(tmp_path: Path) -> None:
    target = FuzzTarget(
        "prune",
        lambda data: ("empty" if not data else f"first:{data[:1].decode()}",),
    )
    directory = tmp_path / "corpus" / target.name
    directory.mkdir(parents=True)
    for data in (b"AAA", b"A", b"BB", b"B", b""):
        digest = hashlib.sha256(data).hexdigest()
        (directory / f"{digest}.input").write_bytes(data)

    report = prune_corpus(target, tmp_path / "corpus", max_input_size=64)

    assert report.before == 5
    assert report.after == 3
    assert report.removed == 2
    assert {path.read_bytes() for path in directory.iterdir()} == {b"", b"A", b"B"}


def test_same_seed_replays_the_same_campaign(tmp_path: Path) -> None:
    def target(data: bytes) -> tuple[str, ...]:
        return (f"shape:{len(data)}:{sum(data) % 7}",)

    fuzz_target = FuzzTarget("stable", target, seeds=(b"a", b"bbb"))
    first = run_campaign(fuzz_target, _config(tmp_path / "one"))
    second = run_campaign(fuzz_target, _config(tmp_path / "two"))

    assert first.seed == second.seed == 73
    assert first.generated_digests == second.generated_digests
    assert first.corpus_digests == second.corpus_digests


def test_campaign_records_the_initial_corpus_snapshot_for_seed_replay(tmp_path: Path) -> None:
    target = FuzzTarget("snapshot", lambda data: (data.hex(),), seeds=(b"built-in",))
    corpus = tmp_path / "corpus" / target.name
    corpus.mkdir(parents=True)
    persisted = b"persisted"
    digest = hashlib.sha256(persisted).hexdigest()
    (corpus / f"{digest}.input").write_bytes(persisted)

    report = run_campaign(target, _config(tmp_path, max_cases=1))

    assert report.initial_corpus_digests == (digest,)


def test_structured_generation_participates_in_the_guided_campaign(tmp_path: Path) -> None:
    strategy = StructuredStrategy(
        "test-grammar",
        3,
        generate=lambda _rng, _max_size: b"STRUCTURED",
    )
    report = run_campaign(
        FuzzTarget(
            "structured",
            lambda data: (f"input:{data.decode(errors='replace')}",),
            seeds=(b"seed",),
            strategy=strategy,
        ),
        _config(tmp_path, max_cases=2),
    )

    digest = hashlib.sha256(b"STRUCTURED").hexdigest()
    assert digest in report.generated_digests
    assert digest in report.corpus_digests
    assert report.structured_strategy == "test-grammar@3"


def test_structured_shrinker_gets_the_first_bounded_failure_attempt(tmp_path: Path) -> None:
    strategy = StructuredStrategy(
        "test-shrinker",
        1,
        shrink=lambda _data: (b"BUG",),
    )

    def target(data: bytes) -> None:
        if b"BUG" in data:
            raise ValueError("bug reached")

    report = run_campaign(
        FuzzTarget(
            "structured-shrink",
            target,
            seeds=(b"padding-BUG-trailer",),
            strategy=strategy,
        ),
        _config(tmp_path, max_cases=1, max_shrink_cases=1),
    )

    assert report.findings[0].input_path.read_bytes() == b"BUG"


def test_none_seed_is_generated_and_reported(tmp_path: Path) -> None:
    report = run_campaign(
        FuzzTarget("fresh", lambda _data: None, seeds=(b"seed",)),
        _config(tmp_path, seed=None, max_cases=1),
    )

    assert 0 <= report.seed < 2**64


def test_coverage_and_semantic_features_grow_the_corpus(tmp_path: Path) -> None:
    def target(data: bytes) -> tuple[str, ...]:
        if data.startswith(b"A"):
            marker = "a"
        else:
            marker = "other"
        return (marker,)

    report = run_campaign(
        FuzzTarget("guided", target, seeds=(b"A", b"B"), source_files=(__file__,)),
        _config(tmp_path, max_cases=2),
    )

    assert report.coverage_features >= 2
    assert report.semantic_features == 2
    assert report.corpus_size == 2


def test_python_branch_arms_have_distinct_coverage_features(tmp_path: Path) -> None:
    def target(data: bytes) -> tuple[str, ...]:
        return ("same",) if data else ("same",)

    one_arm = run_campaign(
        FuzzTarget("one-arm", target, seeds=(b"",), source_files=(__file__,)),
        _config(tmp_path / "one", max_cases=1),
    )
    both_arms = run_campaign(
        FuzzTarget("both-arms", target, seeds=(b"", b"x"), source_files=(__file__,)),
        _config(tmp_path / "both", max_cases=2),
    )

    assert one_arm.semantic_features == both_arms.semantic_features == 1
    assert both_arms.coverage_features > one_arm.coverage_features


def test_native_only_target_does_not_enable_python_monitoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_monitoring(*_args: object) -> None:
        raise AssertionError("native targets must not enable Python line events")

    monkeypatch.setattr(fuzz_engine.sys.monitoring, "use_tool_id", unexpected_monitoring)
    report = run_campaign(
        FuzzTarget(
            "native",
            lambda data: (f"length:{len(data)}",),
            seeds=(b"input",),
            source_files=("parser.c",),
        ),
        _config(tmp_path, max_cases=1),
    )

    assert report.cases_executed == 1
    assert report.coverage_features == 0
    assert report.semantic_features == 1


def test_relative_package_source_is_resolved_independently_of_caller_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def parse(data: bytes) -> None:
        xml.parse(data)

    source_files = ("src/wreath/xml.py",)
    target = FuzzTarget(
        "relative-source",
        parse,
        seeds=(b"<root/>",),
        source_files=source_files,
    )
    away = tmp_path / "away"
    away.mkdir()
    monkeypatch.chdir(away)

    report = run_campaign(target, _config(tmp_path, max_cases=1))

    assert report.coverage_features > 0
    assert target.source_files == source_files


def test_deterministic_failure_is_shrunk_and_saved_for_replay(tmp_path: Path) -> None:
    def target(data: bytes) -> None:
        if b"BUG" in data:
            raise ValueError("bug reached")

    report = run_campaign(
        FuzzTarget("shrinker", target, seeds=(b"padding-BUG-trailer",)),
        _config(tmp_path, max_cases=1),
    )

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.deterministic is True
    assert finding.minimized_size < finding.original_size
    assert finding.input_path.read_bytes() == b"BUG"
    metadata = json.loads(finding.metadata_path.read_text())
    assert metadata["target"] == "shrinker"
    assert metadata["campaign_seed"] == 73
    assert metadata["input_sha256"] == hashlib.sha256(b"BUG").hexdigest()
    assert metadata["exception_type"] == "builtins.ValueError"


def test_nondeterministic_failure_is_not_shrunk(tmp_path: Path) -> None:
    calls = 0

    def target(_data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("once")

    report = run_campaign(
        FuzzTarget("flaky", target, seeds=(b"unchanged",)),
        _config(tmp_path, max_cases=1),
    )

    finding = report.findings[0]
    assert finding.deterministic is False
    assert finding.input_path.read_bytes() == b"unchanged"
    assert finding.minimized_size == finding.original_size


def test_process_control_exceptions_are_not_recorded_as_findings(tmp_path: Path) -> None:
    def target(_data: bytes) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_campaign(
            FuzzTarget("interrupt", target, seeds=(b"input",)),
            _config(tmp_path, max_cases=1),
        )


def test_case_and_input_bounds_are_enforced_before_target_execution(tmp_path: Path) -> None:
    seen: list[int] = []

    def target(data: bytes) -> None:
        seen.append(len(data))

    report = run_campaign(
        FuzzTarget("bounded", target, seeds=(b"x" * 200, b"ok")),
        _config(tmp_path, max_cases=3, max_input_size=8),
    )

    assert report.cases_executed == 3
    assert len(seen) == 3
    assert max(seen) <= 8
    assert report.stop_reason == "case-limit"


def test_replay_only_executes_each_seed_and_corpus_entry_once(tmp_path: Path) -> None:
    existing = b"persisted"
    existing_digest = hashlib.sha256(existing).hexdigest()
    corpus = tmp_path / "corpus" / "replay"
    corpus.mkdir(parents=True)
    (corpus / f"{existing_digest}.input").write_bytes(existing)
    seen: list[bytes] = []

    report = run_campaign(
        FuzzTarget("replay", lambda data: seen.append(data), seeds=(b"seed", b"seed")),
        _config(tmp_path, generate=False, max_cases=20),
    )

    assert seen == [b"seed", b"persisted"]
    assert report.cases_executed == 2
    assert report.stop_reason == "corpus-exhausted"


def test_elapsed_time_bound_is_checked_before_target_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter((10.0, 11.1, 11.1))
    monkeypatch.setattr(fuzz_engine.time, "monotonic", lambda: next(ticks))
    called = False

    def target(_data: bytes) -> None:
        nonlocal called
        called = True

    report = run_campaign(
        FuzzTarget("timed", target, seeds=(b"seed",)),
        _config(tmp_path, max_seconds=1.0),
    )

    assert called is False
    assert report.cases_executed == 0
    assert report.stop_reason == "time-limit"


def test_active_input_journal_exists_during_case_and_clears_after_return(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "active.json"

    def target(data: bytes) -> None:
        payload = json.loads(journal.read_text())
        assert bytes.fromhex(payload["input_hex"]) == data
        assert payload["target"] == "journal"
        assert payload["campaign_seed"] == 73
        assert payload["case_ordinal"] == 0

    run_campaign(
        FuzzTarget("journal", target, seeds=(b"active",)),
        _config(tmp_path, journal_path=journal, max_cases=1),
    )

    assert not journal.exists()


def test_abrupt_child_death_leaves_exact_active_input(tmp_path: Path) -> None:
    journal = tmp_path / "active.json"
    process = multiprocessing.get_context("fork").Process(
        target=_run_abrupt_campaign,
        args=(str(tmp_path), str(journal)),
    )
    process.start()
    process.join(5)

    assert process.exitcode == 91, process.exitcode
    payload = json.loads(journal.read_text())
    assert bytes.fromhex(payload["input_hex"]) == b"exact crash bytes"
    assert payload["input_sha256"] == hashlib.sha256(b"exact crash bytes").hexdigest()
    assert payload["target"] == "abrupt"


def test_shrink_candidate_death_replaces_journal_with_exact_candidate(tmp_path: Path) -> None:
    journal = tmp_path / "active.json"
    process = multiprocessing.get_context("fork").Process(
        target=_run_shrink_crash_campaign,
        args=(str(tmp_path), str(journal)),
    )
    process.start()
    process.join(5)

    assert process.exitcode == 92, process.exitcode
    payload = json.loads(journal.read_text())
    assert bytes.fromhex(payload["input_hex"]) == b"RIGHT"
    assert payload["input_sha256"] == hashlib.sha256(b"RIGHT").hexdigest()
    assert payload["target"] == "shrink-crash"


def test_existing_finding_with_mismatched_metadata_is_refused(tmp_path: Path) -> None:
    data = b"boom"
    digest = hashlib.sha256(data).hexdigest()
    artifact = tmp_path / "artifacts" / "finding" / digest
    artifact.mkdir(parents=True)
    (artifact / "input").write_bytes(data)
    (artifact / "metadata.json").write_text(
        json.dumps({"input_sha256": digest, "target": "somewhere-else"})
    )

    def target(candidate: bytes) -> None:
        if candidate == data:
            raise ValueError("failure")

    with pytest.raises(ValueError, match="does not describe this finding"):
        run_campaign(
            FuzzTarget("finding", target, seeds=(data,)),
            _config(tmp_path, max_cases=1),
        )


def test_concurrent_identical_finding_winner_is_validated_and_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replace = fuzz_engine.os.replace

    def race(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if source_path.is_dir():
            shutil.copytree(source_path, destination)
            raise FileExistsError(destination)
        replace(source, destination)

    monkeypatch.setattr(fuzz_engine.os, "replace", race)

    def target(_data: bytes) -> None:
        raise ValueError("failure")

    report = run_campaign(
        FuzzTarget("race", target, seeds=(b"boom",)),
        _config(tmp_path, max_cases=1),
    )

    assert len(report.findings) == 1
    assert report.findings[0].input_path.read_bytes() == b""


def test_fatal_crash_journal_is_validated_published_and_cleared(tmp_path: Path) -> None:
    journal = tmp_path / "active.json"
    data = _write_journal(journal)
    finding = publish_crash_finding(
        journal,
        tmp_path / "artifacts",
        FuzzTarget("fatal", lambda _data: None),
        73,
        exception_type="signal.SIGSEGV",
        exception_message="target terminated by signal 11",
        diagnostic=b"AddressSanitizer: heap-buffer-overflow\n",
    )

    assert finding.input_path.read_bytes() == data
    assert finding.deterministic is False
    assert finding.exception_type == "signal.SIGSEGV"
    assert not journal.exists()
    metadata = json.loads(finding.metadata_path.read_text())
    assert metadata["campaign_seed"] == 73
    assert metadata["exception_message"] == "target terminated by signal 11"
    diagnostic = finding.metadata_path.parent / "diagnostic.log"
    assert diagnostic.read_bytes() == b"AddressSanitizer: heap-buffer-overflow\n"
    assert metadata["diagnostic_sha256"] == hashlib.sha256(diagnostic.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", 2, "version"),
        ("target", "other", "target"),
        ("campaign_seed", 72, "seed"),
        ("input_sha256", "0" * 64, "SHA-256"),
    ],
)
def test_fatal_crash_journal_refuses_mismatched_identity(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    journal = tmp_path / "active.json"
    _write_journal(journal)
    payload = json.loads(journal.read_text())
    payload[field] = value
    journal.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=message):
        publish_crash_finding(
            journal,
            tmp_path / "artifacts",
            FuzzTarget("fatal", lambda _data: None),
            73,
            exception_type="signal.SIGABRT",
            exception_message="target terminated by signal 6",
        )

    assert journal.exists()


def test_fatal_crash_publication_accepts_a_valid_concurrent_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "active.json"
    data = _write_journal(journal)
    replace = fuzz_engine.os.replace

    def race(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if source_path.is_dir():
            shutil.copytree(source_path, destination)
            raise FileExistsError(destination)
        replace(source, destination)

    monkeypatch.setattr(fuzz_engine.os, "replace", race)
    finding = publish_crash_finding(
        journal,
        tmp_path / "artifacts",
        FuzzTarget("fatal", lambda _data: None),
        73,
        exception_type="signal.SIGSEGV",
        exception_message="target terminated by signal 11",
    )

    assert finding.input_path.read_bytes() == data
    assert not journal.exists()


@pytest.mark.parametrize(
    ("field", "value", "correct_form"),
    [
        ("max_cases", 0, "max_cases must be a positive integer"),
        ("max_seconds", 0.0, "max_seconds must be positive"),
        ("max_input_size", 0, "max_input_size must be a positive integer"),
    ],
)
def test_invalid_bounds_name_the_field_and_correct_form(
    tmp_path: Path,
    field: str,
    value: object,
    correct_form: str,
) -> None:
    with pytest.raises(ValueError, match=correct_form):
        _config(tmp_path, **{field: value})


def test_malformed_corpus_entry_is_refused(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus" / "broken"
    corpus.mkdir(parents=True)
    (corpus / f"{'0' * 64}.input").write_bytes(b"not that digest")

    with pytest.raises(ValueError, match="corpus entry.*SHA-256"):
        run_campaign(
            FuzzTarget("broken", lambda _data: None),
            _config(tmp_path, max_cases=1),
        )


def test_temporary_corpus_entry_is_refused(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus" / "broken"
    corpus.mkdir(parents=True)
    (corpus / ".tmp-interrupted").write_bytes(b"partial")

    with pytest.raises(ValueError, match="corpus entry.*malformed"):
        run_campaign(FuzzTarget("broken", lambda _data: None), _config(tmp_path, max_cases=1))


def test_existing_malformed_artifact_is_refused(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "broken" / ("a" * 64)
    artifact.mkdir(parents=True)
    (artifact / "input").write_bytes(b"payload")

    with pytest.raises(ValueError, match="artifact.*metadata.json"):
        run_campaign(
            FuzzTarget("broken", lambda _data: None),
            _config(tmp_path, max_cases=1),
        )
