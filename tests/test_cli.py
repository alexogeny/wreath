from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

from wreath import cli
from wreath._cli import CliError, build_parser, load_application, options_from_namespace


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
    assert options.lifespan == "auto"
    assert options.factory is False
    assert options.server_header == "wreath"
    assert options.date_header is True


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

    def fake_run(app: Any, config: Any, *, tls: Any, loop_factory: Any) -> None:
        calls.append((app, config, tls, loop_factory))

    monkeypatch.setattr("wreath._cli.run_server", fake_run)

    assert cli.main(["run", f"{module_name}:app", "--port", "8123"]) == 0
    assert len(calls) == 1
    assert calls[0][1].port == 8123
    assert calls[0][2] is None
    assert calls[0][3] is None


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


def teardown_module() -> None:
    for name in tuple(sys.modules):
        if name == "sample_cli_app" or name.startswith("sample_cli_app."):
            sys.modules.pop(name, None)
