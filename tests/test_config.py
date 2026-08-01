from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import pytest

from wreath._pure.env import parse_dotenv as pure_parse_dotenv
from wreath.config import (
    Env,
    Environment,
    Secret,
    SettingsError,
    find_dotenv,
    load_env,
    parse_dotenv,
    read_osenv,
)


def test_dotenv_parser_is_literal_and_strict() -> None:
    data = b"NAME=wreath\nEMPTY=\nLITERAL=$(whoami)\nREFERENCE=${HOME}\n"
    expected = {
        "NAME": "wreath",
        "EMPTY": "",
        "LITERAL": "$(whoami)",
        "REFERENCE": "${HOME}",
    }
    assert pure_parse_dotenv(data) == expected
    assert parse_dotenv(data) == expected
    with pytest.raises(ValueError, match="line 1"):
        parse_dotenv(b"export NAME=wreath\n")
    with pytest.raises(ValueError, match="line 1"):
        parse_dotenv(b"NO_EQUALS\n")
    # A whole-line comment too. This dialect has no comment syntax, which is
    # the clause the next test exists to keep honest.
    with pytest.raises(ValueError, match="line 1"):
        parse_dotenv(b"# a comment\nNAME=wreath\n")


def test_the_shipped_dotenv_template_can_actually_be_copied() -> None:
    """`example/.env.example` says "copy me", so copying it must produce a
    loadable `.env`.

    It did not: the template opened with two comment lines explaining that the
    parser is strict, and the parser rejected the first of them --
    `ValueError: invalid dotenv entry on line 1`. A template nobody can use is
    a documentation defect whatever the parser does, and this dialect has no
    comment syntax to loosen its way out of. Parsed here rather than eyeballed,
    because the next person to annotate the file will find out from this test
    instead of from a reader who copied it.
    """
    template = Path(__file__).resolve().parents[1] / "example" / ".env.example"
    values = parse_dotenv(template.read_bytes())
    assert "CAMERA_TRAP_DSN" in values
    assert values["CAMERA_TRAP_MAX_WINDOW_DAYS"] == "90"


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


class LogLevel(enum.StrEnum):
    INFO = "info"
    DEBUG = "debug"


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int = 5432
    password: Secret[str] = Secret("")


@dataclass(frozen=True)
class ApplicationSettings:
    debug: bool
    mode: Literal["development", "production"]
    log_level: LogLevel
    database: DatabaseSettings
    workers: int = 4
    token: Annotated[Secret[str], Env("SERVICE_TOKEN")] = Secret("")


def test_environment_binds_nested_typed_settings_and_aliases() -> None:
    environment = Environment(
        {
            "APP_DEBUG": "yes",
            "APP_MODE": "production",
            "APP_LOG_LEVEL": "debug",
            "APP_DATABASE__HOST": "db.internal",
            "APP_DATABASE__PORT": "6432",
            "APP_DATABASE__PASSWORD": "database-secret",
            "SERVICE_TOKEN": "service-secret",
        },
        sources={"APP_DEBUG": "process", "SERVICE_TOKEN": "secrets.env"},
    )

    settings = environment.bind(ApplicationSettings, prefix="APP")

    assert settings.debug is True
    assert settings.mode == "production"
    assert settings.log_level is LogLevel.DEBUG
    assert settings.database.host == "db.internal"
    assert settings.database.port == 6432
    assert settings.database.password.reveal() == "database-secret"
    assert settings.workers == 4
    assert settings.token.reveal() == "service-secret"
    assert environment.source("APP_DEBUG") == "process"
    assert environment.source("SERVICE_TOKEN") == "secrets.env"


def test_settings_errors_are_aggregated_and_secrets_are_redacted() -> None:
    environment = Environment(
        {
            "APP_DEBUG": "sometimes",
            "APP_MODE": "staging",
            "APP_LOG_LEVEL": "trace",
            "APP_DATABASE__PORT": "many",
            "APP_DATABASE__PASSWORD": "must-not-leak",
        }
    )

    with pytest.raises(SettingsError) as caught:
        environment.bind(ApplicationSettings, prefix="APP")

    assert [error["key"] for error in caught.value.errors] == [
        "APP_DEBUG",
        "APP_MODE",
        "APP_LOG_LEVEL",
        "APP_DATABASE__HOST",
        "APP_DATABASE__PORT",
    ]
    assert "must-not-leak" not in str(caught.value)
    assert "must-not-leak" not in repr(caught.value.errors)


def test_secret_never_reveals_its_value_implicitly() -> None:
    secret = Secret("credential")

    assert repr(secret) == "Secret(***)"
    assert str(secret) == "***"
    assert secret.reveal() == "credential"


@dataclass(frozen=True)
class ConversionSettings:
    count: int
    ratio: float
    price: Decimal
    identifier: UUID
    path: Path
    timestamp: dt.datetime
    day: dt.date
    clock: dt.time
    disabled: bool
    optional: int | None
    optional_value: int | None
    optional_first: None | int
    choice: int | float
    numbers: list[int]
    coordinates: tuple[int, ...]
    roles: set[str]
    frozen_roles: frozenset[str]
    literal: Literal["one", "two"]
    secret_number: Secret[int]
    arbitrary: Any
    aliased: Annotated[str, "documentation", Env("EXACT_ALIAS")]


