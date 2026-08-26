from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest
from _metal import requires_metal

from wreath import cli
from wreath._cli import (
    CliError,
    _ensure_cwd_importable,
    _execute_flight_replay,
    build_parser,
    load_application,
    options_from_namespace,
)


def test_flight_replay_turns_a_replay_refusal_into_a_stable_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_module = importlib.import_module("wreath._cli")
    replay_module = importlib.import_module("wreath.replay")
    recording_module = importlib.import_module("wreath.recording")
    monkeypatch.setattr(recording_module, "read_ring_file", lambda _path: object())
    monkeypatch.setattr(cli_module, "load_application", lambda *_a, **_k: object())
    monkeypatch.setattr(replay_module, "open_recording", lambda _path: object())

    async def refuse(*_args: object, **_kwargs: object) -> object:
        raise replay_module.ReplayError("no request was in flight")

    monkeypatch.setattr(replay_module, "reproduce_from_ring", refuse)
    namespace = type(
        "Namespace",
        (),
        {
            "path": "crash.ring",
            "target": "example:app",
            "factory": False,
            "recording": "request.wrr",
            "request_id": None,
            "as_json": False,
        },
    )()

    with pytest.raises(CliError, match="no request was in flight") as caught:
        _execute_flight_replay(namespace)

    assert caught.value.exit_code == 2


def _write_app(tmp_path: Path, body: str) -> str:
    for name in tuple(sys.modules):
        if name == "sample_cli_app" or name.startswith("sample_cli_app."):
            sys.modules.pop(name, None)
    package = tmp_path / "sample_cli_app"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text(body)
    return "sample_cli_app.main"


def test_run_parser_defaults_are_safe_and_deterministic() -> None:
    namespace = build_parser().parse_args(["run", "example:app"])
    options = options_from_namespace(namespace)

    assert options.command == "run"
    assert options.target == "example:app"
    assert options.host == "127.0.0.1"
    assert options.port == 8000
    assert options.protocols == ("http/1.1",)
    assert options.loop == "asyncio"
    assert options.workers == 1
    assert options.lifespan == "auto"
    assert options.factory is False
    assert options.server_header == "wreath"
    assert options.date_header is True


def test_metal_worker_affinity_is_default_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_module = importlib.import_module("wreath._cli")

    monkeypatch.delenv("WREATH_METAL_AFFINITY", raising=False)
    monkeypatch.setattr(cli_module.os, "sched_getaffinity", lambda _pid: {2, 6})
    applied: list[tuple[int, set[int]]] = []
    monkeypatch.setattr(
        cli_module.os,
        "sched_setaffinity",
        lambda pid, cpus: applied.append((pid, cpus)),
    )
    assert cli_module._apply_metal_worker_affinity(3) == 6
    assert applied == [(0, {6})]

    monkeypatch.setenv("WREATH_METAL_AFFINITY", "sometimes")
    with pytest.raises(ValueError, match="must be 'auto' or 'off'"):
        cli_module._apply_metal_worker_affinity(0)


def test_multiple_workers_are_explicitly_metal_only() -> None:
    namespace = build_parser().parse_args(
        ["run", "example:app", "--loop", "metal", "--workers", "3"]
    )
    options = options_from_namespace(namespace)
    assert options.workers == 3
    assert options.loop == "metal"

    namespace = build_parser().parse_args(
        ["run", "example:app", "--loop", "asyncio", "--workers", "2"]
    )
    with pytest.raises(CliError, match="requires --loop metal"):
        options_from_namespace(namespace)

    namespace = build_parser().parse_args(
        ["run", "example:app", "--loop", "metal", "--workers", "0"]
    )
    with pytest.raises(CliError, match="at least 1"):
        options_from_namespace(namespace)


