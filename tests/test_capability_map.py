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