def test_environment_bind_covers_every_supported_conversion_branch() -> None:
    identifier = UUID("cbfb7892-bbe8-4d26-9c5d-e12d17f404e2")
    environment = Environment(
        {
            "CFG_COUNT": "7",
            "CFG_RATIO": "1.25",
            "CFG_PRICE": "12.340",
            "CFG_IDENTIFIER": str(identifier),
            "CFG_PATH": "/srv/wreath",
            "CFG_TIMESTAMP": "2026-07-31T12:34:56+10:00",
            "CFG_DAY": "2026-07-31",
            "CFG_CLOCK": "12:34:56",
            "CFG_DISABLED": "off",
            "CFG_OPTIONAL": "",
            "CFG_OPTIONAL_VALUE": "9",
            "CFG_OPTIONAL_FIRST": "10",
            "CFG_CHOICE": "1.5",
            "CFG_NUMBERS": "1, 2,3",
            "CFG_COORDINATES": "4,5",
            "CFG_ROLES": "reader,writer,reader",
            "CFG_FROZEN_ROLES": "reader,writer",
            "CFG_LITERAL": "two",
            "CFG_SECRET_NUMBER": "42",
            "CFG_ARBITRARY": "untouched",
            "EXACT_ALIAS": "aliased",
        }
    )

    settings = environment.bind(ConversionSettings, prefix="CFG")

    assert settings == ConversionSettings(
        count=7,
        ratio=1.25,
        price=Decimal("12.340"),
        identifier=identifier,
        path=Path("/srv/wreath"),
        timestamp=dt.datetime.fromisoformat("2026-07-31T12:34:56+10:00"),
        day=dt.date(2026, 7, 31),
        clock=dt.time(12, 34, 56),
        disabled=False,
        optional=None,
        optional_value=9,
        optional_first=10,
        choice=1.5,
        numbers=[1, 2, 3],
        coordinates=(4, 5),
        roles={"reader", "writer"},
        frozen_roles=frozenset({"reader", "writer"}),
        literal="two",
        secret_number=Secret(42),
        arbitrary="untouched",
        aliased="aliased",
    )


@dataclass(frozen=True)
class InvalidConversionSettings:
    enabled: bool
    count: int
    ratio: float
    price: Decimal
    identifier: UUID
    timestamp: dt.datetime
    day: dt.date
    clock: dt.time
    level: LogLevel
    literal: Literal["one", "two"]
    choice: int | float
    unsupported: dict[str, str]
    unsupported_class: complex


def test_environment_bind_aggregates_each_invalid_conversion_kind() -> None:
    environment = Environment(
        {
            "BAD_ENABLED": "perhaps",
            "BAD_COUNT": "many",
            "BAD_RATIO": "several",
            "BAD_PRICE": "money",
            "BAD_IDENTIFIER": "not-a-uuid",
            "BAD_TIMESTAMP": "yesterday",
            "BAD_DAY": "someday",
            "BAD_CLOCK": "lunchtime",
            "BAD_LEVEL": "verbose",
            "BAD_LITERAL": "three",
            "BAD_CHOICE": "neither",
            "BAD_UNSUPPORTED": "a=b",
            "BAD_UNSUPPORTED_CLASS": "1+2j",
        }
    )

    with pytest.raises(SettingsError) as caught:
        environment.bind(InvalidConversionSettings, prefix="BAD")

    assert [(error["key"], error["msg"], error["type"]) for error in caught.value.errors] == [
        ("BAD_ENABLED", "value is not a boolean", "invalid"),
        ("BAD_COUNT", "value is not an integer", "invalid"),
        ("BAD_RATIO", "value is not a number", "invalid"),
        ("BAD_PRICE", "value is not a decimal", "invalid"),
        ("BAD_IDENTIFIER", "value is not a UUID", "invalid"),
        ("BAD_TIMESTAMP", "value is not an ISO-8601 datetime", "invalid"),
        ("BAD_DAY", "value is not an ISO-8601 date", "invalid"),
        ("BAD_CLOCK", "value is not an ISO-8601 time", "invalid"),
        ("BAD_LEVEL", "value is not an allowed enum member", "invalid"),
        ("BAD_LITERAL", "value is not one of the allowed literals", "invalid"),
        ("BAD_CHOICE", "value matches no union member", "invalid"),
        (
            "BAD_UNSUPPORTED",
            "unsupported settings annotation dict[str, str]",
            "invalid",
        ),
        (
            "BAD_UNSUPPORTED_CLASS",
            "unsupported settings annotation <class 'complex'>",
            "invalid",
        ),
    ]


@dataclass(frozen=True)
class NonOptionalUnionSettings:
    choice: int | float


def test_an_empty_non_optional_union_is_not_treated_as_none() -> None:
    with pytest.raises(SettingsError) as caught:
        Environment({"CFG_CHOICE": ""}).bind(NonOptionalUnionSettings, prefix="CFG")

    assert caught.value.errors == [
        {
            "key": "CFG_CHOICE",
            "msg": "value matches no union member",
            "type": "invalid",
        }
    ]
