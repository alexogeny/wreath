"""`wreath infra infer` end to end, through the same parser the shipped CLI uses.

The exit code is the contract worth pinning: a plan with gaps exits 1, so a CI
step that runs this fails on a settings key nothing supplies rather than
printing one and moving on.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from wreath._cli import main

APP_SOURCE = '''
"""A one-database application with a queue, a bucket, and a settings model."""

from dataclasses import dataclass

from wreath.app import Wreath
from wreath.config import Secret


@dataclass
class Settings:
    dsn: str
    token: Secret[str]
    debug: bool = False


def build() -> Wreath:
    application = Wreath()
    application.postgres("main", dsn="postgresql://trek@db.internal:5432/trek")
    application.jobs("ingest", database="main")
    application.http_client("forage", base_url="https://forage.example.com")
    return application


app = build()
'''


@pytest.fixture
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module = tmp_path / "trek_infra_app.py"
    module.write_text(APP_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield "trek_infra_app:app"
    sys.modules.pop("trek_infra_app", None)


def run(*argv: str) -> int:
    return main(["infra", *argv])


def test_text_output_names_every_section(target: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = run("infer", target)
    out = capsys.readouterr().out
    assert code == 0
    for heading in (
        "PostgreSQL (1)",
        "Object storage (0)",
        "Egress (1)",
        "Listener",
        "What would be a separate service somewhere else",
        "Settings contract",
        "Gaps (0)",
    ):
        assert heading in out
    assert "db.internal:5432/trek" in out
    assert "https://forage.example.com" in out


def test_json_output_is_parseable_and_typed(
    target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("infer", target, "--format", "json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["application"] == target
    assert data["databases"][0]["name"] == "main"
    assert data["egress"][0]["origin"] == "https://forage.example.com"
    assert {row["module"] for row in data["subsystems"]} >= {"wreath.jobs", "wreath.messaging"}


def test_a_settings_gap_fails_the_command(
    target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run("infer", target, "--settings", "trek_infra_app:Settings=TREK")
    out = capsys.readouterr().out
    assert code == 1
    assert "[missing] TREK_DSN" in out
    assert "[missing] TREK_TOKEN" in out


def test_a_dotenv_supplies_the_contract_and_the_command_passes(
    target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv = tmp_path / "deploy.env"
    dotenv.write_text("TREK_DSN=postgresql://trek@db/trek\nTREK_TOKEN=t\n", encoding="utf-8")
    code = run("infer", target, "--settings", "trek_infra_app:Settings=TREK",
               "--env", str(dotenv))
    out = capsys.readouterr().out
    assert code == 0
    assert "Gaps (0)" in out
    assert str(dotenv) in out


def test_an_unread_dotenv_key_is_reported(
    target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv = tmp_path / "deploy.env"
    dotenv.write_text(
        "TREK_DSN=postgresql://trek@db/trek\nTREK_TOKEN=t\nTREK_TOEKN=t\n", encoding="utf-8"
    )
    code = run("infer", target, "--settings", "trek_infra_app:Settings=TREK",
               "--env", str(dotenv))
    out = capsys.readouterr().out
    assert code == 1
    assert "[unread-key] TREK_TOEKN" in out


def test_a_dotenv_that_will_not_parse_names_the_file(
    target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv = tmp_path / "deploy.env"
    dotenv.write_text("# a comment the strict dialect refuses\n", encoding="utf-8")
    assert run("infer", target, "--env", str(dotenv)) == 2
    assert str(dotenv) in capsys.readouterr().err


def test_a_settings_target_that_is_not_a_dataclass_is_refused(
    target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("infer", target, "--settings", "trek_infra_app:build") == 2
    assert "is not a dataclass type" in capsys.readouterr().err


def test_a_settings_spec_without_a_colon_is_refused(
    target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("infer", target, "--settings", "trek_infra_app.Settings") == 2
    assert "module:attribute" in capsys.readouterr().err


def test_the_process_environment_can_be_the_supplier(
    target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TREK_DSN", "postgresql://trek@db/trek")
    monkeypatch.setenv("TREK_TOKEN", "t")
    code = run("infer", target, "--settings", "trek_infra_app:Settings=TREK", "--environ")
    out = capsys.readouterr().out
    assert code == 0
    assert "TREK_DSN" in out
    assert "process" in out


def test_a_factory_target_is_supported(
    target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("infer", "trek_infra_app:build", "--factory") == 0
    assert "db.internal:5432/trek" in capsys.readouterr().out
