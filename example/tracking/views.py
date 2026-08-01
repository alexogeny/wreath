"""How far an animal walked yesterday, and what to do when yesterday changes.

One declaration, and it carries the whole of the late-data argument.

A collar under thick canopy or in a gorge cannot see a satellite. It keeps
taking positions -- that is what the buffer is for -- and uploads them when the
sky comes back, which is hours later on a bad day and *days* later in the wet
season. So the distance an animal walked on Tuesday is a number that this
application confidently reported on Wednesday morning and may have to revise on
Friday afternoon.

There are three things one can do about that, and only one of them is
defensible:

* **Refuse the late write.** Decisively wrong. `fixes` is the business table --
  it is the record of where the animals were -- and a chart's watermark must
  never be able to fail a business write.
* **Rewrite Tuesday's number.** This is the failure that sealing exists to
  prevent. A weekly report went out on Wednesday quoting 14.2 km. If Tuesday can
  silently become 19.8 km, then it was never settled and nobody can reconcile
  the report against the system that produced it.
* **Record the difference beside it.** Tuesday stays 14.2 km, the +5.6 km is
  stored as a correction, the read folds them together, and
  `result.state.corrections` names Tuesday as a day that carries one. Late data
  then *looks* like late data arriving, rather than like a number that changed
  on its own.

`.seal(after=...)` is how the third one is spelled, and `on_late="correct"` --
the default -- is the behaviour above. This module declares it and nothing else.

**Why this is a function and not a constant.** A sealed bucket stores the zone
it was cut in: a Nairobi day cannot be re-cut into a London day after the fact,
so the zone is part of the view's identity rather than a per-request argument.
The camera-trap example's `sealed_activity` is a function for exactly this
reason and says so at more length.
"""

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