def test_one_worker_keeps_single_process_options_available() -> None:
    """Each multi-worker guard must retain its worker-count operand.

    Exercising only invalid multi-worker cases cannot detect mutations that
    make their loop and fixed-port restrictions apply to a single worker.
    """
    ordinary = build_parser().parse_args(
        ["run", "example:app", "--loop", "asyncio", "--workers", "1"]
    )
    ephemeral = build_parser().parse_args(
        [
            "run",
            "example:app",
            "--loop",
            "asyncio",
            "--workers",
            "1",
            "--port",
            "0",
        ]
    )

    ordinary_options = options_from_namespace(ordinary)
    ephemeral_options = options_from_namespace(ephemeral)

    assert ordinary_options.workers == 1
    assert ordinary_options.loop == "asyncio"
    assert ephemeral_options.workers == 1
    assert ephemeral_options.port == 0


def test_run_parser_configures_default_response_headers() -> None:
    namespace = build_parser().parse_args(
        ["run", "example:app", "--server-header", "example", "--no-date-header"]
    )
    options = options_from_namespace(namespace)
    assert options.server_header == "example"
    assert options.date_header is False

    namespace = build_parser().parse_args(
        ["run", "example:app", "--no-server-header"]
    )
    assert options_from_namespace(namespace).server_header is None


def test_run_parser_preserves_explicit_protocol_order() -> None:
    namespace = build_parser().parse_args(
        ["run", "example:app", "--protocol", "http/1.1", "--protocol", "h2"]
    )

    assert options_from_namespace(namespace).protocols == ("http/1.1", "h2")


