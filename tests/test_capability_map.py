from wreath._capability_map import CapabilityMap


class _CountMustNotBeRead:
    def __init__(self, table: object) -> None:
        self._table = table

    def count(self, *, now: float) -> int:
        raise AssertionError(f"refusal occupancy walked the native table at {now}")

    def __getattr__(self, name: str) -> object:
        return getattr(self._table, name)


def test_earliest_overflow_accepts_non_expiring_entries_without_a_deadline() -> None:
    capabilities = CapabilityMap(max_entries=2, overflow="earliest", clock=lambda: 0.0)

    assert capabilities.put("first", 1, now=0.0)
    assert capabilities.put("second", 2, now=0.0)
    assert capabilities.peek("first", now=1.0) == 1
    assert capabilities.peek("second", now=1.0) == 2


def test_updating_an_earliest_entry_does_not_add_another_eviction_record() -> None:
    capabilities = CapabilityMap(
        max_entries=2,
        ttl=10.0,
        overflow="earliest",
        clock=lambda: 0.0,
    )
    capabilities.put("key", 1, now=0.0)

    capabilities.put("key", 2, now=1.0)

    assert len(capabilities._heap) == 1


def test_evict_overflow_does_not_build_an_earliest_deadline_heap() -> None:
    capabilities = CapabilityMap(
        max_entries=2,
        ttl=10.0,
        overflow="evict",
        clock=lambda: 0.0,
    )

    capabilities.put("key", 1, now=0.0)

    assert capabilities._heap == []


def test_refuse_overflow_tracks_occupancy_without_counting_the_native_table() -> None:
    capabilities = CapabilityMap(
        max_entries=2,
        ttl=10.0,
        overflow="refuse",
        clock=lambda: 0.0,
    )
    capabilities.put("first", 1, now=0.0)
    capabilities._table = _CountMustNotBeRead(capabilities._table)

    assert len(capabilities) == 1
    assert capabilities.put("second", 2, now=1.0)
    assert not capabilities.put("third", 3, now=1.0)


def test_refuse_overflow_sweeps_only_due_deadlines_and_returns_their_values() -> None:
    capabilities = CapabilityMap(
        max_entries=2,
        ttl=10.0,
        overflow="refuse",
        clock=lambda: 0.0,
    )
    capabilities.put("first", 1, now=0.0)
    capabilities.put("second", 2, now=5.0)

    assert capabilities.sweep(now=9.0) == ()
    assert capabilities.sweep(now=10.0) == (1,)
    assert capabilities.held("first") is None
    assert capabilities.held("second") == 2
    assert capabilities.next_deadline == 15.0


def test_deadline_policy_can_keep_an_entry_at_the_exact_boundary() -> None:
    capabilities = CapabilityMap(
        max_entries=1,
        ttl=10.0,
        overflow="refuse",
        expire_at_deadline=False,
        clock=lambda: 0.0,
    )
    capabilities.put("key", 1, now=0.0)

    assert capabilities.sweep(now=10.0) == ()
    assert capabilities.sweep(now=10.1) == (1,)


def test_refuse_peek_purges_expired_occupancy() -> None:
    capabilities = CapabilityMap(max_entries=1, overflow="refuse", clock=lambda: 0.0)
    capabilities.put("old", 1, ttl=1.0, now=0.0)

    assert capabilities.peek("old", now=1.0) is None
    assert capabilities._keys == set()


def test_refuse_put_and_claim_reuse_expired_capacity() -> None:
    via_put = CapabilityMap(max_entries=1, overflow="refuse", clock=lambda: 0.0)
    via_put.put("old", 1, ttl=1.0, now=0.0)
    assert via_put.put("new", 2, now=1.0)

    via_claim = CapabilityMap(max_entries=1, overflow="refuse", clock=lambda: 0.0)
    via_claim.put("old", 1, ttl=1.0, now=0.0)
    assert via_claim.claim("new", 2, now=1.0)


def test_earliest_overflow_evicts_the_entry_with_the_first_deadline() -> None:
    capabilities = CapabilityMap(max_entries=2, overflow="earliest", clock=lambda: 0.0)
    capabilities.put("later", 1, ttl=20.0, now=0.0)
    capabilities.put("sooner", 2, ttl=10.0, now=0.0)

    assert capabilities.put("new", 3, ttl=30.0, now=1.0)
    assert capabilities.peek("later", now=1.0) == 1
    assert capabilities.peek("sooner", now=1.0) is None
    assert capabilities.peek("new", now=1.0) == 3
    assert capabilities._keys == set()


