"""Slice 5: two subscribers on one room, one event, different precisions.

The composition wreath is uniquely placed to make: it owns the fan-out
(`rooms`, cross-worker over the bus), the policy engine (Cedar) and the
coordinate type. Assembling this from three packages means re-authorizing every
event per socket, which is exactly the implementation that falls over during the
incident everyone is watching.
"""

from __future__ import annotations

import json

import pytest

from wreath.authorization import PrecisionLadder, coarsen
from wreath.geospatial import Coordinate
from wreath.rooms import RoomRegistry

COLLAR = Coordinate(lat=-23.6980, lon=133.8807)

LADDER = PrecisionLadder(
    ("exact", None), ("fine", 1_000), ("coarse", 10_000)
)

#: What each role may see, as the grade a subscriber carries.
GRADES = {"ranger": None, "partner": 1_000.0, "volunteer": 10_000.0}


class _Socket:
    def __init__(self, role):
        self.role = role
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def _render(grade, payload):
    """One payload per distinct grade. `None` grade is exact; WITHHELD is absent."""
    if grade is _WITHHELD:
        return None
    point = coarsen(COLLAR, grade) if grade is not None else COLLAR
    return json.dumps({"collar": payload, "lat": point.lat, "lon": point.lon})


_WITHHELD = object()


def _grade(socket):
    return GRADES.get(socket.role, _WITHHELD)


# --- the composition ---------------------------------------------------------


async def test_two_subscribers_receive_one_event_at_different_precisions():
    """The single most valuable assertion in the slice.

    Same room, same broadcast, same instant -- and the two watchers legitimately
    disagree about where the animal is, because the authorizer graded them
    differently.
    """
    rooms = RoomRegistry()
    ranger, volunteer = _Socket("ranger"), _Socket("volunteer")
    await rooms.join("collar-7", ranger)
    await rooms.join("collar-7", volunteer)

    delivered = await rooms.broadcast("collar-7", "7", grade=_grade, render=_render)

    assert delivered == 2
    seen_by_ranger = json.loads(ranger.sent[0])
    seen_by_volunteer = json.loads(volunteer.sent[0])
    assert (seen_by_ranger["lat"], seen_by_ranger["lon"]) == (COLLAR.lat, COLLAR.lon)
    coarse = coarsen(COLLAR, 10_000)
    assert (seen_by_volunteer["lat"], seen_by_volunteer["lon"]) == (coarse.lat, coarse.lon)
    assert seen_by_ranger != seen_by_volunteer


async def test_a_grade_seeing_nothing_receives_no_frame_at_all():
    """Absent, not an empty event: a blank frame still announces that one happened."""
    rooms = RoomRegistry()
    ranger, public = _Socket("ranger"), _Socket("nobody")
    await rooms.join("collar-7", ranger)
    await rooms.join("collar-7", public)

    delivered = await rooms.broadcast("collar-7", "7", grade=_grade, render=_render)

    assert delivered == 1
    assert public.sent == []
    assert len(ranger.sent) == 1


async def test_the_expensive_half_runs_once_per_grade_not_once_per_socket():
    """The optimisation that makes this affordable during an incident."""
    rooms = RoomRegistry()
    for _ in range(30):
        await rooms.join("collar-7", _Socket("volunteer"))
    for _ in range(20):
        await rooms.join("collar-7", _Socket("ranger"))

    renders = []

    def counting_render(grade, payload):
        renders.append(grade)
        return _render(grade, payload)

    delivered = await rooms.broadcast(
        "collar-7", "7", grade=_grade, render=counting_render
    )

    assert delivered == 50
    assert len(renders) == 2, f"rendered {len(renders)} times for 2 grades"


async def test_the_cheap_half_runs_once_per_socket():
    """`grade` is per socket per broadcast, which is what keeps it fresh."""
    rooms = RoomRegistry()
    sockets = [_Socket("ranger") for _ in range(5)]
    for socket in sockets:
        await rooms.join("collar-7", socket)

    graded = []

    def counting_grade(socket):
        graded.append(socket)
        return _grade(socket)

    await rooms.broadcast("collar-7", "7", grade=counting_grade, render=_render)
    assert len(graded) == 5


# --- the stale-grant decision ------------------------------------------------


