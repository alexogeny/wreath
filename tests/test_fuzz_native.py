from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from wreath._fuzz import native as native_module
from wreath._fuzz.native import (
    NATIVE_TARGETS,
    NativeCampaignConfig,
    build_native_target,
    merge_native_corpus,
    native_target,
    run_native_campaign,
)


def test_every_fuzz_target_has_a_native_libfuzzer_harness() -> None:
    assert {target.name for target in NATIVE_TARGETS} == {
        "graphql-parser",
        "h2-frames",
        "http-replay-codec",
        "http1-parser",
        "multipart-parser",
        "xml-parser",
    }
    assert all(target.harness.name.endswith("_harness.c") for target in NATIVE_TARGETS)
    root = Path(__file__).parents[1]
    assert all((root / target.harness).is_file() for target in NATIVE_TARGETS)
    assert native_target("h2-frames").build_scripts == (
        Path("tools/sanitizers/setup_core.py"),
        Path("tools/sanitizers/setup_server.py"),
    )
    assert native_target("h2-frames").extension_globs == ("_core*.so", "_server*.so")
    with pytest.raises(ValueError, match="unknown native fuzz target 'missing'"):
        native_target("missing")


def test_native_campaign_bounds_refuse_invalid_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_seconds must be a positive integer"):
        NativeCampaignConfig(
            project_root=tmp_path,
            build_root=tmp_path / "build",
            corpus_root=tmp_path / "corpus",
            artifact_root=tmp_path / "artifacts",
            max_seconds=0,
        )
    with pytest.raises(ValueError, match="max_input_size must be a positive integer"):
        NativeCampaignConfig(
            project_root=tmp_path,
            build_root=tmp_path / "build",
            corpus_root=tmp_path / "corpus",
            artifact_root=tmp_path / "artifacts",
            max_input_size=0,
        )
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )
    assert config.max_runs == 0
    assert native_module._run_arguments(
        NativeCampaignConfig(
            project_root=tmp_path,
            build_root=tmp_path / "build",
            corpus_root=tmp_path / "corpus",
            artifact_root=tmp_path / "artifacts",
            replay_only=True,
        )
    ) == ("-runs=0",)

    with pytest.raises(ValueError, match="max_input_seconds must be a positive integer"):
        NativeCampaignConfig(
            project_root=tmp_path,
            build_root=tmp_path / "build",
            corpus_root=tmp_path / "corpus",
            artifact_root=tmp_path / "artifacts",
            max_input_seconds=0,
        )
    with pytest.raises(ValueError, match="max_build_seconds must be a positive integer"):
        NativeCampaignConfig(
            project_root=tmp_path,
            build_root=tmp_path / "build",
            corpus_root=tmp_path / "corpus",
            artifact_root=tmp_path / "artifacts",
            max_build_seconds=0,
        )


def test_native_build_command_has_a_controller_timeout(monkeypatch) -> None:
    command = ["clang", "--version"]

    def run(actual, **kwargs):
        raise subprocess.TimeoutExpired(actual, kwargs["timeout"], "partial", "blocked")

    monkeypatch.setattr(native_module.subprocess, "run", run)

    with pytest.raises(
        RuntimeError,
        match=r"clang --version.*17-second controller timeout",
    ):
        native_module._run_checked(command, timeout=17)


def test_native_staging_refuses_oversized_persistent_corpus_input(tmp_path: Path) -> None:
    data = b"x" * 65
    corpus = tmp_path / "corpus/http1-parser"
    corpus.mkdir(parents=True)
    (corpus / f"{hashlib.sha256(data).hexdigest()}.input").write_bytes(data)
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
        max_input_size=64,
    )

    with pytest.raises(ValueError, match="exceeds max_input_size"):
        native_module._stage_inputs(native_target("http1-parser"), config)


def test_native_staging_refuses_non_content_addressed_corpus_input(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus/http1-parser"
    corpus.mkdir(parents=True)
    (corpus / "unexpected.tmp").write_bytes(b"input")
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )

    with pytest.raises(ValueError, match="use <content SHA-256>.input files"):
        native_module._stage_inputs(native_target("http1-parser"), config)


