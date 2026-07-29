from __future__ import annotations

import pytest

from wreath.server import (
    EnvConfigWarning,
    ServerConfig,
    configure_from_env,
    missing_required_env,
)


def test_from_env_binds_registered_variables() -> None:
    env = {
        "WREATH_HOST": "0.0.0.0",
        "WREATH_PORT": "9000",
        "WREATH_BACKLOG": "4096",
        "WREATH_REQUEST_TIMEOUT": "12.5",
        "WREATH_DATE_HEADER": "off",
        "WREATH_LIFESPAN": "on",
        "WREATH_PROTOCOLS": "http/1.1, h2",
        "WREATH_MAX_BODY_BYTES": "1048576",
        "WREATH_MAX_BODY_CHUNKS": "2048",
    }
    config = ServerConfig.from_env(env)
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.backlog == 4096
    assert config.request_timeout == 12.5
    assert config.date_header is False
    assert config.lifespan == "on"
    assert config.protocols == ("http/1.1", "h2")
    assert config.max_body_bytes == 1_048_576
    assert config.max_body_chunks == 2048


def test_from_env_defaults_when_absent_or_empty() -> None:
    default = ServerConfig()
    # An unset environment and an explicitly empty value both fall back.
    assert ServerConfig.from_env({}) == default
    assert ServerConfig.from_env({"WREATH_HOST": "", "WREATH_PORT": ""}) == default


def test_precedence_is_defaults_then_env_then_overrides() -> None:
    env = {"WREATH_HOST": "0.0.0.0", "WREATH_PORT": "9000"}
    config = ServerConfig.from_env(env, port=1234)
    # env beats the default, explicit override beats env.
    assert config.host == "0.0.0.0"
    assert config.port == 1234


def test_bad_value_names_the_offending_variable() -> None:
    with pytest.raises(ValueError, match="WREATH_PORT"):
        ServerConfig.from_env({"WREATH_PORT": "not-a-number"})
    with pytest.raises(ValueError, match="WREATH_DATE_HEADER"):
        ServerConfig.from_env({"WREATH_DATE_HEADER": "maybe"})
    with pytest.raises(ValueError, match="WREATH_LIFESPAN"):
        ServerConfig.from_env({"WREATH_LIFESPAN": "sometimes"})


def test_env_value_still_passes_through_serverconfig_validation() -> None:
    # from_env is not a bypass: the dataclass invariants still hold.
    with pytest.raises(ValueError, match="port must be in 0..65535"):
        ServerConfig.from_env({"WREATH_PORT": "70000"})
    with pytest.raises(ValueError, match="unknown protocol"):
        ServerConfig.from_env({"WREATH_PROTOCOLS": "http/9"})


def test_missing_required_env_reports_unset_and_empty() -> None:
    env = {"DATABASE_URL": "postgres://x", "SECRET_KEY": ""}
    missing = missing_required_env(("DATABASE_URL", "SECRET_KEY", "API_TOKEN"), env)
    assert missing == ["SECRET_KEY", "API_TOKEN"]


def test_configure_from_env_warns_on_missing_critical_var() -> None:
    env = {"WREATH_PORT": "9000"}
    with pytest.warns(EnvConfigWarning, match="DATABASE_URL"):
        config, missing = configure_from_env(env, required=("DATABASE_URL",))
    assert missing == ["DATABASE_URL"]
    assert config.port == 9000


def test_configure_from_env_silent_when_critical_vars_present() -> None:
    env = {"DATABASE_URL": "postgres://x"}
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error", EnvConfigWarning)
        config, missing = configure_from_env(env, required=("DATABASE_URL",))
    assert missing == []
    assert config == ServerConfig()


def test_configure_from_env_can_suppress_warnings() -> None:
    env: dict[str, str] = {}
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error", EnvConfigWarning)
        _, missing = configure_from_env(env, required=("DATABASE_URL",), warn=False)
    assert missing == ["DATABASE_URL"]


def test_registry_and_dataclass_fields_stay_in_sync() -> None:
    # Every registered spec must target a real ServerConfig field, or the
    # registry has drifted from the dataclass.
    from dataclasses import fields

    from wreath.server import _SERVER_ENV_REGISTRY

    valid = {f.name for f in fields(ServerConfig)}
    for spec in _SERVER_ENV_REGISTRY:
        assert spec.field in valid, spec.var
    # Env var names are unique and namespaced.
    names = [spec.var for spec in _SERVER_ENV_REGISTRY]
    assert len(names) == len(set(names))
    assert all(name.startswith("WREATH_") for name in names)
