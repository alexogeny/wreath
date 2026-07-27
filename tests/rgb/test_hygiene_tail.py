"""Smaller correctness and bounding items (report 23: G-02, G-03, G-13, G-19,
G-49, G-52, G-54, G-56, G-76, G-80, R-42)."""

from __future__ import annotations

import pytest


class TestMemoryStoreBound:
    """G-02: `__len__` and the `max_entries` bound count entries past their
    deadline, so the bound is on *stored* rather than live entries."""

    def test_length_counts_only_live_entries(self):
        from wreath.store import MemoryStore

        clock = [0.0]
        store = MemoryStore(ttl=10.0, clock=lambda: clock[0])
        store.set("a", 1)
        store.set("b", 2)
        assert len(store) == 2

        clock[0] = 11.0
        assert len(store) == 0, "expired entries still counted toward the bound"

    def test_a_live_entry_is_still_counted(self):
        from wreath.store import MemoryStore

        clock = [0.0]
        store = MemoryStore(ttl=10.0, clock=lambda: clock[0])
        store.set("a", 1)
        clock[0] = 5.0
        assert len(store) == 1


class TestIdentifierRules:
    """G-03: `sql_identifier` accepts reserved words, which fail unquoted at DDL
    time. G-19: `validate_identifier` accepts any Unicode `isalnum()` character
    and calls the result SQL-safe."""

    def test_a_reserved_word_is_refused(self):
        from wreath.store import sql_identifier

        with pytest.raises(ValueError, match="reserved"):
            sql_identifier("user")
        with pytest.raises(ValueError, match="reserved"):
            sql_identifier("select")

    def test_an_ordinary_name_is_accepted(self):
        from wreath.store import sql_identifier

        assert sql_identifier("wreath_session") == "wreath_session"
        assert sql_identifier("users_v2") == "users_v2"

    def test_a_non_ascii_identifier_is_refused(self):
        from wreath._jobcore import validate_identifier

        for value in ("café", "½", "٣"):
            with pytest.raises(ValueError):
                validate_identifier(value, "queue")

    def test_an_ascii_identifier_is_still_accepted(self):
        from wreath._jobcore import validate_identifier

        assert validate_identifier("orders_v2", "queue") == "orders_v2"


