"""Temporal values encode inline, byte-for-byte as the rewrite path did.

`wreath._json.dumps` used to encode, catch TypeError, rebuild the whole payload
through `temporal.jsonable`, and encode again. It now renders temporal values
during the first pass. The bytes must not move: these assert the new output
equals `_dumps(jsonable(payload))` -- literally the old path -- for every shape
and every temporal type, and that the retry still covers what it always did.
"""

from __future__ import annotations

import datetime
import traceback

import pytest

from wreath._json import _dumps as _raw_dumps
from wreath._json import dumps
from wreath.temporal import Instant, jsonable

UTC = datetime.UTC
_VALUES = [
    datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    datetime.datetime(2024, 6, 30, 23, 59, 59, 123456, tzinfo=UTC),
    datetime.datetime(2024, 1, 1, 12, 0, 0),                       # naive
    datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone(
        datetime.timedelta(hours=-5))),                            # offset tz
    datetime.date(2024, 2, 29),
    datetime.time(1, 2, 3),
    datetime.time(1, 2, 3, 400000),
    datetime.timedelta(seconds=90),
    datetime.timedelta(days=2, hours=3, minutes=4, seconds=5),
    datetime.timedelta(0),
    datetime.timedelta(microseconds=1),
    datetime.timedelta(days=-1),
    Instant.parse("2024-01-01T00:00:00+00:00"),
]


def _matches_old_path(payload: object) -> bytes:
    """The old behaviour, stated explicitly as the oracle."""
    expected = _raw_dumps(jsonable(payload))
    actual = dumps(payload)
    assert actual == expected
    return actual


@pytest.mark.parametrize("value", _VALUES, ids=lambda v: repr(v)[:40])
def test_bare_temporal_value(value: object) -> None:
    _matches_old_path(value)


@pytest.mark.parametrize("value", _VALUES, ids=lambda v: repr(v)[:40])
def test_temporal_in_containers(value: object) -> None:
    _matches_old_path({"at": value})
    _matches_old_path([value, value])
    _matches_old_path({"rows": [{"id": 1, "at": value}, {"id": 2, "at": value}]})
    _matches_old_path({"deep": {"deeper": {"deepest": [value]}}})
    _matches_old_path((value,))


def test_realistic_orm_shaped_payload() -> None:
    row = {
        "id": 1,
        "email": "a@b.c",
        "active": True,
        "score": 1.5,
        "created_at": datetime.datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        "updated_at": None,
    }
    _matches_old_path({"rows": [dict(row, id=i) for i in range(20)], "total": 20})


def test_payload_without_temporal_values_is_unchanged() -> None:
    payload = {"rows": [{"id": i, "name": f"n{i}"} for i in range(10)], "ok": True}
    assert dumps(payload) == _raw_dumps(payload)


def test_unserializable_objects_still_raise() -> None:
    class Nope:
        pass

    with pytest.raises(TypeError):
        dumps({"bad": Nope()})


def test_temporal_beside_an_unserializable_object_still_raises() -> None:
    """The retry used to rewrite temporals then fail; it must still fail."""

    class Nope:
        pass

    with pytest.raises(TypeError):
        dumps({"at": datetime.datetime(2024, 1, 1, tzinfo=UTC), "bad": Nope()})


def test_non_finite_floats_are_still_rejected() -> None:
    with pytest.raises(ValueError):
        dumps({"x": float("inf")})


def test_non_str_keys_are_still_rejected() -> None:
    with pytest.raises(TypeError):
        dumps({1: "a"})


# --- the retry must not report one error as two (design 22 item 16) -------------


def test_an_unserializable_object_is_reported_once_not_twice() -> None:
    """The retry re-raises, so the same message appeared under a chained
    traceback -- reading as a second, different problem on the commonest JSON
    failure there is."""

    class Nope:
        pass

    with pytest.raises(TypeError) as caught:
        dumps(Nope())
    printed = "".join(traceback.format_exception(caught.value))
    assert "During handling of the above exception" not in printed
    assert str(caught.value).count("not JSON serializable") == 1


def test_a_nested_unserializable_object_is_also_reported_once() -> None:
    class Nope:
        pass

    with pytest.raises(TypeError) as caught:
        dumps({"a": [1, 2, Nope()]})
    printed = "".join(traceback.format_exception(caught.value))
    assert "During handling of the above exception" not in printed
    assert str(caught.value).count("not JSON serializable") == 1


def test_a_hook_raising_its_own_type_error_is_not_masked() -> None:
    """A __jsonable__ that fails says something the encoder's error did not, so
    it must reach the caller rather than being replaced by 'not serializable'."""

    class BadHook:
        def __jsonable__(self):
            raise TypeError("hook exploded for its own reasons")

    with pytest.raises(TypeError, match="hook exploded for its own reasons"):
        dumps(BadHook())


def test_a_jsonable_hook_is_materialized_once_at_the_encoder_boundary() -> None:
    calls = 0

    class Result:
        def __jsonable__(self):
            nonlocal calls
            calls += 1
            return {"at": datetime.datetime(2024, 1, 1, tzinfo=UTC)}

    assert dumps(Result()) == b'{"at":"2024-01-01T00:00:00+00:00"}'
    assert calls == 1


def test_a_jsonable_hook_must_not_return_itself() -> None:
    class Recursive:
        def __jsonable__(self):
            return self

    with pytest.raises(TypeError, match="returned itself"):
        dumps(Recursive())


def test_an_instance_getattr_is_never_consulted() -> None:
    """`jsonable` looks the hook up on the *type*, so an instance __getattr__
    cannot be triggered by encoding -- the mechanism design 22 item 16
    hypothesised."""
    consulted = []

    class Grumpy:
        def __getattr__(self, name):
            consulted.append(name)
            raise TypeError("unrelated")

    with pytest.raises(TypeError, match="not JSON serializable"):
        dumps(Grumpy())
    assert consulted == []
