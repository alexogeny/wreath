"""ORM, CRUD, GraphQL, and session details (report 23: G-32, G-36, G-59, G-66,
G-67, G-69, G-70, G-72, G-78, G-79, G-82)."""

from __future__ import annotations

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Text


class Widget(Model, table="rgb_widgets"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)


class TestStaleWriteDetection:
    """G-32: `_update`/`_delete` ignore the affected row count, so a write
    against a row another session already deleted silently no-ops."""

    def _session(self, status):
        from wreath.orm.registry import Registry
        from wreath.orm.session import Session

        class _Connection:
            async def execute(self, sql, *args):
                return status

        class _Database:
            name = "app"

            async def acquire(self, workload):
                return _Connection()

            async def release(self, workload, connection):
                pass

        registry = Registry(_Database(), [Widget], validate_schema="off")
        return Session(registry, "write")

    async def test_an_update_that_matched_nothing_is_reported(self):
        from wreath.orm.errors import StaleDataError

        session = self._session("UPDATE 0")
        widget = Widget(id=1, name="a")
        widget._orm_state = 2           # PERSISTENT
        widget._orm_owner = session
        widget.name = "b"
        with pytest.raises(StaleDataError):
            await session._update(widget)

    async def test_an_update_that_matched_is_silent(self):
        session = self._session("UPDATE 1")
        widget = Widget(id=1, name="a")
        widget._orm_state = 2
        widget._orm_owner = session
        widget.name = "b"
        await session._update(widget)

    async def test_an_unparseable_status_is_not_treated_as_stale(self):
        """A test double or a driver that reports nothing must not start
        raising -- absence of evidence is not evidence of a lost write."""
        session = self._session("OK")
        widget = Widget(id=1, name="a")
        widget._orm_state = 2
        widget._orm_owner = session
        widget.name = "b"
        await session._update(widget)


class TestRollbackCancellation:
    """G-36: `_rollback_all` catches `BaseException`, so a cancellation arriving
    during rollback is swallowed and the task keeps running."""

    async def test_cancellation_during_rollback_still_cancels(self):
        import asyncio

        from wreath.orm.session import Session

        class _Connection:
            closed = False

            async def execute(self, sql, *args):
                raise asyncio.CancelledError

            async def close(self):
                self.closed = True

        class _Database:
            name = "app"

            async def acquire(self, workload):
                return _Connection()

            async def release(self, workload, connection):
                pass

        class _Registry:
            database = _Database()
            schema_mode = None

        session = Session(_Registry(), "write")
        await session._acquire()
        session._depth = 1
        with pytest.raises(asyncio.CancelledError):
            await session.close()


class TestCrudInputHandling:
    """G-69: `coerce_pk` turns any digit-string into an int, breaking text
    primary keys. G-70: a driver error is not `(TypeError, ValueError)` and
    becomes a 500. G-72: raw exception text is returned to the client."""

    def test_the_primary_key_type_follows_the_model(self):
        from wreath.crud import _coerce_pk_for

        coerce = _coerce_pk_for(Widget)
        assert coerce("12") == 12

        class Token(Model, table="rgb_tokens"):
            id: Mapped[str] = column(Text, primary_key=True)
            note: Mapped[str] = column(Text)

        assert _coerce_pk_for(Token)("12") == "12"

    def test_an_unparseable_key_stays_a_string(self):
        from wreath.crud import _coerce_pk_for

        assert _coerce_pk_for(Widget)("abc") == "abc"

    def test_client_errors_do_not_echo_exception_text(self):
        from wreath.crud import _unprocessable

        response = _unprocessable(RuntimeError('relation "secrets" does not exist'))
        assert b"secrets" not in response.body


class TestGraphQLSessionAndErrors:
    """G-78: the default session is a *read* one, so a registered mutation runs
    on the read workload. G-79: a resolver exception that is not an
    ExecutionError escapes and becomes a transport 500."""

    def _api(self):
        from wreath.graphql import GraphQL
        from wreath.orm.registry import Registry

        class _Database:
            name = "rgb"

            async def acquire(self, workload):  # pragma: no cover
                raise AssertionError

            async def release(self, workload, connection):  # pragma: no cover
                pass

        return GraphQL(
            Registry(_Database(), [Widget], validate_schema="off"), models=[Widget]
        )

    async def test_a_mutation_gets_a_write_session(self):
        api = self._api()

        @api.mutation("touch", returns="Widget")
        async def touch(info):  # pragma: no cover
            return None

        api.validate()
        session, _close = await api._session(None, None, mutating=True)
        assert session.workload == "write"

    async def test_a_query_still_gets_a_read_session(self):
        api = self._api()
        api.validate()
        session, _close = await api._session(None, None, mutating=False)
        assert session.workload == "read"

    async def test_a_resolver_exception_becomes_a_graphql_error(self):
        api = self._api()

        @api.query("boom", returns="Widget")
        async def boom(info):
            raise RuntimeError("resolver blew up")

        api.validate()
        body = await api.run("{ boom { id } }", session=None)
        assert "errors" in body
        assert body["data"] is None
        # And it does not leak the exception text.
        assert "blew up" not in str(body)


class TestSessionSlidingExpiry:
    """G-59: a stored session's TTL is refreshed only when its content changes,
    so an actively used session expires at its absolute age."""

    async def test_reading_an_active_session_extends_it(self):
        from wreath.policy.sessions import SessionPolicy

        saved: list = []

        class _Store:
            async def load(self, sid):
                return {"who": "ann"}

            async def save(self, sid, data, max_age):
                saved.append((sid, data, max_age))

            async def delete(self, sid):  # pragma: no cover
                pass

            async def touch(self, sid, max_age):
                saved.append(("touch", sid, max_age))

        middleware = SessionPolicy(secret="s" * 32, store=_Store())
        signed = middleware._sign(b"sid-1", __import__("time").time().__trunc__())

        class _State:
            def get(self, name, default=None):
                return getattr(self, name, default)

        class _Request:
            cookies = {"wreath_session": signed}
            state = _State()

        class _Response:
            headers: list = []

            def set_cookie(self, *args, **kwargs):
                pass

            def delete_cookie(self, *args, **kwargs):  # pragma: no cover
                pass

        request = _Request()
        await middleware.before(request)
        await middleware.after(request, _Response())
        assert any(entry[0] == "touch" for entry in saved), (
            "an unchanged but live session was never extended"
        )


class TestPaginationTotals:
    """G-82: the count and the page are two statements outside a transaction, so
    the total and the items can disagree with no way for a caller to tell."""

    async def test_paginate_documents_the_two_statement_read(self):
        import inspect

        from wreath.pagination import paginate

        doc = inspect.getdoc(paginate) or ""
        assert "transaction" in doc.lower() or "two" in doc.lower()
