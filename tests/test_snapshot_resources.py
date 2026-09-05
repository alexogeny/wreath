import pytest

from wreath._snapshot import SnapshotCache


def test_required_snapshot_lookup_hashes_present_key_once():
    class Key:
        calls = 0

        def __hash__(self):
            self.calls += 1
            return 1

    key = Key()
    cache = SnapshotCache()
    value = object()
    cache.replace({key: value})
    key.calls = 0
    assert cache.require(key) is value
    assert key.calls == 1


@pytest.mark.parametrize("key", ["missing", 42, ("missing", 42), None])
def test_required_snapshot_miss_preserves_key_error_arguments(key):
    cache = SnapshotCache()
    with pytest.raises(KeyError) as raised:
        cache.require(key)
    assert raised.value.args == (key,)


def test_required_snapshot_lookup_preserves_unhashable_error():
    cache = SnapshotCache()
    with pytest.raises(TypeError, match="unhashable type: 'list'"):
        cache.require([])


def test_required_snapshot_lookup_keeps_its_generation_during_hash():
    cache = SnapshotCache()

    class Key:
        publish = False

        def __hash__(self):
            if self.publish:
                cache.replace({"next": "generation"})
            return 1

    key = Key()
    cache.replace({key: "original"})
    key.publish = True
    assert cache.require(key) == "original"
    assert cache.require("next") == "generation"