def test_native_staging_reports_the_corpus_before_builtin_seeds(tmp_path: Path) -> None:
    data = b"prior"
    corpus = tmp_path / "corpus/http1-parser"
    corpus.mkdir(parents=True)
    digest = hashlib.sha256(data).hexdigest()
    (corpus / f"{digest}.input").write_bytes(data)
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )

    staged_corpus, _, initial_digests = native_module._stage_inputs(
        native_target("http1-parser"), config
    )

    assert staged_corpus == corpus
    assert initial_digests == (digest,)
    assert len(tuple(corpus.glob("*.input"))) > 1


@pytest.mark.parametrize(
    ("diagnostic", "signature"),
    [
        ("ERROR: libFuzzer: deadly signal", ("libfuzzer", "deadly-signal")),
        ("ERROR: libFuzzer: fuzz target exited", ("libfuzzer", "target-exited")),
    ],
)
def test_native_failure_signature_covers_harness_abort(
    diagnostic: str, signature: tuple[str, str]
) -> None:
    assert native_module._failure_signature(diagnostic) == signature


def test_build_uses_fuzzer_sanitizers_and_coverage(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repository"
    (root / "src/wreath").mkdir(parents=True)
    (root / "src/wreath/__init__.py").write_text("")
    (root / "tools/sanitizers").mkdir(parents=True)
    (root / "tools/sanitizers/setup_core.py").write_text("")
    harness = root / "tools/fuzz_native/http1_harness.c"
    harness.parent.mkdir(parents=True)
    harness.write_text("int LLVMFuzzerTestOneInput(void) { return 0; }")
    (harness.parent / "harness.c").write_text("")
    (harness.parent / "harness.h").write_text("")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "clang version 20\n", "")
        if "build_ext" in command:
            extension = tmp_path / "build/targets/http1-parser/lib/wreath/_native/_core.so"
            extension.parent.mkdir(parents=True)
            extension.write_bytes(b"__asan_init __ubsan_handle __sanitizer_cov")
        else:
            executable = Path(command[command.index("-o") + 1])
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"binary __asan_init __ubsan_handle __sanitizer_cov")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("wreath._fuzz.native._compiler", lambda: "/usr/bin/clang")
    monkeypatch.setattr("wreath._fuzz.native.subprocess.run", run)
    config = NativeCampaignConfig(
        project_root=root,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )

    executable = build_native_target(native_target("http1-parser"), config)

    assert executable.read_bytes().startswith(b"binary")
    build_call = next(call for call in calls if "build_ext" in call[0])
    build_environment = build_call[1]["env"]
    assert "-fsanitize=fuzzer-no-link,address,undefined" in build_environment["CFLAGS"]
    harness_command = next(call[0] for call in calls if "-o" in call[0])
    assert "-fsanitize=fuzzer,address,undefined" in harness_command
    assert "-fsanitize-coverage=trace-cmp,trace-div,indirect-calls" in harness_command
    manifest = json.loads(executable.with_suffix(".build.json").read_text())
    assert manifest["schema"] == 4
    assert len(manifest["source_sha256"]) == 64
    assert manifest["python"] == native_module._python_build_identity()
    assert manifest["compiler"]["path"] == "/usr/bin/clang"
    assert manifest["compiler"]["version"]
    assert all(call[1]["timeout"] == config.max_build_seconds for call in calls)


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/wreath/__init__.py",
        "tools/fuzz_native/http1_harness.c",
        "tools/fuzz_native/harness.c",
        "tools/fuzz_native/harness.h",
        "tools/sanitizers/setup_core.py",
    ],
)
def test_reused_build_refuses_changed_build_input(
    tmp_path: Path, monkeypatch, relative_path: str
) -> None:
    root = tmp_path / "repository"
    (root / "src/wreath").mkdir(parents=True)
    (root / "src/wreath/__init__.py").write_text("")
    (root / "tools/sanitizers").mkdir(parents=True)
    (root / "tools/sanitizers/setup_core.py").write_text("")
    harness_dir = root / "tools/fuzz_native"
    harness_dir.mkdir(parents=True)
    (harness_dir / "http1_harness.c").write_text("")
    (harness_dir / "harness.c").write_text("")
    (harness_dir / "harness.h").write_text("")

    def run(command, **kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "clang version 20\n", "")
        if "build_ext" in command:
            extension = tmp_path / "build/targets/http1-parser/lib/wreath/_native/_core.so"
            extension.parent.mkdir(parents=True, exist_ok=True)
            extension.write_bytes(b"__asan_init __ubsan_handle __sanitizer_cov")
        else:
            executable = Path(command[command.index("-o") + 1])
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"binary __asan_init __ubsan_handle __sanitizer_cov")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(native_module, "_compiler", lambda: "/usr/bin/clang")
    monkeypatch.setattr(native_module.subprocess, "run", run)
    config = NativeCampaignConfig(
        project_root=root,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )
    target = native_target("http1-parser")
    executable = build_native_target(target, config)

    (root / relative_path).write_text("changed")

    with pytest.raises(ValueError, match="current native fuzz sources"):
        native_module._validate_build(executable, target, config)


