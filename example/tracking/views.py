"""A zone-bound daily distance view with corrections for late positions."""

from __future__ import annotations

from wreath.queries import Param
from wreath.series import Series, count, sum_
from wreath.temporal import Day, zone

from .models import Fix

#: How long after a day ends its distance can still change.
#:
#: Thirty-six hours because that is this programme's field claim: a collar that
#: has not reported for a day and a half is a collar somebody drives out to
#: check, so a position older than that is not "late", it is a recovery. It is a
#: statement about *these* collars under *this* canopy, not a framework default,
#: which is why it is declared here. A programme on open grassland with hourly
#: uplinks would seal in two hours and see corrections almost never.
BUFFER_LATENESS = "36h"


def daily_distance(timezone: str) -> Series:
    """Fixes and metres travelled per local day, for one animal, sealed.

    Two measures rather than two views because they are read together and share
    a scan, and because they answer each other: 400 m over three fixes is a
    collar that was mostly asleep, and 400 m over ninety fixes is an animal
    standing at a waterhole all day. A distance without its fix count invites
    the reader to treat the first as the second.

    ``distance_m`` sums `Fix.leg_m`, a `float8`, so it reads back as a `float`
    and serialises. That is not an accident of the column type: the camera-trap
    example's sealed view can only store a *count*, because `avg()` over an
    integer column is `numeric`, the driver decodes `numeric` to `Decimal`, and
    `json.dumps` refuses a `Decimal` -- which for a settled row is fatal, since
    the value is stored rather than rendered and there is no edge to round at.
    Choosing `float8` for a measurement rather than `Numeric` sidesteps it here,
    and is the right choice for a GPS reading anyway.

    Args:
        timezone: An IANA zone name. A day is only a day once you say whose, and
            "how far did it walk yesterday" is asked in the conservancy's own
            calendar -- not the reader's, and not UTC.

    Returns:
        A declaration whose sealed buckets are stored on first read and
        thereafter answered from storage, and whose late arrivals are recorded
        as corrections beside them.
    """
    return (
        Series(Fix, at=Fix.recorded_at, bucket=Day, stored_in=zone(timezone))
        .where(Fix.animal_id == Param("animal"))
        .measure(fixes=count(), distance_m=sum_(Fix.leg_m, unit="m"))
        .seal(after=BUFFER_LATENESS)
    )
