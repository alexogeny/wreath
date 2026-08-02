"""Task-progress exposure and the permissions endpoints (report 23: R-28, R-30,
R-33, R-34, G-24, G-25, G-27, G-28, G-29, G-31)."""

from __future__ import annotations

import asyncio

import pytest

from wreath.progress import ProgressRegistry, progress_stream, status_response


class TestProgressAuthorization:
    """R-28: `status_response`/`progress_stream` carry no authorization, and
    `jobs.launch` makes the task id the *sequential job id* -- so counting
    integers reads every task's state, message, and error text."""

    def test_status_refuses_when_the_guard_says_no(self):
        registry = ProgressRegistry()
        registry.report("7", 50, "halfway")

        response = status_response(registry, "7", authorize=lambda task_id: False)
        assert response.status == 404          # not 403; see the test below
        assert b"halfway" not in response.body

    def test_status_answers_when_the_guard_says_yes(self):
        registry = ProgressRegistry()
        registry.report("7", 50, "halfway")

        response = status_response(registry, "7", authorize=lambda task_id: True)
        assert response.status == 200
        assert b"halfway" in response.body

    def test_an_unknown_task_is_indistinguishable_from_a_refused_one(self):
        """Otherwise the 404/403 split is itself an oracle for which task ids
        exist, which is most of what enumeration wants."""
        registry = ProgressRegistry()
        registry.report("7", 50, "halfway")

        refused = status_response(registry, "7", authorize=lambda task_id: False)
        missing = status_response(registry, "9", authorize=lambda task_id: False)
        assert refused.status == missing.status

    def test_a_stream_refuses_the_same_way(self):
        registry = ProgressRegistry()
        registry.report("7", 50, "halfway")
        response = progress_stream(registry, "7", authorize=lambda task_id: False)
        assert response.status == 404

    def test_omitting_the_guard_still_works_for_a_trusted_caller(self):
        registry = ProgressRegistry()
        registry.report("7", 50, "halfway")
        assert status_response(registry, "7").status == 200


class TestProgressBusTrust:
    """R-30: `_apply` accepts any bus payload and overwrites any task id, with
    no check beyond the echo guard."""

    async def test_a_malformed_payload_is_ignored(self):
        registry = ProgressRegistry()
        registry.report("7", 50, "mine")
        await registry._apply({"task_id": "7", "percent": "not a number"})
        assert registry.get("7").message == "mine"

    async def test_an_oversized_message_is_bounded(self):
        registry = ProgressRegistry()
        await registry._apply({"task_id": "7", "percent": 1, "message": "x" * 100_000})
        stored = registry.get("7")
        assert stored is not None and len(stored.message) <= 4096


class TestProgressLifetimes:
    """G-24: the default TTL is an hour, so a job longer than that loses its
    entry mid-stream and the stream *returns* -- the client sees a clean end.
    G-25: no maximum stream duration."""

    def test_the_default_ttl_outlasts_a_long_job(self):
        registry = ProgressRegistry()
        assert registry._store.ttl is None or registry._store.ttl >= 24 * 3600

    async def test_a_stream_stops_at_its_maximum_duration(self):
        registry = ProgressRegistry()
        registry.report("7", 1, "working")

        seen = []
        async with asyncio.timeout(2):
            async for item in registry.stream("7", interval=0.01, max_duration=0.05):
                seen.append(item)
        # It ended on its own, without the task ever reaching a terminal state.
        assert registry.get("7").state == "running"


class TestPermissionsResponses:
    """G-27: `_private` appends `cache-control` even when the handler set one,
    leaving two headers and letting a proxy read the first. G-28: no
    `Vary: Authorization` on the per-principal manifest."""

    def test_a_single_cache_control_survives(self):
        from wreath._auth.permissions import _private
        from wreath.response import JSONResponse

        response = JSONResponse({"a": 1})
        response.headers.append((b"cache-control", b"public, max-age=60"))
        _private(response)
        values = [v for name, v in response.headers if name.lower() == b"cache-control"]
        assert values == [b"private, no-store"]

    def test_the_manifest_varies_on_authorization(self):
        from wreath._auth.permissions import _private
        from wreath.response import JSONResponse

        response = _private(JSONResponse({"a": 1}))
        assert any(
            name.lower() == b"vary" and b"authorization" in value.lower()
            for name, value in response.headers
        )


class TestManifestEtagComparison:
    """R-33/G-30-shaped: `if-none-match` is compared with `==`, so a client
    sending a list revalidates the whole manifest every time."""

    def test_a_list_of_tags_matches(self):
        from wreath._auth.permissions import _etag_matches

        assert _etag_matches('W/"aaa", W/"bbb"', 'W/"bbb"')
        assert _etag_matches("*", 'W/"bbb"')
        assert not _etag_matches('W/"aaa"', 'W/"bbb"')
        assert not _etag_matches(None, 'W/"bbb"')


class TestBatchIdentifiers:
    """R-34: `str(identifier)` collapses `1` and `"1"` into one key, so a client
    asking about both silently gets fewer answers than ids."""

    def test_duplicate_stringified_ids_are_refused(self):
        from wreath._auth.permissions import _distinct_identifiers

        assert _distinct_identifiers([1, 2]) == ["1", "2"]
        with pytest.raises(ValueError):
            _distinct_identifiers([1, "1"])


class TestInstanceTokenBound:
    """G-29: `_PINNED_TOKENS` keys on `id(engine)` and holds the engine forever,
    so repeated reloads leak one engine each."""

    def test_the_pinned_token_table_is_bounded(self):
        from wreath._auth import permissions

        class _Slotted:
            __slots__ = ()

        for _ in range(2000):
            permissions._instance_token(_Slotted())
        assert len(permissions._PINNED_TOKENS) <= 1024, (
            "the fallback token table grows one engine per instance, forever"
        )