@pytest.mark.parametrize("changed_identity", ["python", "compiler"])
def test_reused_build_refuses_changed_build_environment(
    tmp_path: Path, monkeypatch, changed_identity: str
) -> None:
    root = tmp_path / "repository"
    (root / "src/wreath").mkdir(parents=True)
    (root / "src/wreath/__init__.py").write_text("")
    (root / "tools/sanitizers").mkdir(parents=True)
    (root / "tools/sanitizers/setup_core.py").write_text("")
    harness_dir = root / "tools/fuzz_native"
    harness_dir.mkdir(parents=True)
    (harness_dir / "http1_harness.c").write_text("")
    (harness_dir / "harness.c").write_text("")
    (harness_dir / "harness.h").write_text("")

    def run(command, **kwargs):
        if "build_ext" in command:
            extension = tmp_path / "build/targets/http1-parser/lib/wreath/_native/_core.so"
            extension.parent.mkdir(parents=True, exist_ok=True)
            extension.write_bytes(b"__asan_init __ubsan_handle __sanitizer_cov")
        elif command[-1] != "--version":
            executable = Path(command[command.index("-o") + 1])
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"binary __asan_init __ubsan_handle __sanitizer_cov")
        return subprocess.CompletedProcess(command, 0, "clang version 20\n", "")

    monkeypatch.setattr(native_module, "_compiler", lambda: "/usr/bin/clang")
    monkeypatch.setattr(native_module.subprocess, "run", run)
    config = NativeCampaignConfig(
        project_root=root,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )
    target = native_target("http1-parser")
    executable = build_native_target(target, config)
    if changed_identity == "python":
        current = native_module._python_build_identity()
        monkeypatch.setattr(
            native_module,
            "_python_build_identity",
            lambda: {**current, "cache_tag": "different"},
        )
    else:
        monkeypatch.setattr(
            native_module,
            "_compiler_build_identity",
            lambda compiler, timeout: {
                "path": compiler,
                "version": "clang version 21",
            },
        )

    with pytest.raises(ValueError, match="current Python ABI, platform, and compiler"):
        native_module._validate_build(executable, target, config)


def test_native_finding_deduplicates_volatile_evidence_for_the_same_failure(
    tmp_path: Path,
) -> None:
    target = native_target("http1-parser")
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )
    first = native_module._save_native_finding(
        target,
        config,
        b"same input",
        b"original input",
        b"ERROR: AddressSanitizer: heap-buffer-overflow\naddress 0x1",
        "crash-old-hash",
        ["fuzzer"],
        True,
        "a" * 64,
    )
    second = native_module._save_native_finding(
        target,
        config,
        b"same input",
        b"different generated original",
        b"ERROR: AddressSanitizer: heap-buffer-overflow\naddress 0x2",
        "crash-new-hash",
        ["fuzzer", "different campaign"],
        True,
        "a" * 64,
    )

    assert second == first
    assert first.name == hashlib.sha256(b"same input").hexdigest()


@pytest.mark.parametrize(
    ("diagnostic", "deterministic", "build_identity"),
    [
        (b"ERROR: AddressSanitizer: stack-buffer-overflow\n", True, "a" * 64),
        (b"ERROR: AddressSanitizer: heap-buffer-overflow\n", False, "a" * 64),
        (b"ERROR: AddressSanitizer: heap-buffer-overflow\n", True, "b" * 64),
    ],
)
def test_native_finding_retains_distinct_failure_or_build_evidence(
    tmp_path: Path, diagnostic: bytes, deterministic: bool, build_identity: str
) -> None:
    target = native_target("http1-parser")
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )
    first = native_module._save_native_finding(
        target,
        config,
        b"same input",
        b"original input",
        b"ERROR: AddressSanitizer: heap-buffer-overflow\n",
        "crash-first",
        ["fuzzer"],
        True,
        "a" * 64,
    )
    second = native_module._save_native_finding(
        target,
        config,
        b"same input",
        b"original input",
        diagnostic,
        "crash-second",
        ["fuzzer"],
        deterministic,
        build_identity,
    )

    assert second != first
    assert second.name == first.name


