from __future__ import annotations

from pathlib import Path

import pytest

from wreath._pure.env import parse_dotenv as pure_parse_dotenv
from wreath.config import Environment, find_dotenv, load_env, parse_dotenv, read_osenv


def test_dotenv_parser_is_literal_and_strict() -> None:
    data = b"NAME=neo\nEMPTY=\nLITERAL=$(whoami)\nREFERENCE=${HOME}\n"
    expected = {
        "NAME": "neo",
        "EMPTY": "",
        "LITERAL": "$(whoami)",
        "REFERENCE": "${HOME}",
    }
    assert pure_parse_dotenv(data) == expected
    assert parse_dotenv(data) == expected
    with pytest.raises(ValueError, match="line 1"):
        parse_dotenv(b"export NAME=neo\n")
    with pytest.raises(ValueError, match="line 1"):
        parse_dotenv(b"NO_EQUALS\n")


def test_explicit_dotenv_path_searches_parents(tmp_path: Path) -> None:
    dotenv = tmp_path / "config.env"
    dotenv.write_text("FROM_FILE=yes\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_dotenv("config.env", start=nested) == dotenv


def test_process_environment_wins_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("WREATH_TEST_VALUE=file\nFILE_ONLY=yes\n", encoding="utf-8")
    monkeypatch.setenv("WREATH_TEST_VALUE", "process")

    values = load_env(dotenv)
    overridden = load_env(dotenv, override=True)

    assert values["WREATH_TEST_VALUE"] == "process"
    assert values["FILE_ONLY"] == "yes"
    assert overridden["WREATH_TEST_VALUE"] == "file"
    assert read_osenv()["WREATH_TEST_VALUE"] == "process"


def test_environment_repr_does_not_include_values() -> None:
    environment = Environment({"SECRET": "do-not-print"})
    assert repr(environment) == "Environment(keys=('SECRET',))"
    assert "do-not-print" not in repr(environment)
