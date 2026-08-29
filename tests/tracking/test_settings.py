from __future__ import annotations

import pytest
from tracking.config import (
    DEVELOPMENT_SESSION_SECRET,
    DSN_VARIABLE,
    FALLBACK_DSN_VARIABLE,
    SETTINGS,
)


def test_an_unset_dsn_is_a_refusal_naming_both_variables(monkeypatch) -> None:
    monkeypatch.delenv(DSN_VARIABLE, raising=False)
    monkeypatch.delenv(FALLBACK_DSN_VARIABLE, raising=False)
    with pytest.raises(RuntimeError) as raised:
        SETTINGS.database_url()
    assert DSN_VARIABLE in str(raised.value)
    assert FALLBACK_DSN_VARIABLE in str(raised.value)


def test_the_fallback_variable_is_honoured_but_does_not_win(monkeypatch) -> None:
    monkeypatch.delenv(DSN_VARIABLE, raising=False)
    monkeypatch.setenv(FALLBACK_DSN_VARIABLE, "postgresql://fallback/db")
    assert SETTINGS.database_url() == "postgresql://fallback/db"

    monkeypatch.setenv(DSN_VARIABLE, "postgresql://mine/db")
    assert SETTINGS.database_url() == "postgresql://mine/db"


def test_an_unset_session_secret_warns_and_still_starts(monkeypatch) -> None:
    monkeypatch.delenv("TRACKING_SESSION_SECRET", raising=False)
    with pytest.warns(RuntimeWarning, match="TRACKING_SESSION_SECRET"):
        assert SETTINGS.session_secret() == DEVELOPMENT_SESSION_SECRET


def test_a_set_session_secret_is_used_without_warning(monkeypatch) -> None:
    import warnings

    monkeypatch.setenv("TRACKING_SESSION_SECRET", "a" * 40)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert SETTINGS.session_secret() == "a" * 40