def test_h2_build_instruments_core_and_server_extensions(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repository"
    (root / "src/wreath").mkdir(parents=True)
    (root / "src/wreath/__init__.py").write_text("")
    sanitizers = root / "tools/sanitizers"
    sanitizers.mkdir(parents=True)
    (sanitizers / "setup_core.py").write_text("")
    (sanitizers / "setup_server.py").write_text("")
    harness = root / "tools/fuzz_native/h2_harness.c"
    harness.parent.mkdir(parents=True)
    harness.write_text("int LLVMFuzzerTestOneInput(void) { return 0; }")
    (harness.parent / "harness.c").write_text("")
    (harness.parent / "harness.h").write_text("")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "clang version 20\n", "")
        if "build_ext" in command:
            stem = "_server" if command[1].endswith("setup_server.py") else "_core"
            extension = tmp_path / f"build/targets/h2-frames/lib/wreath/_native/{stem}.so"
            extension.parent.mkdir(parents=True, exist_ok=True)
            extension.write_bytes(b"__asan_init __ubsan_handle __sanitizer_cov")
        else:
            executable = Path(command[command.index("-o") + 1])
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"binary __asan_init __ubsan_handle __sanitizer_cov")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("wreath._fuzz.native._compiler", lambda: "/usr/bin/clang")
    monkeypatch.setattr("wreath._fuzz.native.subprocess.run", run)
    config = NativeCampaignConfig(
        project_root=root,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )

    executable = build_native_target(native_target("h2-frames"), config)

    build_scripts = [Path(call[0][1]).name for call in calls if "build_ext" in call[0]]
    assert build_scripts == ["setup_core.py", "setup_server.py"]
    manifest = json.loads(executable.with_suffix(".build.json").read_text())
    assert manifest["extension_globs"] == ["_core*.so", "_server*.so"]
    assert {Path(extension["path"]).stem for extension in manifest["extensions"]} == {
        "_core",
        "_server",
    }