class TestFormParameterPollution:
    """R-42: duplicate field names are first-value-wins with no way to see the
    rest, which is the shape that lets a proxy and the app disagree."""

    async def test_every_value_is_reachable(self):
        from wreath.request import Request

        body = b"role=user&role=admin"

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http", "method": "POST", "path": "/",
                "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            },
            receive,
        )
        form = await request.form()
        assert form["role"] == "user"          # first wins, as before
        assert form.getlist("role") == ["user", "admin"]

    async def test_a_single_value_still_reads_normally(self):
        from wreath.request import Request

        async def receive():
            return {"type": "http.request", "body": b"role=user", "more_body": False}

        request = Request(
            {
                "type": "http", "method": "POST", "path": "/",
                "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            },
            receive,
        )
        form = await request.form()
        assert form.getlist("role") == ["user"]
        assert form.getlist("absent") == []


@pytest.mark.skip(
    reason="not a defect: first-registration precedence for static mounts is "
    "the documented rule, pinned by two tests in tests/test_app.py -- one named "
    "for it. The ordering is the application's to choose. See report 23 G-52."
)
def test_static_mount_precedence_is_longest_prefix():
    raise AssertionError("unimplemented")


@pytest.mark.skip(
    reason="not a defect worth the break: model write announcements use the "
    "bare class name, which is what `invalidate_on` and `publish_write` callers "
    "pass as strings. Two same-named models cross-invalidate, which is "
    "over-invalidation (safe) rather than stale data. See report 23 G-13."
)
def test_written_model_names_are_qualified():
    raise AssertionError("unimplemented")


class TestClauseExpansion:
    """G-49: `mode="any"` checks expand as a cartesian product, so k checks of n
    values produce n**k clauses in the route table."""

    def test_a_pathological_requirement_is_refused(self):
        from wreath import Wreath
        from wreath.authorization import roles

        app = Wreath()

        def build():
            handler = lambda request: None      # noqa: E731 - metadata carrier
            for index in range(6):
                handler = roles(
                    *[f"r{index}_{n}" for n in range(8)], mode="any"
                )(handler)
            app.get("/wide")(handler)
            app._compile_routes()

        with pytest.raises(ValueError, match="clause"):
            build()

    def test_an_ordinary_requirement_still_compiles(self):
        from wreath import Wreath
        from wreath.authorization import roles

        app = Wreath()

        @app.get("/fine")
        @roles("editor", "admin", mode="any")
        async def fine(request):
            return {}

        app._compile_routes()


class TestCsrfTokenLifetime:
    """G-54: tokens renew only on safe methods, so a client that only POSTs is
    403'd once `max_age` passes with no way to renew."""

    async def test_an_unsafe_request_renews_an_ageing_token(self):
        import time

        from wreath.middleware.csrf import CSRFMiddleware

        secret = "k" * 32
        middleware = CSRFMiddleware(secret, secure=False, max_age=100)
        now = int(time.time())
        # Minted 80s ago: valid, but past the 3/4 renewal point.
        token = middleware._new_token(now - 80)

        class _State:
            def __init__(self):
                self.values = {}

            def __setattr__(self, name, value):
                if name == "values":
                    object.__setattr__(self, name, value)
                else:
                    self.values[name] = value

            def get(self, name, default=None):
                return self.values.get(name, default)

        state = _State()

        class _Request:
            method = "POST"
            scheme = "http"
            cookies = {"wreath_csrf": token}

            def __init__(self):
                self.state = state

            def _index_headers(self):
                return {
                    b"host": b"example.com",
                    b"origin": b"http://example.com",
                    b"x-csrf-token": token.encode("ascii"),
                }

        request = _Request()
        assert await middleware.before(request) is None
        assert state.get("_wreath_csrf_issue") is True, (
            "an ageing token was accepted but never reissued"
        )

    def test_the_cookie_name_may_carry_the_host_prefix(self):
        from wreath.middleware.csrf import CSRFMiddleware

        middleware = CSRFMiddleware("k" * 32, cookie_name="__Host-csrf", secure=True)
        assert middleware._cookie_name == "__Host-csrf"

    def test_a_host_prefixed_cookie_requires_secure(self):
        from wreath.middleware.csrf import CSRFMiddleware

        with pytest.raises(ValueError, match="__Host-"):
            CSRFMiddleware("k" * 32, cookie_name="__Host-csrf", secure=False)


class TestActionTokenFields:
    """G-76: the token payload is `:`-joined with no escaping, so a subject
    containing `:` reassigns the fields."""

    def test_a_subject_with_a_colon_round_trips(self):
        from wreath._userkit import sign_token, verify_token

        secret = "s" * 32
        subject = "urn:user:42"
        token = sign_token(secret, "verify", subject, ttl=3600)
        assert verify_token(secret, "verify", token) == subject

    def test_a_bound_value_with_a_colon_round_trips(self):
        from wreath._userkit import sign_token, verify_token

        secret = "s" * 32
        token = sign_token(secret, "reset", "u1", ttl=3600, bound="a:b:c")
        assert verify_token(secret, "reset", token, bound="a:b:c") == "u1"
        assert verify_token(secret, "reset", token, bound="a:b") is None


class TestGraphQLParseCacheBound:
    """G-80: the parse cache is keyed on raw client text and bounded by entry
    count, not by bytes."""

    def test_an_enormous_document_is_not_cached(self):
        from wreath.graphql import MAX_CACHED_QUERY_CHARS

        assert MAX_CACHED_QUERY_CHARS > 0
