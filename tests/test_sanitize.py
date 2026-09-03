from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from wreath._devtools import sanitize


def test_sanitizer_finding_preserves_reproduction_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    library = root / ".sanitizers" / "native-core" / "lib"
    library.mkdir(parents=True)
    (library / "_core.so").write_bytes(b"libasan.so.8\0libubsan.so.1\0")
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(sanitize, "_asan_runtime", lambda: "/runtime/libasan.so")

    def run(command, **kwargs):
        assert kwargs["env"]["PYTHONPATH"] == str(library)
        return subprocess.CompletedProcess(
            command,
            1,
            f"WREATH_SANITIZER_EXTENSION={library / '_core.so'}\n"
            "================ 1 failed in 0.01s ================\n",
            "ERROR: AddressSanitizer: heap-buffer-overflow\n",
        )

    monkeypatch.setattr(sanitize.subprocess, "run", run)
    target = sanitize.Target("core", ("tests/test_native.py",), "native core")

    outcome = sanitize.run_target(
        root,
        target,
        target.tests,
        leaks=False,
        rebuild=False,
        artifact_root=artifact_root,
    )

    bundle = Path(outcome.finding_bundle)
    assert bundle.parent.parent == artifact_root
    assert (bundle / "stdout.txt").read_text() == (
        f"WREATH_SANITIZER_EXTENSION={library / '_core.so'}\n"
        "================ 1 failed in 0.01s ================\n"
    )
    assert (bundle / "stderr.txt").read_text() == (
        "ERROR: AddressSanitizer: heap-buffer-overflow\n"
    )
    metadata = json.loads((bundle / "metadata.json").read_text())
    assert metadata["target"] == "core"
    assert metadata["instrumented"] is True
    assert metadata["instrumented_extensions"] == [str(library / "_core.so")]
    assert metadata["imported_extension"] == str((library / "_core.so").resolve())
    assert metadata["command"][-4:] == [
        "tests/test_native.py",
        "--tb=no",
        "-p",
        "no:randomly",
    ]
    assert metadata["exit_code"] == 1
    assert metadata["reproduction"]["environment"]["LD_PRELOAD"] == (
        "/runtime/libasan.so"
    )


def test_clean_sanitizer_run_does_not_create_artifact_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    library = root / ".sanitizers" / "native-core" / "lib"
    library.mkdir(parents=True)
    (library / "_core.so").write_bytes(b"libasan.so.8\0libubsan.so.1\0")
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(sanitize, "_asan_runtime", lambda: "/runtime/libasan.so")
    monkeypatch.setattr(
        sanitize.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            f"WREATH_SANITIZER_EXTENSION={library / '_core.so'}\n"
            "================ 1 passed in 0.01s ================\n",
            "",
        ),
    )
    target = sanitize.Target("core", ("tests/test_native.py",), "native core")

    outcome = sanitize.run_target(
        root,
        target,
        target.tests,
        leaks=False,
        rebuild=False,
        artifact_root=artifact_root,
    )

    assert outcome.finding_bundle == ""
    assert not artifact_root.exists()


