"""`is_message` — the public answer to "is this a protobuf message?".

`wreath.grpc` was reading the private `__wreath_protobuf_plan__` marker to
decide, which meant a second module carried its own notion of what a message
is. These pin the predicate so the marker can move without breaking consumers.
"""

from __future__ import annotations

from dataclasses import dataclass

from wreath.protobuf import field, is_message, message


@message
class Position:
    collar_id: int = field(1)
    lat: float = field(2)


@dataclass
class NotAMessage:
    collar_id: int


def test_a_message_class_is_recognised() -> None:
    assert is_message(Position)


def test_an_instance_is_recognised_too() -> None:
    """A caller branching on a value, not a type, must get the same answer."""
    assert is_message(Position(collar_id=1, lat=0.0))


def test_a_plain_dataclass_is_not_a_message() -> None:
    assert not is_message(NotAMessage)
    assert not is_message(NotAMessage(collar_id=1))


def test_ordinary_values_are_not_messages() -> None:
    for value in (None, 1, "x", b"x", object(), dict, list):
        assert not is_message(value), value


def test_the_predicate_agrees_with_the_private_marker() -> None:
    """The point of the export: one notion of a message, not two.

    If these ever disagree, a consumer that used the marker directly and one
    that used the predicate would classify the same object differently.
    """
    from wreath.protobuf import _PLAN

    assert is_message(Position) is hasattr(Position, _PLAN)
    assert is_message(NotAMessage) is hasattr(NotAMessage, _PLAN)
