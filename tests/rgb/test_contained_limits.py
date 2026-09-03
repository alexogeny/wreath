from __future__ import annotations

import pytest


class TestHeaderBounds:
    """G-47: nothing bounds the header count or the Cookie size at the framework
    layer, so a request that gets past the front server is unbounded here --
    and `RequestLimits` exists precisely because "the server in front will
    handle it" is not true of every conforming ASGI server."""

    def _request(self, headers, **limits):
        from wreath.request import Request, RequestLimits

        return Request(
            {"type": "http", "method": "GET", "path": "/", "headers": headers},
            None,
            limits=RequestLimits(**limits) if limits else RequestLimits(),
        )

    def test_the_header_count_is_left_to_the_server(self):
        headers = [(f"x-{n}".encode(), b"v") for n in range(500)]
        assert len(self._request(headers)._index_headers()) == 500

    def test_an_enormous_cookie_header_is_refused(self):
        from wreath.exceptions import RequestHeaderFieldsTooLarge

        request = self._request([(b"cookie", b"a=" + b"x" * 100_000)], max_cookie_bytes=8192)
        with pytest.raises(RequestHeaderFieldsTooLarge):
            _ = request.cookies

    def test_ordinary_headers_are_unaffected(self):
        request = self._request([(b"host", b"example.com"), (b"cookie", b"sid=abc; theme=dark")])
        assert request._index_headers()[b"host"] == b"example.com"
        assert request.cookies == {"sid": "abc", "theme": "dark"}

    def test_the_bounds_are_configurable(self):
        from wreath.request import RequestLimits

        assert RequestLimits(max_cookie_bytes=64).max_cookie_bytes == 64
        with pytest.raises(ValueError):
            RequestLimits(max_cookie_bytes=0)


class TestStatementTimeout:
    """B-09: nothing sets a statement timeout, so one pathological query holds a
    pooled connection for as long as PostgreSQL will let it."""

    def _session(self, statements, **kwargs):
        from wreath.orm.session import Session

        class _Connection:
            async def execute(self, sql, *args):
                statements.append(sql)
                return "OK"

        class _Database:
            name = "app"

            async def acquire(self, workload):
                return _Connection()

            async def release(self, workload, connection):
                pass

        class _Registry:
            database = _Database()
            schema_mode = None
            statement_timeout = kwargs.pop("registry_timeout", None)

        return Session(_Registry(), "write", **kwargs)

    async def test_a_transaction_sets_the_timeout(self):
        statements: list[str] = []
        session = self._session(statements, statement_timeout=2.5)
        async with session.begin():
            pass
        assert any("SET LOCAL statement_timeout" in sql for sql in statements)
        assert any("2500" in sql for sql in statements)

    async def test_the_registry_supplies_a_default(self):
        statements: list[str] = []
        session = self._session(statements, registry_timeout=1.0)
        async with session.begin():
            pass
        assert any("SET LOCAL statement_timeout" in sql for sql in statements)

    async def test_no_timeout_configured_changes_nothing(self):
        statements: list[str] = []
        session = self._session(statements)
        async with session.begin():
            pass
        assert not any("statement_timeout" in sql for sql in statements)
        assert statements[0] == "BEGIN"

    async def test_a_savepoint_does_not_reset_it(self):
        statements: list[str] = []
        session = self._session(statements, statement_timeout=2.5)
        async with session.begin():
            async with session.begin():
                pass
        assert sum("SET LOCAL statement_timeout" in sql for sql in statements) == 1

    def test_a_non_positive_timeout_is_refused(self):
        from wreath.orm.errors import SessionError

        with pytest.raises((ValueError, SessionError)):
            self._session([], statement_timeout=0)


