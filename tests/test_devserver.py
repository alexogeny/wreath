from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from wreath._cli import CliError, build_parser, options_from_namespace
from wreath._devserver import ChangeDetector, snapshot_files, worker_argv


def test_dev_parser_has_reload_defaults() -> None:
    options = options_from_namespace(build_parser().parse_args(["dev", "example:app"]))

    assert options.command == "dev"
    assert options.reload_dirs == ()
    assert options.reload_includes == ("*.py",)
    assert options.reload_excludes == ()
    assert options.reload_delay == 0.25
    assert options.reload_debounce == 0.10


def test_dev_rejects_ephemeral_port() -> None:
    namespace = build_parser().parse_args(["dev", "example:app", "--port", "0"])

    with pytest.raises(CliError, match="port 0"):
        options_from_namespace(namespace)


def test_snapshot_detects_create_change_and_delete(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    detector = ChangeDetector((tmp_path,), ("*.py",), ())

    assert detector.poll() is False
    source.write_text("value = 200\n")
    assert detector.poll() is True
    assert detector.poll() is False
    extra = tmp_path / "routes.py"
    extra.write_text("route = True\n")
    assert detector.poll() is True
    source.unlink()
    assert detector.poll() is True


def test_snapshot_excludes_artifacts_hidden_dirs_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("app = 1\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "cached.py").write_text("ignored = 1\n")
    hidden = tmp_path / ".git"
    hidden.mkdir()
    (hidden / "hook.py").write_text("ignored = 1\n")
    build = tmp_path / "build"
    build.mkdir()
    (build / "generated.py").write_text("ignored = 1\n")
    text = tmp_path / "notes.txt"
    text.write_text("ignored\n")
    if hasattr(os, "symlink"):
        os.symlink(tmp_path / "app.py", tmp_path / "linked.py")

    snapshot = snapshot_files((tmp_path,), ("*.py",), ())

    assert tuple(Path(path).name for path in snapshot) == ("app.py",)


def test_snapshot_honors_additional_include_and_exclude_patterns(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("app = 1\n")
    (tmp_path / "settings.toml").write_text("debug = true\n")
    (tmp_path / "secret.toml").write_text("token = 'x'\n")

    snapshot = snapshot_files(
        (tmp_path,), ("*.py", "*.toml"), ("secret.*",)
    )

    assert {Path(path).name for path in snapshot} == {"app.py", "settings.toml"}


def test_worker_argv_round_trips_server_configuration(tmp_path: Path) -> None:
    options = options_from_namespace(
        build_parser().parse_args(
            [
                "dev",
                "example:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                "8123",
                "--protocol",
                "http/1.1",
                "--protocol",
                "h2",
                "--max-body-bytes",
                "4321",
                "--max-body-chunks",
                "123",
                "--response-high-water",
                "8192",
                "--response-low-water",
                "4096",
                "--response-high-water-segments",
                "32",
                "--response-low-water-segments",
                "16",
                "--loop",
                "asyncio",
                "--reload-dir",
                str(tmp_path),
            ]
        )
    )

    argv = worker_argv(options)
    parsed = options_from_namespace(build_parser().parse_args(list(argv[3:])))

    assert argv[:3] == (os.fspath(Path(sys.executable)), "-m", "wreath")
    assert parsed.command == "run"
    assert parsed.target == options.target
    assert parsed.factory is True
    assert parsed.host == options.host
    assert parsed.port == options.port
    assert parsed.protocols == options.protocols
    assert parsed.max_body_bytes == options.max_body_bytes
    assert parsed.max_body_chunks == options.max_body_chunks
    assert parsed.response_high_water == options.response_high_water
    assert parsed.response_low_water == options.response_low_water
    assert parsed.response_high_water_segments == options.response_high_water_segments
    assert parsed.response_low_water_segments == options.response_low_water_segments
    assert "--reload-dir" not in argv


@pytest.mark.parametrize(("tls_key", "expected"), [(None, ""), ("private.pem", "private.pem")])
def test_worker_argv_serializes_the_tls_key(
    tls_key: str | None, expected: str
) -> None:
    options = options_from_namespace(
        build_parser().parse_args(["dev", "example:app"])
    )
    options = replace(options, tls_cert="certificate.pem", tls_key=tls_key)

    argv = worker_argv(options)

    key = argv.index("--tls-key")
    assert argv[key + 1] == expected

def test_supervisor_gracefully_replaces_one_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = options_from_namespace(
        build_parser().parse_args(
            ["dev", "example:app", "--reload-dir", str(tmp_path), "--reload-debounce", "0"]
        )
    )

    class FakeChild:
        def __init__(self) -> None:
            self.running = True
            self.terminated = 0
            self.killed = 0

        def poll(self) -> int | None:
            return None if self.running else 0

        def terminate(self) -> None:
            self.terminated += 1
            self.running = False

        def kill(self) -> None:
            self.killed += 1
            self.running = False

        def wait(self, timeout: float | None = None) -> int:
            return 0

    children: list[FakeChild] = []

    def fake_popen(argv: object) -> FakeChild:
        child = FakeChild()
        children.append(child)
        return child

    sleeps = 0

    def fake_sleep(delay: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("wreath._devserver.subprocess.Popen", fake_popen)
    monkeypatch.setattr("wreath._devserver.ChangeDetector.poll", lambda self: True)
    monkeypatch.setattr("wreath._devserver.time.sleep", fake_sleep)

    from wreath._devserver import supervise

    supervise(options)

    assert len(children) == 2
    assert [child.terminated for child in children] == [1, 1]
    assert [child.killed for child in children] == [0, 0]
