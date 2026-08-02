"""`wreath.kv` behaviour, on whichever arm the facade selected and on the pure one.

Both arms are driven directly rather than through the facade alone, because the
facade selects exactly one of them per process and a suite that only tested the
selection would leave the other completely unexercised on every machine.

The randomised cross-check at the bottom is the one that has already earned its
place: it found the native table failing to add its rebuild's expired count to
`expirations`, while every operation result and every other counter matched.
That is the shape of divergence nothing else catches -- the table behaves
correctly and only the number an operator reads is wrong.
"""

from __future__ import annotations

import random

import pytest

from wreath._pure.kv import KV as PureKV
from wreath.kv import KV, Stats, stats

ARMS = [PureKV]
if KV is not PureKV:
    ARMS.append(KV)

ARM_IDS = ["pure", "native"][: len(ARMS)]


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestBasics:
    def test_get_returns_the_default_for_an_absent_key(self, arm) -> None:
        table = arm(max_entries=4)
        assert table.get("nothing") is None
        assert table.get("nothing", "fallback") == "fallback"
        assert table.misses == 2
        assert table.hits == 0

    def test_set_then_get_round_trips_any_object(self, arm) -> None:
        table = arm(max_entries=4)
        payload = {"nested": [1, 2, 3]}
        table.set("key", payload)
        assert table.get("key") is payload
        assert table.hits == 1

    def test_keys_are_any_hashable_not_just_strings(self, arm) -> None:
        table = arm(max_entries=8)
        for key in (1, b"bytes", ("a", "tuple"), None, 3.5):
            table.set(key, repr(key))
        for key in (1, b"bytes", ("a", "tuple"), None, 3.5):
            assert table.get(key) == repr(key)

    def test_integer_keys_do_not_collapse_onto_one_tag(self, arm) -> None:
        # Python hashes a small int to itself, so the high bits a tag is cut
        # from are all zero without the mixing step. The table stays correct
        # either way; this pins that it stays correct.
        table = arm(max_entries=4096)
        for i in range(2000):
            table.set(i, i * 2)
        assert all(table.get(i) == i * 2 for i in range(2000))
        assert len(table) == 2000

    def test_delete_reports_whether_anything_went(self, arm) -> None:
        table = arm(max_entries=4)
        table.set("key", 1)
        assert table.delete("key") is True
        assert table.delete("key") is False
        assert table.get("key") is None

    def test_pop_removes_and_returns(self, arm) -> None:
        table = arm(max_entries=4)
        table.set("key", "value")
        assert table.pop("key") == "value"
        assert table.pop("key", "gone") == "gone"
        assert len(table) == 0

    def test_contains_and_len_agree(self, arm) -> None:
        table = arm(max_entries=8)
        table.set("a", 1)
        table.set("b", 2)
        assert "a" in table
        assert "z" not in table
        assert len(table) == 2

    def test_clear_drops_entries_but_keeps_counters(self, arm) -> None:
        table = arm(max_entries=8)
        table.set("a", 1)
        table.get("a")
        table.get("absent")
        table.clear()
        assert len(table) == 0
        assert table.get("a") is None
        # Counters describe the table's history; a test clearing between cases
        # still wants them, which is why clear() deliberately leaves them.
        assert table.hits == 1
        assert table.misses >= 1


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestEviction:
    def test_the_ceiling_holds(self, arm) -> None:
        table = arm(max_entries=8)
        for i in range(100):
            table.set(f"k{i}", i)
        assert len(table) == 8
        assert table.evictions == 92

    def test_eviction_is_least_recently_used_not_first_inserted(self, arm) -> None:
        table = arm(max_entries=3)
        table.set("old", 1)
        table.set("middle", 2)
        table.set("new", 3)
        # Reading "old" makes it the most recent, so "middle" is now the victim.
        assert table.get("old") == 1
        table.set("newest", 4)
        assert table.get("old") == 1
        assert table.get("middle") is None
        assert table.get("new") == 3
        assert table.get("newest") == 4

    def test_a_ceiling_of_one_holds_exactly_one(self, arm) -> None:
        table = arm(max_entries=1)
        table.set("a", 1)
        table.set("b", 2)
        assert len(table) == 1
        assert table.get("a") is None
        assert table.get("b") == 2

    def test_items_are_most_recently_used_first(self, arm) -> None:
        table = arm(max_entries=8)
        table.set("a", 1)
        table.set("b", 2)
        table.set("c", 3)
        table.get("a")
        assert [key for key, _ in table.items()] == ["a", "c", "b"]
        assert table.keys() == ["a", "c", "b"]
        assert table.values() == [1, 3, 2]

    def test_max_entries_must_be_positive(self, arm) -> None:
        with pytest.raises(ValueError, match="max_entries must be positive"):
            arm(max_entries=0)


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestExpiry:
    def test_an_entry_expires_at_its_deadline_and_not_before(self, arm) -> None:
        table = arm(max_entries=8, ttl=10.0)
        table.set("key", "value", now=100.0)
        assert table.get("key", now=109.999) == "value"
        assert table.get("key", now=110.0) is None
        assert table.expirations == 1

    def test_a_per_write_ttl_overrides_the_default(self, arm) -> None:
        table = arm(max_entries=8, ttl=1000.0)
        table.set("brief", "x", ttl=5.0, now=0.0)
        assert table.get("brief", now=6.0) is None

    def test_no_ttl_means_no_expiry(self, arm) -> None:
        table = arm(max_entries=8)
        table.set("key", "value", now=0.0)
        assert table.get("key", now=1e9) == "value"
        assert table.expirations == 0

    def test_writing_again_moves_the_deadline(self, arm) -> None:
        table = arm(max_entries=8, ttl=10.0)
        table.set("key", 1, now=0.0)
        table.set("key", 2, now=5.0)
        assert table.get("key", now=12.0) == 2

    def test_keep_deadline_refuses_to_extend_the_window(self, arm) -> None:
        # The rule a claim ledger needs: a holder that keeps writing must not be
        # able to hold its key forever.
        table = arm(max_entries=8, ttl=10.0)
        table.set("key", 1, now=0.0)
        table.set("key", 2, now=5.0, keep_deadline=True)
        table.set("key", 3, now=9.0, keep_deadline=True)
        assert table.get("key", now=9.5) == 3
        assert table.get("key", now=10.0) is None

    def test_keep_deadline_starts_a_fresh_window_once_the_old_one_is_gone(
        self, arm
    ) -> None:
        table = arm(max_entries=8, ttl=10.0)
        table.set("key", 1, now=0.0)
        table.set("key", 2, now=20.0, keep_deadline=True)
        assert table.get("key", now=25.0) == 2

    def test_len_counts_what_the_table_will_still_return(self, arm) -> None:
        # `len()` takes no `now`, so it reads the real monotonic clock. A table
        # written against an injected clock is therefore entirely expired as far
        # as `len()` is concerned, which is worth pinning rather than tripping
        # over: use `items(now=...)` to ask the question at an injected time.
        table = arm(max_entries=8, ttl=10.0)
        for i in range(4):
            table.set(f"k{i}", i, now=0.0)
        assert len(table.items(now=5.0)) == 4
        # Nothing has read them, so they still occupy slots -- but the table
        # would refuse every one, and the count has to say so.
        assert table.items(now=100.0) == []
        assert len(table) == 0

    def test_len_honours_a_real_deadline(self, arm) -> None:
        table = arm(max_entries=8, ttl=3600.0)
        for i in range(4):
            table.set(f"k{i}", i)
        assert len(table) == 4

    def test_purge_reclaims_without_being_read(self, arm) -> None:
        table = arm(max_entries=64, ttl=5.0)
        for i in range(10):
            table.set(f"k{i}", i, now=0.0)
        assert table.purge(now=10.0) == 10
        assert len(table) == 0
        assert table.expirations == 10

    def test_touch_extends_a_live_key_only(self, arm) -> None:
        table = arm(max_entries=8, ttl=10.0)
        table.set("key", 1, now=0.0)
        assert table.touch("key", now=5.0) is True
        assert table.get("key", now=12.0) == 1
        assert table.touch("absent", now=5.0) is False

    def test_ttl_must_be_positive(self, arm) -> None:
        with pytest.raises(ValueError, match="ttl must be positive"):
            arm(max_entries=8, ttl=0.0)
        table = arm(max_entries=8)
        with pytest.raises(ValueError, match="ttl must be positive"):
            table.set("key", 1, ttl=-1.0)


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestClaim:
    def test_the_first_claim_wins_and_the_second_is_told(self, arm) -> None:
        table = arm(max_entries=8, ttl=10.0)
        assert table.claim("key", now=0.0) is True
        assert table.claim("key", now=0.0) is False

    def test_a_claim_is_reclaimable_once_it_expires(self, arm) -> None:
        table = arm(max_entries=8, ttl=10.0)
        assert table.claim("key", now=0.0) is True
        assert table.claim("key", now=5.0) is False
        assert table.claim("key", now=11.0) is True

    def test_a_claim_carries_a_payload(self, arm) -> None:
        table = arm(max_entries=8, ttl=10.0)
        assert table.claim("key", "held-by-me", now=0.0) is True
        assert table.get("key", now=1.0) == "held-by-me"

    def test_claiming_respects_the_ceiling(self, arm) -> None:
        table = arm(max_entries=4)
        for i in range(20):
            assert table.claim(f"k{i}") is True
        assert len(table) == 4


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_a_key_whose_comparison_raises_propagates(arm) -> None:
    class Hostile:
        def __hash__(self) -> int:
            return 42

        def __eq__(self, other: object) -> bool:
            raise RuntimeError("comparison refused")

    table = arm(max_entries=8)

    class Quiet:
        def __hash__(self) -> int:
            return 42

        def __eq__(self, other: object) -> object:
            # NotImplemented rather than False, so Python falls through to the
            # reflected comparison. Returning False here would answer the
            # question outright and `Hostile.__eq__` would never run -- the test
            # would pass by never reaching the thing it is about.
            return NotImplemented

    table.set(Quiet(), "occupying the slot")
    with pytest.raises(RuntimeError, match="comparison refused"):
        table.get(Hostile())


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_colliding_hashes_stay_distinguishable(arm) -> None:
    # Everything lands in one group, so the probe has to walk past 32 lanes of
    # matching tags and tell the entries apart by key rather than by tag.
    class Collides:
        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

        def __hash__(self) -> int:
            return 7

        def __eq__(self, other: object) -> bool:
            return isinstance(other, Collides) and other.name == self.name

    table = arm(max_entries=256)
    keys = [Collides(f"n{i}") for i in range(200)]
    for i, key in enumerate(keys):
        table.set(key, i)
    assert len(table) == 200
    assert all(table.get(key) == i for i, key in enumerate(keys))
    for key in keys[:100]:
        assert table.delete(key) is True
    assert all(table.get(key) is None for key in keys[:100])
    assert all(table.get(key) == i + 100 for i, key in enumerate(keys[100:]))


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_now_is_accepted_positionally_on_both_arms(arm) -> None:
    """The wrappers pass `now` positionally, and one arm used to refuse it.

    `wreath.cache.BoundedCache` and `wreath.store.MemoryStore` call these four
    methods with `now` as a positional argument, deliberately: a keyword forces
    the C method to build an argument tuple and a keyword dict per call, which
    together cost more than the lookup they carry. The pure twin kept `now`
    keyword-only for a while, so every one of those calls raised `TypeError`
    under `WREATH_PURE=1` while the native-arm suite stayed entirely green.

    The parity suite could not catch it -- it calls both arms the same way -- so
    the calling *convention* needs a test of its own.
    """
    table = arm(max_entries=8, ttl=10.0)
    assert table.claim("key", "held", None, 0.0) is True
    assert table.get("key", "-", 1.0) == "held"
    assert table.peek("key", "-", 1.0) == "held"
    table.set("key", "written", None, 2.0, True)
    assert table.get("key", "-", 3.0) == "written"
    # keep_deadline was passed positionally too, so the window must not have moved.
    assert table.get("key", "-", 11.0) == "-"


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_an_injected_clock_reaches_both_arms_through_the_wrappers(arm) -> None:
    """The end of the same bug, from the caller's side."""
    from wreath.cache import BoundedCache

    class _Table(BoundedCache):
        __slots__ = ()

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._table = arm(max_entries=8, ttl=10.0)

    ticks = [0.0]
    cache = _Table(max_entries=8, ttl=10.0, clock=lambda: ticks[0])
    cache.set("key", "value")
    assert cache.get("key") == "value"
    assert "key" in cache
    assert len(cache) == 1
    ticks[0] = 10.0
    assert cache.get("key") is None
    assert "key" not in cache
    assert len(cache) == 0


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestByteBudget:
    def test_a_table_bounded_by_bytes_evicts_to_stay_under(self, arm) -> None:
        table = arm(max_entries=100, max_bytes=1000)
        for i in range(5):
            table.set(f"k{i}", i, cost=300)
        assert table.bytes == 900
        assert table.max_bytes == 1000
        assert len(table) == 3
        assert table.evictions == 2

    def test_the_least_recently_used_goes_first_under_the_byte_bound_too(
        self, arm
    ) -> None:
        table = arm(max_entries=100, max_bytes=1000)
        table.set("a", 1, cost=400)
        table.set("b", 2, cost=400)
        table.get("a")  # "b" is now the least recently used
        table.set("c", 3, cost=400)
        assert table.get("a") == 1
        assert table.get("b") is None
        assert table.get("c") == 3

    def test_an_entry_larger_than_the_whole_budget_leaves_the_table_empty(
        self, arm
    ) -> None:
        # Rather than sitting there over the bound. Both hand-written caches
        # this replaces behaved this way, because they evicted after inserting.
        table = arm(max_entries=100, max_bytes=100)
        table.set("huge", "x", cost=500)
        assert len(table) == 0
        assert table.bytes == 0

    def test_rewriting_a_key_replaces_its_cost_rather_than_adding_to_it(
        self, arm
    ) -> None:
        table = arm(max_entries=10, max_bytes=10_000)
        table.set("a", 1, cost=100)
        table.set("a", 2, cost=250)
        assert table.bytes == 250
        table.delete("a")
        assert table.bytes == 0

    def test_removals_of_every_kind_return_the_bytes(self, arm) -> None:
        table = arm(max_entries=10, max_bytes=10_000, ttl=10.0)
        table.set("a", 1, cost=100, now=0.0)
        table.set("b", 2, cost=100, now=0.0)
        table.set("c", 3, cost=100, now=0.0)
        assert table.bytes == 300
        table.pop("a", now=1.0)
        assert table.bytes == 200
        table.get("b", now=99.0)  # expires it
        assert table.bytes == 100
        table.clear()
        assert table.bytes == 0

    def test_no_byte_bound_means_costs_are_recorded_but_never_enforced(
        self, arm
    ) -> None:
        table = arm(max_entries=10)
        assert table.max_bytes is None
        for i in range(5):
            table.set(f"k{i}", i, cost=10**9)
        assert len(table) == 5

    def test_a_negative_cost_is_refused(self, arm) -> None:
        with pytest.raises(ValueError, match="cost cannot be negative"):
            arm(max_entries=4).set("k", 1, cost=-1)


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
class TestEvictionReporting:
    def test_evicted_entries_are_handed_back_once(self, arm) -> None:
        table = arm(max_entries=2, track_evictions=True)
        for i in range(5):
            table.set(f"k{i}", i)
        assert table.take_evicted() == [("k0", 0), ("k1", 1), ("k2", 2)]
        assert table.take_evicted() == []

    def test_an_untracked_table_reports_nothing_and_still_evicts(self, arm) -> None:
        table = arm(max_entries=2)
        for i in range(5):
            table.set(f"k{i}", i)
        assert table.take_evicted() == []
        assert table.evictions == 3

    def test_an_expiry_is_not_an_eviction(self, arm) -> None:
        # The distinction the caller depends on: it already knows about the
        # deletes and the expiries it caused. An eviction is the one the table
        # decided on its own, and the only one that can surprise a caller
        # holding something outside the table.
        table = arm(max_entries=8, ttl=10.0, track_evictions=True)
        table.set("a", 1, now=0.0)
        assert table.get("a", now=20.0) is None
        assert table.expirations == 1
        assert table.take_evicted() == []

    def test_a_delete_is_not_an_eviction(self, arm) -> None:
        table = arm(max_entries=8, track_evictions=True)
        table.set("a", 1)
        table.delete("a")
        table.pop("a", "gone")
        assert table.take_evicted() == []

    def test_a_byte_bound_eviction_is_reported_too(self, arm) -> None:
        table = arm(max_entries=100, max_bytes=200, track_evictions=True)
        table.set("a", "first", cost=150)
        table.set("b", "second", cost=150)
        assert table.take_evicted() == [("a", "first")]

    def test_clear_does_not_report_what_it_dropped(self, arm) -> None:
        # A teardown is not an eviction: the caller asked for it by name, and
        # the driver that uses this clears when the connection is going away,
        # where the statements it would close no longer exist.
        table = arm(max_entries=8, track_evictions=True)
        table.set("a", 1)
        table.clear()
        assert table.take_evicted() == []