class TestCrudFieldAllowList:
    """G-68: sensitivity is a regex *deny*-list over column names -- `dob`,
    `iban`, `recovery_answer`, `pw` do not match -- while the module opens by
    promising the `password_hash` leak is impossible by accident."""

    def _model(self):
        from wreath.orm import Mapped, Model, column
        from wreath.orm.types import Int64, Text

        class Patient(Model, table="rgb_patients"):
            id: Mapped[int] = column(Int64, primary_key=True)
            name: Mapped[str] = column(Text)
            dob: Mapped[str] = column(Text, nullable=True)
            iban: Mapped[str] = column(Text, nullable=True)

        return Patient

    async def test_only_named_fields_are_serialized(self):
        import json

        from wreath.crud import Access, crud_router

        model = self._model()
        row = model(id=1, name="Ann", dob="1990-01-01", iban="GB00")

        class _Session:
            async def fetch(self, query):
                return [row]

            async def close(self):
                pass

        router = crud_router(
            model,
            lambda request: _Session(),
            operations=("list",),
            fields=("id", "name"),
            authorize=Access.public(),
        )

        class _Request:
            query_string = b""
            path_params: dict[str, str] = {}
            identity = None

        payload = json.loads((await router.routes[0].endpoint(_Request())).body)
        assert payload["items"] == [{"id": 1, "name": "Ann"}]

    def test_naming_an_unknown_field_is_refused(self):
        from wreath.crud import Access, crud_router

        with pytest.raises(ValueError, match="nope"):
            crud_router(
                self._model(),
                lambda request: None,
                operations=("list",),
                fields=("id", "nope"),
                authorize=Access.public(),
            )

    def test_fields_and_expose_are_mutually_exclusive(self):
        from wreath.crud import Access, crud_router

        with pytest.raises(ValueError, match="fields"):
            crud_router(
                self._model(),
                lambda request: None,
                operations=("list",),
                fields=("id",),
                expose=("dob",),
                authorize=Access.public(),
            )

    def test_the_deny_list_still_applies_without_fields(self):
        from wreath.crud import sensitive_fields
        from wreath.orm import Mapped, Model, column
        from wreath.orm.types import Int64, Text

        class Account(Model, table="rgb_accounts"):
            id: Mapped[int] = column(Int64, primary_key=True)
            password_hash: Mapped[str] = column(Text, nullable=True)

        assert "password_hash" in sensitive_fields(Account)


class TestCsrfTrustedHosts:
    """R-50: the expected origin is built from the raw `Host` header, so
    `Host: evil.example` + `Origin: http://evil.example` passes unless
    `TrustedHostPolicy` was separately mounted -- a dependency between two
    middlewares that nothing states or enforces."""

    def _request(self, host, origin):
        class _Request:
            method = "POST"
            scheme = "http"
            cookies: dict[str, str] = {}

        request = _Request()
        return request, {b"host": host, b"origin": origin}

    def test_a_forged_host_is_refused_when_hosts_are_named(self):
        from wreath.policy.csrf import CsrfPolicy

        middleware = CsrfPolicy("k" * 32, secure=False, trusted_hosts=["app.example"])
        request, headers = self._request(b"evil.example", b"http://evil.example")
        assert middleware._origin_valid(request, headers) is False

    def test_the_configured_host_still_passes(self):
        from wreath.policy.csrf import CsrfPolicy

        middleware = CsrfPolicy("k" * 32, secure=False, trusted_hosts=["app.example"])
        request, headers = self._request(b"app.example", b"http://app.example")
        assert middleware._origin_valid(request, headers) is True

    def test_a_port_is_matched_as_written(self):
        from wreath.policy.csrf import CsrfPolicy

        middleware = CsrfPolicy("k" * 32, secure=False, trusted_hosts=["app.example:8000"])
        request, headers = self._request(b"app.example:8000", b"http://app.example:8000")
        assert middleware._origin_valid(request, headers) is True

    def test_without_the_list_the_behaviour_is_unchanged(self):
        from wreath.policy.csrf import CsrfPolicy

        middleware = CsrfPolicy("k" * 32, secure=False)
        request, headers = self._request(b"evil.example", b"http://evil.example")
        # Still passes -- the Host header is trusted, which is why the guide
        # tells you to mount TrustedHostPolicy. Pinned so the default
        # cannot change silently.
        assert middleware._origin_valid(request, headers) is True
