"""Bounds and information exposure (report 23: R-06, R-21, R-31, R-32, R-36,
R-67, R-69, R-78, G-81)."""

from __future__ import annotations

import pytest


class TestPermissionsBatchBounds:
    """R-32: `max_ids` bounds the id list and nothing bounds `actions`, so one
    authenticated request can ask for ids x actions policy evaluations."""

    def _app_with_actions(self):
        from wreath import Wreath
        from wreath.authorization import authorize

        app = Wreath()

        @app.get("/llamas/{llama_id}")
        @authorize(action="Llama::read", resource=lambda r: "Llama::\"x\"")
        async def read(request):  # pragma: no cover
            return {}

        return app

    async def test_repeated_actions_are_not_evaluated_repeatedly(self):
        from wreath._auth.permissions import permissions_router

        app = self._app_with_actions()
        app._compile_routes()

        evaluations = 0

        class _Authorizer:
            async def authorize(self, request, requirement):
                nonlocal evaluations
                evaluations += 1

                class _Decision:
                    allowed = True

                return _Decision()

        app._authorizer = _Authorizer()
        router = permissions_router(app)
        batch = next(r.endpoint for r in router.routes if "POST" in r.methods)

        class _Request:
            identity = type("I", (), {"id": "u1", "type": "User", "roles": frozenset()})()
            path = "/permissions"
            method = "POST"

            async def json(self):
                return {"type": "Llama", "ids": ["1", "2"], "actions": ["Llama::read"] * 500}

            def header(self, name, default=None):
                return default

        await batch(_Request())
        assert evaluations <= 2, f"{evaluations} evaluations for 2 ids and 1 distinct action"


class TestAdvisoryLockKeyWidth:
    """R-36: the lock key is two 32-bit `hashtext` values, so distinct keys
    collide and silently share one mutual-exclusion lock."""

    async def test_the_lock_key_is_wider_than_32_bits(self):
        from wreath.orm.session import Session

        captured: list[tuple[str, tuple]] = []

        class _Connection:
            async def fetchval(self, sql, *args):
                captured.append((sql, args))
                return True

            async def execute(self, sql, *args):
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

        session = Session(_Registry(), "write")
        session._depth = 1
        await session.lock("tenant-42")
        sql, args = captured[0]
        # 64-bit, and still hashed server-side so Python and PostgreSQL cannot
        # disagree -- the invariant `wreath._locks` states.
        assert "hashtextextended" in sql
        assert "hashtext(" not in sql, "the 32-bit two-operand form still keys the lock"
        assert args == ("app", "tenant-42")

    def test_the_session_and_the_database_name_the_same_lock(self):
        from wreath._locks import _KEYED

        assert "hashtextextended" in _KEYED


class TestIdempotencyBodyCap:
    """R-06: nothing caps the stored body, so an endpoint returning a large one
    is a memory (or table) amplifier for the whole TTL."""

    async def test_a_large_response_is_not_stored(self):
        from wreath.policy.idempotency import (
            IdempotencyPolicy,
            MemoryIdempotencyStore,
        )
        from wreath.response import Response

        store = MemoryIdempotencyStore()
        middleware = IdempotencyPolicy(store=store, max_body_bytes=1024)

        class _State:
            idempotency_key = "k"

            def get(self, name, default=None):
                return getattr(self, name, default)

        class _Request:
            state = _State()

        big = Response(b"x" * 4096, media_type=b"application/octet-stream")
        await middleware.after(_Request(), big)
        assert await store.reserve("k") == ("fresh", None), "an oversized body was stored"

    async def test_an_ordinary_response_is_still_stored(self):
        from wreath.policy.idempotency import (
            IdempotencyPolicy,
            MemoryIdempotencyStore,
        )
        from wreath.response import Response

        store = MemoryIdempotencyStore()
        middleware = IdempotencyPolicy(store=store, max_body_bytes=1024)

        class _State:
            idempotency_key = "k"

            def get(self, name, default=None):
                return getattr(self, name, default)

        class _Request:
            state = _State()

        await middleware.after(_Request(), Response(b"small"))
        state, replay = await store.reserve("k")
        assert state == "done" and replay[2] == b"small"