def test_stats_reports_live_size_not_slots() -> None:
    table = KV(max_entries=8, ttl=10.0)
    table.set("a", 1, now=0.0)
    table.get("a", now=1.0)
    table.get("absent", now=1.0)
    snapshot = stats(table)
    assert isinstance(snapshot, Stats)
    assert snapshot.hits == 1
    assert snapshot.misses == 1
    assert snapshot.size == len(table)
    assert snapshot.hit_rate == 0.5


def test_hit_rate_is_zero_before_the_first_read() -> None:
    assert stats(KV(max_entries=4)).hit_rate == 0.0


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_the_table_holds_its_values_alive_and_lets_them_go(arm) -> None:
    import weakref

    class Held:
        pass

    table = arm(max_entries=4)
    value = Held()
    reference = weakref.ref(value)
    table.set("key", value)
    del value
    assert reference() is not None, "the table owns a reference while it holds the key"
    table.delete("key")
    assert reference() is None, "and releases it when the key goes"


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_a_cycle_through_the_table_is_collectable(arm) -> None:
    import gc
    import weakref

    class Node:
        def __init__(self) -> None:
            self.table = None

    table = arm(max_entries=4)
    node = Node()
    node.table = table
    table.set("self", node)
    reference = weakref.ref(node)
    del node, table
    gc.collect()
    assert reference() is None, "the cycle collector must be able to see through it"