def test_run_reuses_wreath_corpus_and_imports_libfuzzer_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    harness = root / "tools/fuzz_native/http1_harness.c"
    harness.parent.mkdir(parents=True)
    harness.write_text("")
    (harness.parent / "harness.c").write_text("")
    (harness.parent / "harness.h").write_text("")
    (root / "src/wreath").mkdir(parents=True)
    (root / "src/wreath/__init__.py").write_text("")
    (root / "tools/sanitizers").mkdir(parents=True)
    (root / "tools/sanitizers/setup_core.py").write_text("")
    executable = tmp_path / "build/bin/http1-parser"
    executable.parent.mkdir(parents=True)
    executable_bytes = b"binary __asan_init __ubsan_handle __sanitizer_cov"
    executable.write_bytes(executable_bytes)
    executable.chmod(0o755)
    extension = tmp_path / "build/targets/http1-parser/lib/wreath/_native/_core.so"
    extension.parent.mkdir(parents=True)
    extension_bytes = b"extension __asan_init __ubsan_handle __sanitizer_cov"
    extension.write_bytes(extension_bytes)
    compiler_identity = {"path": "/usr/bin/clang", "version": "clang version 20"}
    manifest = {
        "schema": 4,
        "target": "http1-parser",
        "compiler": compiler_identity,
        "python": native_module._python_build_identity(),
        "executable_sha256": hashlib.sha256(executable_bytes).hexdigest(),
        "extensions": [
            {
                "path": str(extension),
                "sha256": hashlib.sha256(extension_bytes).hexdigest(),
            }
        ],
        "extension_globs": ["_core*.so"],
        "sanitizers": ["address", "undefined"],
        "sanitizer_coverage": "trace-cmp,trace-div,indirect-calls",
        "source_sha256": native_module._source_digest(native_target("http1-parser"), root),
    }
    manifest["build_identity"] = native_module._identity_digest(manifest)
    executable.with_suffix(".build.json").write_text(json.dumps(manifest))
    config = NativeCampaignConfig(
        project_root=root,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
        max_seconds=3,
        max_runs=20,
        max_input_size=512,
        seed=41,
        rebuild=False,
    )
    crash = b"GET /bad HTTP/1.1\r\n\r\n"
    minimized = b"BAD"
    discovered = b"GET /new HTTP/1.1\r\nHost: x\r\n\r\n"

    timeouts = []

    def run(command, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        if "-minimize_crash=1" in command:
            output = next(value for value in command if value.startswith("-exact_artifact_path="))
            Path(output.removeprefix("-exact_artifact_path=")).write_bytes(minimized)
            return subprocess.CompletedProcess(command, 0, "minimized\n", "")
        if "-runs=1" in command:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "ERROR: AddressSanitizer: heap-buffer-overflow\n",
            )
        (Path(command[1]) / "libfuzzer-sha1-name").write_bytes(discovered)
        prefix = next(value for value in command if value.startswith("-artifact_prefix="))
        pending = Path(prefix.removeprefix("-artifact_prefix="))
        pending.mkdir(parents=True, exist_ok=True)
        (pending / "crash-deadbeef").write_bytes(crash)
        return subprocess.CompletedProcess(
            command,
            1,
            "#20 DONE cov: 12 ft: 34\n",
            "ERROR: AddressSanitizer: heap-buffer-overflow\n",
        )

    monkeypatch.setattr("wreath._fuzz.native.subprocess.run", run)
    monkeypatch.setattr(
        native_module,
        "_compiler_build_identity",
        lambda compiler, timeout: compiler_identity,
    )

    report = run_native_campaign(native_target("http1-parser"), config)

    digest = hashlib.sha256(minimized).hexdigest()
    corpus = config.corpus_root / "http1-parser"
    assert any(path.suffix == ".input" for path in corpus.iterdir())
    assert all(path.suffix == ".input" and len(path.stem) == 64 for path in corpus.iterdir())
    assert (corpus / f"{hashlib.sha256(discovered).hexdigest()}.input").read_bytes() == discovered
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.name == digest
    assert len(finding.parent.name) == 64
    assert len(finding.parent.parent.name) == 64
    assert (finding / "input").read_bytes() == minimized
    metadata = json.loads((finding / "metadata.json").read_text())
    assert metadata["input_sha256"] == digest
    assert metadata["backend"] == "libfuzzer"
    assert metadata["build_identity"] == finding.parent.parent.name
    assert metadata["failure_identity"] == finding.parent.name
    assert metadata["failure_signature"] == ["address", "heap-buffer-overflow"]
    assert metadata["sanitizers"] == ["address", "undefined"]
    assert metadata["original_sha256"] == hashlib.sha256(crash).hexdigest()
    assert metadata["original_size"] == len(crash)
    assert metadata["minimized_size"] == len(minimized)
    assert metadata["deterministic"] is True
    diagnostic = (finding / "diagnostic.log").read_text()
    assert "ERROR: AddressSanitizer: heap-buffer-overflow\n" in diagnostic
    assert "minimized\n" in diagnostic
    assert report.exit_code == 1
    assert report.seed == 41
    assert report.findings == (finding,)
    assert report.cases_executed == 20
    assert report.coverage_features == 12
    assert report.fuzzer_features == 34
    assert "-max_total_time=3" in report.command
    assert "-runs=20" in report.command
    assert "-timeout=2" in report.command
    from wreath._fuzz_targets import by_name

    expected_additions = tuple(
        sorted(
            hashlib.sha256(value).hexdigest()
            for value in (*by_name("http1-parser").seeds, discovered)
        )
    )
    assert report.corpus_added == len(expected_additions)
    assert report.corpus_size == len(report.corpus_digests)
    assert report.corpus_addition_digests == expected_additions
    assert all(timeout is not None for timeout in timeouts)


