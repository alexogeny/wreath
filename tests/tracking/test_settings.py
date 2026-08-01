"""Start-up configuration: the two refusals and the one warning.

Small, and worth having anyway: these are the code paths a reader hits *first*,
before anything else in the example has a chance to work, and a message that
names the wrong variable at that moment costs somebody an afternoon.

No database — that is the point of `database_url` being a method rather than a
field.
"""

from __future__ import annotations

import pytest
from tracking.config import (
    DEVELOPMENT_SESSION_SECRET,
    DSN_VARIABLE,
    FALLBACK_DSN_VARIABLE,
    SETTINGS,
)


def test_an_unset_dsn_is_a_refusal_naming_both_variables(monkeypatch) -> None:
    """Guessing at localhost and connecting to the wrong database is worse.

    Both names are in the message because someone who has run wreath's own
    database suites has already exported the fallback, and telling them to set a
    variable they do not need is the kind of instruction people follow anyway.
    """
    monkeypatch.delenv(DSN_VARIABLE, raising=False)
    monkeypatch.delenv(FALLBACK_DSN_VARIABLE, raising=False)
    with pytest.raises(RuntimeError) as raised:
        SETTINGS.database_url()
    assert DSN_VARIABLE in str(raised.value)
    assert FALLBACK_DSN_VARIABLE in str(raised.value)


def test_the_fallback_variable_is_honoured_but_does_not_win(monkeypatch) -> None:
    """A developer's own `TRACKING_DSN` beats the one wreath's suites export.

    Getting the precedence backwards would silently point the example at the
    test database on a machine that has both, which reads as "my data is not
    there" rather than as a configuration problem.
    """
    monkeypatch.delenv(DSN_VARIABLE, raising=False)
    monkeypatch.setenv(FALLBACK_DSN_VARIABLE, "postgresql://fallback/db")
    assert SETTINGS.database_url() == "postgresql://fallback/db"

    monkeypatch.setenv(DSN_VARIABLE, "postgresql://mine/db")
    assert SETTINGS.database_url() == "postgresql://mine/db"


def test_an_unset_session_secret_warns_and_still_starts(monkeypatch) -> None:
    """A fresh clone runs with no setup, and is told once that it is unsafe.

    The two alternatives are both worse: refusing to start makes a reader's
    first experience an error about a variable they have no opinion on yet, and
    generating a random secret works perfectly on one process and silently signs
    every user out the moment a second replica starts.
    """
    monkeypatch.delenv("TRACKING_SESSION_SECRET", raising=False)
    with pytest.warns(RuntimeWarning, match="TRACKING_SESSION_SECRET"):
        assert SETTINGS.session_secret() == DEVELOPMENT_SESSION_SECRET


def test_a_set_session_secret_is_used_without_warning(monkeypatch) -> None:
    """And the warning stops, which is what makes it worth reading when it fires."""
    import warnings

    monkeypatch.setenv("TRACKING_SESSION_SECRET", "a" * 40)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert SETTINGS.session_secret() == "a" * 40
