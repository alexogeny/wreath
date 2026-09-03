from __future__ import annotations

import asyncio

import pytest

from wreath._orm_events import publish_write
from wreath.response import Response
from wreath.response_cache import TAG_HEADERS, CDNPurge, Tags, cached

SECRET = b"a-deployment-secret"


class Report:
    pass


class Invoice:
    pass


class FakeQueue:
    """Records what `enqueue` was called with, or fails on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, tuple, str | None]] = []
        self.fail = fail

    async def enqueue(self, task, *args, key=None, **kwargs):
        if self.fail:
            raise RuntimeError("job queue is not running")
        self.calls.append((task, args, key))
        return len(self.calls)


class BlockingQueue(FakeQueue):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started = 0

    async def enqueue(self, task, *args, key=None, **kwargs):
        self.started += 1
        await self.release.wait()
        return await super().enqueue(task, *args, key=key, **kwargs)


class FakeRequest:
    def __init__(self, path: str = "/reports", method: str = "GET") -> None:
        self.path = path
        self.method = method
        self.query_string = b""
        self.identity = None


def tag_values(response: Response) -> dict[bytes, bytes]:
    return {name: value for name, value in response.headers if name in TAG_HEADERS}


@pytest.fixture(autouse=True)
def _drop_subscriptions():
    """A `CDNPurge` registers on the process-global write signal."""
    registered: list[CDNPurge] = []
    yield registered
    for purge in registered:
        purge.close()


def test_a_secret_is_required_and_says_why():
    with pytest.raises(ValueError) as caught:
        Tags(secret=b"")
    assert "purge that tag" in str(caught.value)


def test_a_str_secret_is_refused_rather_than_encoded():
    with pytest.raises(TypeError, match="must be bytes"):
        Tags(secret="a-deployment-secret")  # ty: ignore[invalid-argument-type]


def test_a_key_does_not_contain_the_model_name():
    key = Tags(secret=SECRET).key(Report)

    assert "Report" not in key
    assert "report" not in key.lower()


def test_a_key_is_not_computable_without_the_secret():
    assert Tags(secret=SECRET).key(Report) != Tags(secret=b"other").key(Report)


def test_a_prefix_separates_two_applications_on_one_cdn():
    plain = Tags(secret=SECRET)
    prefixed = Tags(secret=SECRET, prefix="staging")

    assert plain.key(Report) != prefixed.key(Report)


def test_keys_are_sorted_and_deduplicated():
    tags = Tags(secret=SECRET)

    keys = tags.keys([Report, Invoice, Report, "Report"])

    assert keys == tuple(sorted(keys))
    assert len(keys) == 2


def test_a_model_may_be_named_as_a_string():
    tags = Tags(secret=SECRET)
    assert tags.key(Report) == tags.key("Report")


def test_the_header_value_is_space_separated():
    value = Tags(secret=SECRET).header_value([Report, Invoice])

    assert value.count(b" ") == 1
    assert all(part.isalnum() for part in value.split(b" "))


async def test_a_cached_response_carries_every_tag_header():
    tags = Tags(secret=SECRET)

    @cached(invalidate_on=[Report], tags=tags)
    async def handler(request):
        return Response(b"body")

    response = await handler(FakeRequest())

    present = tag_values(response)
    assert set(present) == set(TAG_HEADERS)
    assert set(present.values()) == {tags.header_value([Report])}
    groups = [value for name, value in response.headers if name == b"cache-groups"]
    expected = b'"' + tags.key(Report).encode("ascii") + b'"'
    assert groups == [expected]


async def test_an_unsafe_response_invalidates_only_its_declared_cache_groups():
    tags = Tags(secret=SECRET)

    @cached(invalidate_on=[Report, Invoice], tags=tags)
    async def handler(request):
        return Response(b"updated")

    response = await handler(FakeRequest(method="POST"))

    values = [value for name, value in response.headers if name == b"cache-group-invalidation"]
    expected = b", ".join(b'"' + key.encode("ascii") + b'"' for key in tags.keys([Report, Invoice]))
    assert values == [expected]
    assert not [value for name, value in response.headers if name == b"cache-groups"]


async def test_a_safe_uncached_method_does_not_emit_group_invalidation():
    @cached(invalidate_on=[Report], tags=Tags(secret=SECRET))
    async def handler(request):
        return Response(b"metadata")

    response = await handler(FakeRequest(method="HEAD"))

    assert not [value for name, value in response.headers if name == b"cache-group-invalidation"]


async def test_a_cache_hit_is_tagged_the_same_as_the_miss_that_filled_it():
    tags = Tags(secret=SECRET)

    @cached(invalidate_on=[Report], tags=tags)
    async def handler(request):
        return Response(b"body")

    miss = await handler(FakeRequest())
    hit = await handler(FakeRequest())

    assert handler.cache_store.stats.hits == 1
    assert tag_values(hit) == tag_values(miss)


async def test_tags_without_invalidate_on_is_refused():
    tags = Tags(secret=SECRET)

    with pytest.raises(ValueError) as caught:

        @cached(tags=tags)
        async def handler(request):
            return Response(b"body")

    assert "no purge could ever reach this response" in str(caught.value)


async def test_a_handler_returning_a_dict_is_left_alone():

    @cached(invalidate_on=[Report], tags=Tags(secret=SECRET))
    async def handler(request):
        return {"rows": []}

    assert await handler(FakeRequest()) == {"rows": []}


async def test_a_reused_response_object_does_not_accumulate_tags():
    tags = Tags(secret=SECRET)
    shared = Response(b"body")

    @cached(ttl=None, max_entries=1, invalidate_on=[Report], tags=tags)
    async def handler(request):
        return shared

    for index in range(4):
        # A distinct key each time, so every call is a miss that re-tags the
        # same object rather than being served from the store.
        await handler(FakeRequest(path=f"/reports/{index}"))

    for name in TAG_HEADERS:
        assert [n for n, _ in shared.headers].count(name) == 1


async def test_an_untagged_cached_handler_emits_no_tag_headers():
    @cached(invalidate_on=[Report])
    async def handler(request):
        return Response(b"body")

    assert tag_values(await handler(FakeRequest())) == {}


async def test_a_handlers_own_cache_tag_is_kept_beside_the_derived_one():
    tags = Tags(secret=SECRET)

    @cached(invalidate_on=[Report], tags=tags)
    async def handler(request):
        return Response(b"body", headers=[(b"cache-tag", b"hand-written")])

    response = await handler(FakeRequest())

    values = [value for name, value in response.headers if name == b"cache-tag"]
    assert b"hand-written" in values
    assert tags.header_value([Report]) in values


async def test_a_write_enqueues_a_purge_for_the_watched_model(_drop_subscriptions):
    queue = FakeQueue()
    tags = Tags(secret=SECRET)
    purge = CDNPurge(queue, tags=tags)
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    publish_write(frozenset({"Report"}))
    await asyncio.sleep(0)

    assert queue.calls == [("wreath_cdn_purge", (tags.key(Report),), f"purge:{tags.key(Report)}")]
    assert purge.enqueued() == 1
    assert purge.dropped() == 0


async def test_close_stops_watching_for_writes(_drop_subscriptions):
    queue = FakeQueue()
    purge = CDNPurge(queue, tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    purge.close()
    publish_write(frozenset({"Report"}))
    await asyncio.sleep(0)

    assert queue.calls == []
    assert purge.watching == set()


def test_a_closed_purger_cannot_be_watched_again(_drop_subscriptions):
    purge = CDNPurge(FakeQueue(), tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    purge.close()

    with pytest.raises(RuntimeError, match="purger was closed"):
        purge.watch(Report)


def test_watching_no_models_does_not_subscribe(_drop_subscriptions):
    purge = CDNPurge(FakeQueue(), tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)

    purge.watch()

    assert purge.watching == frozenset()
    assert purge._subscribed is False


async def test_unwatch_keeps_other_models_subscribed(_drop_subscriptions):
    queue = FakeQueue()
    tags = Tags(secret=SECRET)
    purge = CDNPurge(queue, tags=tags)
    _drop_subscriptions.append(purge)
    purge.watch(Report, Invoice)

    purge.unwatch(Report)
    publish_write(frozenset({"Report", "Invoice"}))
    await asyncio.sleep(0)

    assert queue.calls == [("wreath_cdn_purge", (tags.key(Invoice),), f"purge:{tags.key(Invoice)}")]
    assert purge.watching == {"Invoice"}


async def test_a_blocked_enqueue_coalesces_repeated_writes(_drop_subscriptions):
    queue = BlockingQueue()
    tags = Tags(secret=SECRET)
    purge = CDNPurge(queue, tags=tags)
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    for _ in range(20):
        publish_write(frozenset({"Report"}))
    await asyncio.sleep(0)

    assert queue.started == 1
    queue.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert queue.calls == [("wreath_cdn_purge", (tags.key(Report),), f"purge:{tags.key(Report)}")]


async def test_work_arriving_between_runner_exit_and_done_callback_is_drained(
    _drop_subscriptions,
):
    queue = FakeQueue()
    tags = Tags(secret=SECRET)
    purge = CDNPurge(queue, tags=tags)
    _drop_subscriptions.append(purge)
    purge.watch(Report, Invoice)

    publish_write(frozenset({"Report"}))
    asyncio.get_running_loop().call_soon(publish_write, frozenset({"Invoice"}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert {call[1][0] for call in queue.calls} == {tags.key(Report), tags.key(Invoice)}


async def test_a_cancelled_runner_does_not_restart_during_loop_shutdown(_drop_subscriptions):
    queue = BlockingQueue()
    purge = CDNPurge(queue, tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    purge.watch(Report, Invoice)

    publish_write(frozenset({"Report", "Invoice"}))
    await asyncio.sleep(0)
    runner = purge._runner
    assert runner is not None

    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    await asyncio.sleep(0)

    assert purge._runner is None
    assert purge.dropped() == 2


async def test_an_unwatched_model_enqueues_nothing(_drop_subscriptions):
    queue = FakeQueue()
    purge = CDNPurge(queue, tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    publish_write(frozenset({"Invoice"}))
    await asyncio.sleep(0)

    assert queue.calls == []


async def test_the_dedup_key_makes_a_burst_of_writes_one_purge(_drop_subscriptions):
    queue = FakeQueue()
    tags = Tags(secret=SECRET)
    purge = CDNPurge(queue, tags=tags)
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    for _ in range(5):
        publish_write(frozenset({"Report"}))
    await asyncio.sleep(0)

    assert {call[2] for call in queue.calls} == {f"purge:{tags.key(Report)}"}


async def test_start_runner_needs_pending_work(_drop_subscriptions):
    purge = CDNPurge(FakeQueue(), tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)

    purge._start_runner()

    assert purge._runner is None


async def test_start_runner_does_not_replace_a_live_runner(_drop_subscriptions):
    queue = BlockingQueue()
    purge = CDNPurge(queue, tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    purge._pending.add("tag")
    purge._start_runner()
    runner = purge._runner

    purge._start_runner()

    assert purge._runner is runner
    queue.release.set()
    assert runner is not None
    await runner


async def test_a_stale_done_callback_does_not_clear_the_live_runner(_drop_subscriptions):
    purge = CDNPurge(FakeQueue(), tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    live = asyncio.current_task()
    stale = asyncio.create_task(asyncio.sleep(0))
    await stale
    purge._runner = live

    purge._runner_done(stale)

    assert purge._runner is live


async def test_a_purge_is_enqueued_and_not_awaited_on_the_write(_drop_subscriptions):
    queue = FakeQueue()
    purge = CDNPurge(queue, tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    publish_write(frozenset({"Report"}))

    # Nothing has run yet: `publish_write` returned before the enqueue.
    assert queue.calls == []
    await asyncio.sleep(0)
    assert len(queue.calls) == 1


async def test_a_failed_enqueue_is_counted_rather_than_raised(_drop_subscriptions):
    queue = FakeQueue(fail=True)
    purge = CDNPurge(queue, tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    publish_write(frozenset({"Report"}))  # must not raise
    await asyncio.sleep(0)

    assert purge.dropped() == 1
    assert purge.enqueued() == 0


def test_a_purge_with_no_running_loop_is_counted(_drop_subscriptions):
    queue = FakeQueue()
    purge = CDNPurge(queue, tags=Tags(secret=SECRET))
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    publish_write(frozenset({"Report"}))

    assert purge.dropped() == 1
    assert queue.calls == []


async def test_watching_several_models_purges_each_tag(_drop_subscriptions):
    queue = FakeQueue()
    tags = Tags(secret=SECRET)
    purge = CDNPurge(queue, tags=tags)
    _drop_subscriptions.append(purge)
    purge.watch(Report, Invoice)

    publish_write(frozenset({"Report", "Invoice"}))
    await asyncio.sleep(0)

    assert {call[1][0] for call in queue.calls} == {tags.key(Report), tags.key(Invoice)}
    assert purge.watching == {"Report", "Invoice"}


async def test_the_purge_tag_matches_the_tag_on_the_response(_drop_subscriptions):
    tags = Tags(secret=SECRET)
    queue = FakeQueue()
    purge = CDNPurge(queue, tags=tags)
    _drop_subscriptions.append(purge)
    purge.watch(Report)

    @cached(invalidate_on=[Report], tags=tags)
    async def handler(request):
        return Response(b"body")

    response = await handler(FakeRequest())
    served = tag_values(response)[b"cache-tag"]

    publish_write(frozenset({"Report"}))
    await asyncio.sleep(0)
    purged = queue.calls[0][1][0].encode("ascii")

    assert purged in served.split(b" ")
    # ... and the local cache dropped its entry from the same signal.
    assert handler.cache_store.stats.size == 0