def test_explicit_ttl_overrides_the_map_default() -> None:
    capabilities = CapabilityMap(
        max_entries=1, ttl=100.0, overflow="refuse", clock=lambda: 0.0
    )

    capabilities.put("key", 1, ttl=5.0, now=0.0)

    assert capabilities.next_deadline == 5.0
    assert capabilities.sweep(now=5.0) == (1,)


def test_refuse_update_can_remove_an_existing_deadline() -> None:
    capabilities = CapabilityMap(max_entries=1, overflow="refuse", clock=lambda: 0.0)
    capabilities.put("key", 1, ttl=5.0, now=0.0)

    capabilities.put("key", 2, now=1.0)

    assert capabilities.next_deadline == float("inf")
    assert capabilities.peek("key", now=10.0) == 2


def test_refuse_update_replaces_stale_deadline_records() -> None:
    capabilities = CapabilityMap(max_entries=1, overflow="refuse", clock=lambda: 0.0)
    capabilities.put("key", 1, ttl=5.0, now=0.0)
    capabilities.put("key", 2, ttl=10.0, now=1.0)

    assert capabilities.sweep(now=5.0) == ()
    assert capabilities.peek("key", now=5.0) == 2
    assert capabilities.sweep(now=11.0) == (2,)


def test_sweep_does_not_report_none_as_an_expired_value() -> None:
    capabilities = CapabilityMap(max_entries=1, overflow="refuse", clock=lambda: 0.0)
    capabilities.put("key", None, ttl=1.0, now=0.0)

    assert capabilities.sweep(now=1.0) == ()


def test_nonrefusal_peek_does_not_consume_other_expired_held_values() -> None:
    capabilities = CapabilityMap(max_entries=2, overflow="earliest", clock=lambda: 0.0)
    capabilities.put("first", 1, ttl=1.0, now=0.0)
    capabilities.put("second", 2, ttl=1.0, now=0.0)

    assert capabilities.peek("first", now=1.0) is None
    assert capabilities.held("second") == 2


def test_keep_deadline_preserves_the_original_refuse_expiry() -> None:
    capabilities = CapabilityMap(max_entries=1, overflow="refuse", clock=lambda: 0.0)
    capabilities.put("key", 1, ttl=5.0, now=0.0)

    capabilities.put("key", 2, ttl=20.0, now=1.0, keep_deadline=True)

    assert capabilities.next_deadline == 5.0
    assert capabilities.sweep(now=5.0) == (2,)


def test_a_new_refuse_entry_gets_a_deadline_even_when_keep_is_requested() -> None:
    capabilities = CapabilityMap(max_entries=1, overflow="refuse", clock=lambda: 0.0)

    capabilities.put("key", 1, ttl=5.0, now=0.0, keep_deadline=True)

    assert capabilities.next_deadline == 5.0
    assert capabilities.sweep(now=5.0) == (1,)


def test_an_earliest_update_keeps_its_original_eviction_position() -> None:
    capabilities = CapabilityMap(max_entries=2, overflow="earliest", clock=lambda: 0.0)
    capabilities.put("first", 1, ttl=5.0, now=0.0)
    capabilities.put("second", 2, ttl=10.0, now=0.0)

    capabilities.put("first", 3, ttl=30.0, now=1.0)
    capabilities.put("third", 4, ttl=40.0, now=2.0)

    assert capabilities.peek("first", now=2.0) is None
    assert capabilities.peek("second", now=2.0) == 2
    assert capabilities.peek("third", now=2.0) == 4


def test_nonrefusal_length_counts_live_entries() -> None:
    capabilities = CapabilityMap(max_entries=2, overflow="earliest", clock=lambda: 0.0)
    capabilities.put("key", 1, ttl=5.0, now=0.0)

    assert len(capabilities) == 1


def test_claim_refuses_to_replace_an_existing_capability() -> None:
    capabilities = CapabilityMap(max_entries=2, overflow="refuse", clock=lambda: 0.0)

    assert capabilities.claim("key", 1, now=0.0)
    assert not capabilities.claim("key", 2, now=0.0)
    assert capabilities.peek("key", now=0.0) == 1