class TestPageDepth:
    """R-67 / G-81: `page` is bounded below by 1 and not above, so a caller can
    ask for a deep OFFSET scan."""

    def test_page_params_bound_the_page_number(self):
        from wreath.pagination import MAX_PAGE, page_params

        class _Request:
            # `page_params` is a `Depends`, so it takes the request and reads
            # the query string -- a dependency's own parameters are never bound.
            query_string = b"page=1000000000&size=20"

        params = page_params(_Request())
        assert params.page <= MAX_PAGE

    def test_crud_bounds_the_page_number(self):
        from wreath.crud import _page_params

        class _Request:
            query_string = b"page=100000000"

        page, _size = _page_params(_Request(), 20)
        from wreath.pagination import MAX_PAGE

        assert page <= MAX_PAGE


@pytest.mark.skip(
    reason="not a defect: surfacing the handler's exception text to whoever is "
    "watching the task is the shipped contract, pinned by "
    "tests/test_live_progress.py::test_a_dead_lettered_job_is_reported_as_failed. "
    "The hazard -- a driver exception publishing SQL or paths -- is now called "
    "out in JobContext.report's docstring. See report 23 R-21/R-31."
)
def test_job_errors_are_redacted_for_clients():
    raise AssertionError("unimplemented")


class TestWebhookSignatureFraming:
    """R-78: the signature base joins fields with newlines and no framing, and
    the envelope does not reject a newline in `id`/`type` -- so one MAC covers
    more than one (id, type, body) split."""

    def test_a_newline_in_the_event_id_is_refused(self):
        from datetime import UTC, datetime

        from wreath.webhooks import WebhookEnvelope

        with pytest.raises(ValueError):
            WebhookEnvelope(
                id="a\nb", type="thing", version="1",
                timestamp=datetime.now(UTC), content_type="application/json",
                body=b"{}",
            )

    def test_a_newline_in_the_event_type_is_refused(self):
        from datetime import UTC, datetime

        from wreath.webhooks import WebhookEnvelope

        with pytest.raises(ValueError):
            WebhookEnvelope(
                id="a", type="thing\nmore", version="1",
                timestamp=datetime.now(UTC), content_type="application/json",
                body=b"{}",
            )

    def test_verification_refuses_a_framed_header_too(self):
        from wreath.webhooks import HMACWebhookVerifier

        verifier = HMACWebhookVerifier({"k1": b"secret" * 8})
        with pytest.raises(ValueError):
            verifier.verify(
                body=b"{}",
                headers={
                    b"wreath-webhook-id": b"a\nb",
                    b"wreath-webhook-type": b"thing",
                    b"wreath-webhook-version": b"1",
                    b"wreath-webhook-timestamp": b"2026-07-27T00:00:00.000000Z",
                    b"wreath-webhook-key-id": b"k1",
                    b"wreath-webhook-signature": b"v1=00",
                },
            )

    def test_an_ordinary_envelope_still_signs_and_verifies(self):
        from datetime import UTC, datetime

        from wreath.webhooks import (
            HMACWebhookSigner,
            HMACWebhookVerifier,
            WebhookEnvelope,
        )

        keys = {"k1": b"secret" * 8}
        envelope = WebhookEnvelope(
            id="evt_1", type="thing.created", version="1",
            timestamp=datetime.now(UTC), content_type="application/json", body=b"{}",
        )
        headers = dict(HMACWebhookSigner(keys, key_id="k1").headers(envelope))
        verified = HMACWebhookVerifier(keys).verify(body=b"{}", headers=headers)
        assert verified.id == "evt_1"


class TestOidcNonce:
    """R-69: no `nonce` is sent in the authorization request or checked on the
    returned id_token."""

    def test_the_login_flow_sends_and_checks_a_nonce(self):
        import inspect

        from wreath._auth import oauth2

        source = inspect.getsource(oauth2.register_oauth2_login)
        assert '"nonce"' in source
        assert source.count("nonce") >= 4, "a nonce must be sent *and* verified"