def test_minimization_rejects_an_incompatible_reproduction(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "crash"
    artifact.write_bytes(b"ORIGINAL")
    executable = tmp_path / "fuzzer"
    executable.write_bytes(b"")
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )

    def run(command, **kwargs):
        if "-minimize_crash=1" in command:
            output = next(value for value in command if value.startswith("-exact_artifact_path="))
            Path(output.removeprefix("-exact_artifact_path=")).write_bytes(b"MIN")
            return subprocess.CompletedProcess(command, 0, "", "")
        data = Path(command[-1]).read_bytes()
        kind = "heap-buffer-overflow" if data == b"ORIGINAL" else "stack-buffer-overflow"
        return subprocess.CompletedProcess(command, 1, "", f"ERROR: AddressSanitizer: {kind}\n")

    monkeypatch.setattr(native_module.subprocess, "run", run)

    minimized, diagnostic, deterministic = native_module._minimize_finding(
        executable,
        config,
        {},
        artifact,
        b"ERROR: AddressSanitizer: heap-buffer-overflow\n",
    )

    assert minimized == b"ORIGINAL"
    assert deterministic is True
    assert b"incompatible minimized reproduction" in diagnostic


def test_minimization_replays_and_keeps_a_stable_harness_abort(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "crash"
    artifact.write_bytes(b"ORIGINAL")
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )
    replays = 0

    def run(command, **kwargs):
        nonlocal replays
        if "-minimize_crash=1" in command:
            output = next(value for value in command if value.startswith("-exact_artifact_path="))
            Path(output.removeprefix("-exact_artifact_path=")).write_bytes(b"MIN")
            return subprocess.CompletedProcess(command, 0, "", "")
        replays += 1
        return subprocess.CompletedProcess(command, 1, "", "ERROR: libFuzzer: deadly signal\n")

    monkeypatch.setattr(native_module.subprocess, "run", run)

    minimized, _, deterministic = native_module._minimize_finding(
        tmp_path / "fuzzer",
        config,
        {},
        artifact,
        b"ERROR: libFuzzer: deadly signal\n",
    )

    assert minimized == b"MIN"
    assert deterministic is True
    assert replays == 3


def test_minimization_rejects_a_candidate_that_does_not_repeat(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "crash"
    artifact.write_bytes(b"ORIGINAL")
    executable = tmp_path / "fuzzer"
    executable.write_bytes(b"")
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )
    minimized_replays = 0

    def run(command, **kwargs):
        nonlocal minimized_replays
        if "-minimize_crash=1" in command:
            output = next(value for value in command if value.startswith("-exact_artifact_path="))
            Path(output.removeprefix("-exact_artifact_path=")).write_bytes(b"MIN")
            return subprocess.CompletedProcess(command, 0, "", "")
        if Path(command[-1]).read_bytes() == b"MIN":
            minimized_replays += 1
            if minimized_replays == 2:
                return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(
            command, 1, "", "ERROR: AddressSanitizer: heap-buffer-overflow\n"
        )

    monkeypatch.setattr(native_module.subprocess, "run", run)

    minimized, diagnostic, deterministic = native_module._minimize_finding(
        executable,
        config,
        {},
        artifact,
        b"ERROR: AddressSanitizer: heap-buffer-overflow\n",
    )

    assert minimized == b"ORIGINAL"
    assert deterministic is True
    assert minimized_replays == 2
    assert b"did not reproduce repeatedly" in diagnostic


def test_replay_timeout_keeps_the_original_as_nondeterministic(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "crash"
    artifact.write_bytes(b"ORIGINAL")
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], "", "blocked")

    monkeypatch.setattr(native_module.subprocess, "run", run)

    minimized, diagnostic, deterministic = native_module._minimize_finding(
        tmp_path / "fuzzer",
        config,
        {},
        artifact,
        b"ERROR: AddressSanitizer: heap-buffer-overflow\n",
    )

    assert minimized == b"ORIGINAL"
    assert deterministic is False
    assert b"Python wall-clock timeout" in diagnostic
    assert b"did not reproduce compatibly" in diagnostic


def test_minimization_timeout_keeps_the_reproducing_original(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "crash"
    artifact.write_bytes(b"ORIGINAL")
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
    )

    def run(command, **kwargs):
        if "-runs=1" in command:
            return subprocess.CompletedProcess(
                command, 1, "", "ERROR: AddressSanitizer: heap-buffer-overflow\n"
            )
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], "", "blocked")

    monkeypatch.setattr(native_module.subprocess, "run", run)

    minimized, diagnostic, deterministic = native_module._minimize_finding(
        tmp_path / "fuzzer",
        config,
        {},
        artifact,
        b"ERROR: AddressSanitizer: heap-buffer-overflow\n",
    )

    assert minimized == b"ORIGINAL"
    assert deterministic is True
    assert b"Python wall-clock timeout" in diagnostic