async def test_a_revoked_grant_takes_effect_on_the_very_next_event():
    """The plan required an explicit answer, and this is it.

    Grouping subscribers by authorization outcome is only sound while the
    outcome holds. `grade` is therefore called per broadcast rather than cached
    at join, so a grade backed by live state is fresh by construction: the
    framework cannot serve an event under a grant that has been revoked.
    """
    rooms = RoomRegistry()
    watcher = _Socket("ranger")
    await rooms.join("collar-7", watcher)

    await rooms.broadcast("collar-7", "7", grade=_grade, render=_render)
    first = json.loads(watcher.sent[0])
    assert (first["lat"], first["lon"]) == (COLLAR.lat, COLLAR.lon)

    # The policy changes mid-stream: this watcher is demoted.
    watcher.role = "volunteer"
    await rooms.broadcast("collar-7", "7", grade=_grade, render=_render)

    second = json.loads(watcher.sent[1])
    coarse = coarsen(COLLAR, 10_000)
    assert (second["lat"], second["lon"]) == (coarse.lat, coarse.lon)


async def test_a_revocation_to_nothing_stops_delivery_entirely():
    rooms = RoomRegistry()
    watcher = _Socket("ranger")
    await rooms.join("collar-7", watcher)

    await rooms.broadcast("collar-7", "7", grade=_grade, render=_render)
    watcher.role = "revoked"
    delivered = await rooms.broadcast("collar-7", "7", grade=_grade, render=_render)

    assert delivered == 0
    assert len(watcher.sent) == 1


# --- refusals and degradation ------------------------------------------------


async def test_grade_without_render_is_refused():
    """One without the other would deliver the ungraded payload to everyone."""
    rooms = RoomRegistry()
    await rooms.join("collar-7", _Socket("ranger"))
    with pytest.raises(ValueError, match="both or neither"):
        await rooms.broadcast("collar-7", "7", grade=_grade)


async def test_render_without_grade_is_refused():
    rooms = RoomRegistry()
    await rooms.join("collar-7", _Socket("ranger"))
    with pytest.raises(ValueError, match="both or neither"):
        await rooms.broadcast("collar-7", "7", render=_render)


async def test_a_grade_that_raises_drops_that_socket_and_is_counted():
    """One socket that cannot be graded must not end the fan-out -- or vanish."""
    rooms = RoomRegistry()
    good, bad = _Socket("ranger"), _Socket("ranger")
    await rooms.join("collar-7", good)
    await rooms.join("collar-7", bad)

    def grade(socket):
        if socket is bad:
            raise RuntimeError("no answer")
        return _grade(socket)

    delivered = await rooms.broadcast(
        "collar-7", "7", grade=grade, render=_render
    )

    assert delivered == 1
    assert good.sent and not bad.sent
    assert rooms.grade_errors == 1
    # Failing to answer "what may you see" is not evidence the socket is dead.
    assert rooms.members("collar-7") == 2


async def test_a_dead_socket_is_still_dropped_from_a_graded_broadcast():
    rooms = RoomRegistry()

    class _Dead(_Socket):
        async def send(self, payload):
            raise ConnectionError("gone")

    live, dead = _Socket("ranger"), _Dead("ranger")
    await rooms.join("collar-7", live)
    await rooms.join("collar-7", dead)

    delivered = await rooms.broadcast("collar-7", "7", grade=_grade, render=_render)

    assert delivered == 1
    assert rooms.members("collar-7") == 1


async def test_a_graded_broadcast_to_an_empty_room_delivers_nothing():
    rooms = RoomRegistry()
    assert await rooms.broadcast("empty", "7", grade=_grade, render=_render) == 0


async def test_a_graded_broadcast_to_an_unknown_room_delivers_nothing():
    rooms = RoomRegistry()
    await rooms.join("other", _Socket("ranger"))
    assert await rooms.broadcast("collar-7", "7", grade=_grade, render=_render) == 0


async def test_an_ungraded_broadcast_is_unchanged():
    """The default path must be exactly what it was before this slice."""
    rooms = RoomRegistry()
    a, b = _Socket("ranger"), _Socket("volunteer")
    await rooms.join("chat", a)
    await rooms.join("chat", b)

    delivered = await rooms.broadcast("chat", "hello")

    assert delivered == 2
    assert a.sent == ["hello"] and b.sent == ["hello"]
