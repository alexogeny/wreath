from wreath._capability_map import CapabilityMap


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
