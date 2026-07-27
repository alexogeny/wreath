"""What a very large fetch costs the session (report 23: G-37)."""

from __future__ import annotations

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text


class Row(Model, table="rgb_rows"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)


def _session(**kwargs):
    from wreath.orm.session import Session

    class _Database:
        name = "app"

        async def acquire(self, workload):  # pragma: no cover
            raise AssertionError("no connection in these tests")

        async def release(self, workload, connection):  # pragma: no cover
            pass

    return Session(Registry(_Database(), [Row], validate_schema="off"), "read", **kwargs)


class TestIdentityMapWarning:
    """G-37: the identity map is unbounded for the session's life, so a fetch
    that hydrates a million rows pins a million objects -- and nothing says so
    until the process is in trouble.

    A hard ceiling is the wrong answer: evicting an entry would detach an object
    the caller still holds, which changes what the ORM means. What ships is a
    threshold you can *ask* about, plus the behaviour written into the class
    docstring -- because an automatic check costs a boundary crossing on a path
    every request pays for."""

    def test_a_large_identity_map_warns_once(self):
        session = _session(identity_map_warn_at=10)
        with pytest.warns(ResourceWarning, match="identity map"):
            for index in range(11):
                session._identity[(object(), (index,))] = object()
                session.check_identity_map()

    def test_it_warns_only_once_per_session(self):
        import warnings

        session = _session(identity_map_warn_at=5)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for index in range(50):
                session._identity[(object(), (index,))] = object()
                session.check_identity_map()
        assert len([w for w in caught if issubclass(w.category, ResourceWarning)]) == 1

    def test_an_ordinary_session_says_nothing(self):
        import warnings

        session = _session(identity_map_warn_at=1000)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for index in range(100):
                session._identity[(object(), (index,))] = object()
                session.check_identity_map()
        assert caught == []

    def test_the_threshold_can_be_turned_off(self):
        import warnings

        session = _session(identity_map_warn_at=None)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for index in range(100):
                session._identity[(object(), (index,))] = object()
                session.check_identity_map()
        assert caught == []

    def test_nothing_checks_automatically(self):
        """The check is called, not scheduled: every automatic placement --
        per fetch, per session close -- measured at +1 boundary crossing on the
        realistic scenario, which is too much for a diagnostic that fires for
        almost nobody."""
        import inspect

        from wreath.orm.session import Session

        for method in (Session.fetch, Session.close, Session._fetch_objects):
            assert "check_identity_map" not in inspect.getsource(method)

    def test_the_behaviour_is_documented(self):
        import inspect

        from wreath.orm.session import Session

        doc = inspect.getdoc(Session) or ""
        assert "identity map" in doc.lower()