# --- the two arms, on identical operation sequences -------------------------


def _script(seed: int) -> list[tuple[str, object, object]]:
    """A deterministic sequence of operations, weighted toward the edges."""
    random.seed(seed)
    steps: list[tuple[str, object, object]] = []
    now = 0.0
    for index in range(600):
        now += random.choice([0.0, 0.0, 0.0, 1.0, 7.0])
        key = f"k{random.randrange(20)}"
        steps.append((random.choice(
            ["get", "set", "set_keep", "claim", "delete", "pop", "touch", "items", "purge"]
        ), key, (now, index)))
    return steps


def _run(table, steps) -> list[object]:
    results: list[object] = []
    for operation, key, (now, index) in steps:
        if operation == "get":
            results.append(table.get(key, "-", now=now))
        elif operation == "set":
            results.append(table.set(key, index, now=now))
        elif operation == "set_keep":
            results.append(table.set(key, index, now=now, keep_deadline=True))
        elif operation == "claim":
            results.append(table.claim(key, index, now=now))
        elif operation == "delete":
            results.append(table.delete(key))
        elif operation == "pop":
            results.append(table.pop(key, "-", now=now))
        elif operation == "touch":
            results.append(table.touch(key, now=now))
        elif operation == "items":
            results.append(table.items(now=now))
        else:
            results.append(table.purge(now=now))
    return results


@pytest.mark.skipif(KV is PureKV, reason="only one arm is built here")
@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("max_entries", [1, 2, 8, 64])
@pytest.mark.parametrize("ttl", [None, 5.0])
def test_both_arms_agree_step_for_step(seed: int, max_entries: int, ttl: float | None) -> None:
    steps = _script(seed)
    native = KV(max_entries=max_entries, ttl=ttl)
    pure = PureKV(max_entries=max_entries, ttl=ttl)
    assert _run(native, steps) == _run(pure, steps)
    # Counters too, and this is not decoration: the native table used to drop
    # the expired entries its rebuild reclaimed without counting them, which
    # every assertion above passed straight through.
    assert stats(native) == stats(pure)