def test_load_application_uses_app_as_the_default_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = _write_app(
        tmp_path,
        "async def app(scope, receive, send):\n    pass\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    loaded = load_application(module_name)

    assert loaded is importlib.import_module(module_name).app


def test_load_application_invokes_an_explicit_factory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = _write_app(
        tmp_path,
        "calls = 0\n"
        "async def application(scope, receive, send):\n    pass\n"
        "def create_app():\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return application\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    loaded = load_application(f"{module_name}:create_app", factory=True)
    module = importlib.import_module(module_name)

    assert loaded is module.application
    assert module.calls == 1


def test_load_application_rejects_malformed_and_non_callable_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = _write_app(tmp_path, "app = 42\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(CliError, match="module:attribute"):
        load_application("bad:target:extra")
    with pytest.raises(CliError, match="module:attribute"):
        load_application(":app")
    with pytest.raises(CliError, match="not callable"):
        load_application(f"{module_name}:app")


def test_options_build_server_and_tls_configuration(tmp_path: Path) -> None:
    password_file = tmp_path / "tls-password"
    password_file.write_text("secret\n")
    namespace = build_parser().parse_args(
        [
            "run",
            "example:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8443",
            "--protocol",
            "http/1.1",
            "--protocol",
            "h2",
            "--lifespan",
            "on",
            "--max-body-bytes",
            "1234",
            "--max-body-chunks",
            "321",
            "--tls-cert",
            "cert.pem",
            "--tls-key",
            "key.pem",
            "--tls-password-file",
            str(password_file),
        ]
    )

    options = options_from_namespace(namespace)
    config = options.server_config()
    tls = options.tls_config()

    assert config.host == "0.0.0.0"
    assert config.port == 8443
    assert config.protocols == ("http/1.1", "h2")
    assert config.lifespan == "on"
    assert config.max_body_bytes == 1234
    assert config.max_body_chunks == 321
    assert tls is not None
    assert tls.certfile == "cert.pem"
    assert tls.keyfile == "key.pem"
    assert tls.password == "secret"


def test_tls_certificate_and_key_must_be_supplied_together(capsys: Any) -> None:
    result = cli.main(["run", "example:app", "--tls-cert", "cert.pem"])

    assert result == 2
    assert "--tls-cert and --tls-key must be supplied together" in capsys.readouterr().err


def test_main_loads_the_target_and_delegates_to_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = _write_app(
        tmp_path,
        "async def app(scope, receive, send):\n    pass\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    calls: list[tuple[Any, Any, Any, Any]] = []

    def fake_run(
        app: Any, config: Any, *, tls: Any, loop_factory: Any, announce: Any = None
    ) -> None:
        calls.append((app, config, tls, loop_factory))

    monkeypatch.setattr("wreath._cli.run_server", fake_run)

    assert cli.main(["run", f"{module_name}:app", "--port", "8123"]) == 0
    assert len(calls) == 1
    assert calls[0][1].port == 8123
    assert calls[0][2] is None
    assert calls[0][3] is None


def test_main_routes_multiple_metal_workers_to_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = _write_app(
        tmp_path,
        "async def app(scope, receive, send):\n    pass\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    calls: list[int] = []

    def fake_group(app: Any, config: Any, *, tls: Any, workers: int, target: str) -> None:
        del app, config, tls, target
        calls.append(workers)

    monkeypatch.setattr("wreath._cli._run_metal_worker_group", fake_group)
    assert cli.main([
        "run", f"{module_name}:app", "--loop", "metal", "--workers", "2"
    ]) == 0
    assert calls == [2]


def test_main_reports_target_import_errors_without_a_traceback(capsys: Any) -> None:
    result = cli.main(["run", "missing_wreath_application:app"])

    assert result == 1
    error = capsys.readouterr().err
    assert "could not import application module" in error
    assert "Traceback" not in error


def test_help_does_not_import_the_application(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    original = importlib.import_module

    def recording_import(name: str, package: str | None = None) -> Any:
        imported.append(name)
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", recording_import)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--help"])

    assert stopped.value.code == 0
    assert "example" not in imported


def test_version_is_available_without_an_application(capsys: Any) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--version"])

    assert stopped.value.code == 0
    assert "wreath" in capsys.readouterr().out.lower()


QUICKSTART_APP = """\
from wreath import Request, Wreath

app = Wreath()


@app.get("/hello/{name}")
async def hello(request: Request, name: str) -> dict:
    return {"hello": name}
"""


def _console_script() -> Path:
    script = Path(sys.executable).parent / "wreath"
    if not script.exists():
        pytest.skip("the wreath console script is not installed in this environment")
    return script


def _read_line(stream: Any, timeout: float) -> str:
    """One line, or "" if the process stayed silent for `timeout` seconds."""
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    return box[0] if box else ""


# Only the `metal` case needs the reactor; the `asyncio` case is the one that
# proves the console script works at all, and it runs everywhere.
@pytest.mark.parametrize(
    "loop", ["asyncio", pytest.param("metal", marks=requires_metal)]
)
def test_the_console_script_serves_an_app_from_the_working_directory(
    tmp_path: Path, loop: str
) -> None:
    """`wreath run app:app` works where the README says it does.

    Deliberately a subprocess driving the installed console script rather than
    an in-process call: the defect this covers is that a console script's
    `sys.path[0]` is the virtualenv's `bin`, not the working directory, and
    every in-process test already runs with the repository importable. Running
    `python -m wreath` would pass this test with the fix reverted, so it must
    not be used here, and `PYTHONPATH` is scrubbed for the same reason.

    Asserts on a served response, not just on a clean start: an app that
    imports but never binds is not what the quickstart promises.
    """
    script = _console_script()
    (tmp_path / "app.py").write_text(QUICKSTART_APP)
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}

    process = subprocess.Popen(
        [str(script), "run", "app:app", "--port", "0", "--loop", loop],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        line = _read_line(process.stdout, 30.0)
        if loop == "metal" and "io_uring" in line.lower():
            pytest.skip("io_uring unavailable")
        assert "No module named" not in line, line
        assert line.startswith("\N{HERB} wreath "), line
        assert "serving app:app on http://127.0.0.1:" in line, line
        assert f"{loop} loop" in line, line

        port = int(line.split("http://127.0.0.1:")[1].split()[0])
        assert port != 0, "the startup line must name the bound port, not the requested one"

        connection = HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", "/hello/world")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"hello": "world"}
        connection.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_loading_an_application_leaves_an_already_importable_cwd_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The empty `sys.path` entry already means the working directory.

    A second, absolute entry for the same directory would be harmless but
    untrue to what the interpreter was handed, and it would grow `sys.path` once
    per CLI command in a long-lived process.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", ["", *sys.path])

    _ensure_cwd_importable()

    assert sys.path.count(str(tmp_path)) == 0
    assert sys.path[0] == ""


def teardown_module() -> None:
    for name in tuple(sys.modules):
        if name == "sample_cli_app" or name.startswith("sample_cli_app."):
            sys.modules.pop(name, None)