def test_campaign_timeout_retains_available_crash_artifact(tmp_path: Path, monkeypatch) -> None:
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
        rebuild=False,
        max_seconds=1,
    )
    target = native_target("http1-parser")
    executable = config.build_root / "bin/http1-parser"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    corpus = config.corpus_root / target.name
    corpus.mkdir(parents=True)
    dictionary = tmp_path / "dictionary"
    dictionary.write_text("")
    monkeypatch.setattr(native_module, "_validate_build", lambda *args: "a" * 64)
    monkeypatch.setattr(native_module, "_stage_inputs", lambda *args: (corpus, dictionary, ()))

    def run(command, **kwargs):
        if "-minimize_crash=1" in command:
            return subprocess.CompletedProcess(command, 1, "", "minimize failed")
        if "-runs=1" in command:
            return subprocess.CompletedProcess(
                command, 1, "", "ERROR: AddressSanitizer: heap-buffer-overflow\n"
            )
        prefix = next(value for value in command if value.startswith("-artifact_prefix="))
        pending = Path(prefix.removeprefix("-artifact_prefix="))
        (pending / "timeout-deadbeef").write_bytes(b"CRASH")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], "partial", "timed out")

    monkeypatch.setattr(native_module.subprocess, "run", run)

    report = run_native_campaign(target, config)

    assert report.exit_code == 124
    assert len(report.findings) == 1
    assert "Python wall-clock timeout" in report.stderr


def test_native_merge_keeps_only_libfuzzer_coverage_corpus(tmp_path: Path, monkeypatch) -> None:
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
        rebuild=False,
    )
    target = native_target("http1-parser")
    executable = config.build_root / "bin/http1-parser"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    corpus = config.corpus_root / target.name
    corpus.mkdir(parents=True)
    first = b"GET / HTTP/1.1\r\n\r\n"
    second = b"POST / HTTP/1.1\r\n\r\n"
    for value in (first, second):
        (corpus / f"{hashlib.sha256(value).hexdigest()}.input").write_bytes(value)
    dictionary = tmp_path / "dictionary"
    dictionary.write_text("")
    monkeypatch.setattr(native_module, "_validate_build", lambda *args: None)
    monkeypatch.setattr(
        native_module,
        "_stage_inputs",
        lambda target, config: (
            corpus,
            dictionary,
            tuple(sorted(path.stem for path in corpus.iterdir())),
        ),
    )

    def run(command, **kwargs):
        assert "-merge=1" in command
        assert "-timeout=2" in command
        assert kwargs["timeout"] > 0
        output = Path(command[command.index("-merge=1") + 1])
        (output / "libfuzzer-name").write_bytes(second)
        return subprocess.CompletedProcess(command, 0, "MERGE-OUTER: 2 files\n", "")

    monkeypatch.setattr(native_module.subprocess, "run", run)

    report = merge_native_corpus(target, config)

    assert report.before == 2
    assert report.after == 1
    assert tuple(path.read_bytes() for path in corpus.iterdir()) == (second,)


def test_native_merge_reports_wall_clock_timeout(tmp_path: Path, monkeypatch) -> None:
    config = NativeCampaignConfig(
        project_root=tmp_path,
        build_root=tmp_path / "build",
        corpus_root=tmp_path / "corpus",
        artifact_root=tmp_path / "artifacts",
        rebuild=False,
        max_seconds=1,
    )
    target = native_target("http1-parser")
    executable = config.build_root / "bin/http1-parser"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    corpus = config.corpus_root / target.name
    corpus.mkdir(parents=True)
    (corpus / f"{hashlib.sha256(b'a').hexdigest()}.input").write_bytes(b"a")
    dictionary = tmp_path / "dictionary"
    dictionary.write_text("")
    monkeypatch.setattr(native_module, "_validate_build", lambda *args: None)
    monkeypatch.setattr(
        native_module,
        "_stage_inputs",
        lambda *args: (
            corpus,
            dictionary,
            tuple(sorted(path.stem for path in corpus.iterdir())),
        ),
    )
    monkeypatch.setattr(
        native_module.subprocess,
        "run",
        lambda command, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(command, kwargs["timeout"], "partial", "blocked")
        ),
    )

    with pytest.raises(RuntimeError, match="native corpus merge exceeded"):
        merge_native_corpus(target, config)