def test_uninstrumented_reused_build_is_refused(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repository"
    library = root / ".sanitizers" / "native-core" / "lib"
    library.mkdir(parents=True)
    (library / "_core.so").write_bytes(b"ordinary extension")
    monkeypatch.setattr(sanitize, "_asan_runtime", lambda: "/runtime/libasan.so")

    def run(command, **kwargs):
        raise AssertionError("an uninstrumented extension must not be executed")

    monkeypatch.setattr(sanitize.subprocess, "run", run)

    outcome = sanitize.run_target(
        root,
        sanitize.Target("core", ("tests/test_native.py",), "native core"),
        ("tests/test_native.py",),
        leaks=False,
        rebuild=False,
    )

    assert not outcome.ran
    assert outcome.instrumented_extensions == []
    assert "no ASan/UBSan-instrumented extension" in outcome.reason


def test_instrumented_sibling_does_not_validate_the_selected_target(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    library = root / ".sanitizers" / "native-core" / "lib"
    library.mkdir(parents=True)
    (library / "_server.so").write_bytes(b"libasan.so.8\0libubsan.so.1\0")
    (library / "_core.so").write_bytes(b"ordinary extension")
    monkeypatch.setattr(sanitize, "_asan_runtime", lambda: "/runtime/libasan.so")

    outcome = sanitize.run_target(
        root,
        sanitize.Target("core", ("tests/test_native.py",), "native core"),
        ("tests/test_native.py",),
        False,
        False,
    )

    assert not outcome.ran
    assert "extension for selected _core" in outcome.reason


def test_sanitizer_refuses_when_python_imports_a_different_extension(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    library = root / ".sanitizers" / "native-core" / "lib"
    library.mkdir(parents=True)
    selected = library / "_core.so"
    selected.write_bytes(b"libasan.so.8\0libubsan.so.1\0")
    imported = tmp_path / "ordinary" / "_core.so"
    monkeypatch.setattr(sanitize, "_asan_runtime", lambda: "/runtime/libasan.so")
    monkeypatch.setattr(
        sanitize.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            f"WREATH_SANITIZER_EXTENSION={imported}\n"
            "================ 1 passed in 0.01s ================\n",
            "",
        ),
    )

    outcome = sanitize.run_target(
        root,
        sanitize.Target("core", ("tests/test_native.py",), "native core"),
        ("tests/test_native.py",),
        False,
        False,
    )

    assert not outcome.ran
    assert str(imported) in outcome.reason
    assert str(selected.resolve()) in outcome.reason


def test_corrupted_existing_finding_bundle_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    library = root / ".sanitizers" / "native-core" / "lib"
    library.mkdir(parents=True)
    (library / "_core.so").write_bytes(b"libasan.so.8\0libubsan.so.1\0")
    monkeypatch.setattr(sanitize, "_asan_runtime", lambda: "/runtime/libasan.so")
    monkeypatch.setattr(
        sanitize.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            f"WREATH_SANITIZER_EXTENSION={library / '_core.so'}\n"
            "================ 1 failed in 0.01s ================\n",
            "ERROR: AddressSanitizer: heap-buffer-overflow\n",
        ),
    )
    target = sanitize.Target("core", ("tests/test_native.py",), "native core")
    artifact_root = tmp_path / "artifacts"
    first = sanitize.run_target(
        root, target, target.tests, False, False, artifact_root
    )
    (Path(first.finding_bundle) / "metadata.json").unlink()

    with pytest.raises(ValueError, match="corrupted sanitizer finding bundle"):
        sanitize.run_target(root, target, target.tests, False, False, artifact_root)


def test_interrupted_bundle_publication_leaves_no_partial_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    library = root / ".sanitizers" / "native-core" / "lib"
    library.mkdir(parents=True)
    (library / "_core.so").write_bytes(b"libasan.so.8\0libubsan.so.1\0")
    monkeypatch.setattr(sanitize, "_asan_runtime", lambda: "/runtime/libasan.so")
    monkeypatch.setattr(
        sanitize.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            f"WREATH_SANITIZER_EXTENSION={library / '_core.so'}\n"
            "================ 1 failed in 0.01s ================\n",
            "ERROR: AddressSanitizer: heap-buffer-overflow\n",
        ),
    )
    monkeypatch.setattr(
        sanitize.os,
        "rename",
        lambda source, destination: (_ for _ in ()).throw(OSError("interrupted")),
    )
    artifact_root = tmp_path / "artifacts"

    with pytest.raises(OSError, match="interrupted"):
        sanitize.run_target(
            root,
            sanitize.Target("core", ("tests/test_native.py",), "native core"),
            ("tests/test_native.py",),
            False,
            False,
            artifact_root,
        )

    target_root = artifact_root / "core"
    assert not target_root.exists() or not tuple(target_root.iterdir())


def test_concurrent_publication_reuses_the_complete_winning_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    library = root / ".sanitizers" / "native-core" / "lib"
    library.mkdir(parents=True)
    (library / "_core.so").write_bytes(b"libasan.so.8\0libubsan.so.1\0")
    monkeypatch.setattr(sanitize, "_asan_runtime", lambda: "/runtime/libasan.so")
    monkeypatch.setattr(
        sanitize.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            f"WREATH_SANITIZER_EXTENSION={library / '_core.so'}\n"
            "================ 1 failed in 0.01s ================\n",
            "ERROR: AddressSanitizer: heap-buffer-overflow\n",
        ),
    )
    rename = sanitize.os.rename

    def publish_other_process_first(source, destination):
        rename(source, destination)
        raise FileExistsError(errno.EEXIST, "another process published the bundle")

    monkeypatch.setattr(sanitize.os, "rename", publish_other_process_first)

    outcome = sanitize.run_target(
        root,
        sanitize.Target("core", ("tests/test_native.py",), "native core"),
        ("tests/test_native.py",),
        False,
        False,
        tmp_path / "artifacts",
    )

    bundle = Path(outcome.finding_bundle)
    assert bundle.is_dir()
    assert {path.name for path in bundle.iterdir()} == {
        "metadata.json",
        "stderr.txt",
        "stdout.txt",
    }
