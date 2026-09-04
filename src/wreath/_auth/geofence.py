"""Geofencing and location precision, as authorization rather than as code.

Two capabilities that only exist because one project owns both the policy
engine and the coordinate type, and that a team could not assemble by
installing two libraries:

* **`Regions`** turns the caller's position into `context.regions`, a set of
  the named areas containing it, so `context.regions.contains(resource.site)`
  is a *policy* rather than a predicate rewritten at every call site.
* **`PrecisionLadder`** makes the answer to "may you see where this is?" a
  **resolution** rather than a verdict — exact for a ranger, 10 km for a
  volunteer, absent for the public — decided by the same engine that decides
  everything else.

Neither adds a word to the policy language. A geofence is a set membership
test, which Cedar already has, and a precision level is an ordered list of
ordinary actions asked in order. That matters: an evaluator is a compatibility
surface, and a dialect nobody else parses is a poor trade for syntax sugar.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from .._reqcache import resolve_once_async
from ..geospatial import BoundingBox, Coordinate, distance
from .requirements import PolicyRequirement

__all__ = [
    "WITHHELD",
    "PrecisionLadder",
    "Regions",
    "coarsen",
    "resolve_precision",
]


class _Withheld:
    """The answer when no rung permits: absent, and distinguishable from exact.

    A sentinel rather than `None`, because `None` already means *exact* on a
    rung and the two are opposite ends of the ladder. Collapsing them is the
    one mistake in this design that would publish a precise coordinate to a
    caller entitled to none.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "WITHHELD"

    def __bool__(self) -> bool:
        return False


#: No rung permitted: the coordinate does not appear at all.
WITHHELD = _Withheld()

#: Metres per degree of latitude, on the sphere `wreath.geospatial` measures on.
#: Longitude is this scaled by cos(latitude), which is why a grid cell's width
#: is latitude-dependent and its height is not.
_METRES_PER_DEGREE = math.pi * 6_371_008.8 / 180.0

#: Below this cosine a longitude grid step would exceed the whole globe, so the
#: cell collapses to the parallel instead. Reached only within ~30 km of a pole.
_POLE_COSINE = 1e-9


class Regions:
    """Named areas, and which of them contain a point.

    The vocabulary a geofencing policy names. Each region is either a circle
    (a centre and a radius in metres) or a `BoundingBox`; a point is inside a
    circle when the great-circle distance is within the radius, and inside a
    box when the box contains it.

    ```python
    regions = Regions(
        depot=(Coordinate(lat=-25.1, lon=133.4), 5_000),
        reserve=BoundingBox(-26.0, -24.0, 132.0, 135.0),
    )
    ```

    Names are the strings a policy writes, so they are validated once here
    rather than failing as a silent non-match later: a name that is not a
    non-empty string is refused where the region is declared.

    Args:
        regions: name to `(centre, radius_metres)` or `BoundingBox`.

    Raises:
        ValueError: a name is empty or not a string, a radius is not positive,
            or a region is neither a circle pair nor a `BoundingBox`.
    """

    __slots__ = ("_boxes", "_circles")

    def __init__(self, regions: Mapping[str, Any] | None = None, **named: Any) -> None:
        merged: dict[str, Any] = dict(regions or {})
        merged.update(named)
        circles: dict[str, tuple[Coordinate, float]] = {}
        boxes: dict[str, BoundingBox] = {}
        for name, region in merged.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"region names are the strings a policy writes, so they must "
                    f"be non-empty strings; got {name!r}"
                )
            if isinstance(region, BoundingBox):
                boxes[name] = region
                continue
            centre, radius = _as_circle(name, region)
            circles[name] = (centre, radius)
        self._circles = circles
        self._boxes = boxes

    def names(self) -> frozenset[str]:
        """Every declared region name.

        The enumeration `CedarAuthorizer` validates a policy set against at
        startup, so a misspelled region fails where it is written rather than
        never matching.
        """
        return frozenset(self._circles) | frozenset(self._boxes)

    def containing(self, point: Coordinate, names: Iterable[str] | None = None) -> frozenset[str]:
        """The declared regions containing `point`.

        Args:
            names: resolve only these, which is what the authorizer passes when
                the policy set's references are statically knowable. `None`
                resolves every declared region — the honest answer when a policy
                computes the name it tests.
        """
        wanted = self.names() if names is None else frozenset(names)
        found: set[str] = set()
        for name in wanted:
            circle = self._circles.get(name)
            if circle is not None:
                centre, radius = circle
                if distance(point, centre) <= radius:
                    found.add(name)
                continue
            box = self._boxes.get(name)
            if box is not None and box.contains(point):
                found.add(name)
        return frozenset(found)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Regions({len(self._circles)} circles, {len(self._boxes)} boxes)"


def _as_circle(name: str, region: Any) -> tuple[Coordinate, float]:
    """Read a `(centre, radius)` pair, refusing anything else by name."""
    if not isinstance(region, tuple | list) or len(region) != 2:
        raise ValueError(
            f"region {name!r} must be a BoundingBox or a (centre, radius_metres) "
            f"pair; got {region!r}"
        )
    centre, radius = region
    if not isinstance(centre, Coordinate):
        raise ValueError(f"region {name!r} needs a Coordinate centre; got {type(centre).__name__}")
    if not isinstance(radius, int | float) or isinstance(radius, bool):
        raise ValueError(f"region {name!r} needs a numeric radius; got {radius!r}")
    radius = float(radius)
    if not math.isfinite(radius) or radius <= 0.0:
        raise ValueError(
            f"region {name!r} needs a positive finite radius in metres; got {radius!r}"
        )
    return centre, radius


def coarsen(point: Coordinate, metres: float) -> Coordinate:
    """`point` rounded onto a fixed grid of roughly `metres` on a side.

    **The grid is absolute, not relative to the point**, and there is no
    randomness anywhere in it. That is the whole security property: the
    alternative — adding jitter per request — averages away, so an attacker who
    can ask repeatedly recovers the true position to arbitrary precision. A
    grid cell reveals the cell and nothing further however many times it is
    asked, which is why `coarsen` is a pure function of `(point, metres)` and
    takes no seed, no salt and no request.

    The cell is *at least* `metres` in both dimensions. Latitude rounds on a
    constant step; longitude rounds on that step divided by the cosine of the
    **rounded** latitude, so a cell near the poles stays as wide on the ground
    as one at the equator and the band a point falls in cannot depend on the
    part of its own longitude being removed. Within roughly 30 km of a pole a
    cell would span the globe, and the longitude collapses to 0 rather than
    pretending to a precision the grid cannot express.

    Args:
        metres: cell size. Must be positive and finite.

    Raises:
        ValueError: `metres` is not a positive finite number.
    """
    if not isinstance(metres, int | float) or isinstance(metres, bool):
        raise ValueError(f"coarsen needs a numeric cell size in metres; got {metres!r}")
    metres = float(metres)
    if not math.isfinite(metres) or metres <= 0.0:
        raise ValueError(f"coarsen needs a positive cell size in metres; got {metres!r}")
    lat_step = metres / _METRES_PER_DEGREE
    lat = _snap(point.lat, lat_step)
    # Clamped rather than wrapped: a cell centred on the last band can round
    # past the pole, and a latitude outside +/-90 is not a place.
    lat = max(-90.0, min(90.0, lat))
    scale = math.cos(math.radians(lat))
    if scale <= _POLE_COSINE:
        return Coordinate(lat=lat, lon=0.0)
    lon_step = lat_step / scale
    if lon_step >= 360.0:
        return Coordinate(lat=lat, lon=0.0)
    lon = _snap(point.lon, lon_step)
    # A longitude landing exactly on the antimeridian is written -180.0 by
    # convention, so two points either side of it agree on one cell name.
    if lon >= 180.0:
        lon -= 360.0
    elif lon < -180.0:
        lon += 360.0
    return Coordinate(lat=lat, lon=lon)


def _snap(value: float, step: float) -> float:
    """`value` rounded to the nearest multiple of `step`, cell centre included.

    The centre offset matters: snapping to the *edge* puts a point reported at
    `0.0` on the boundary of four cells, and a reader cannot tell whether it
    means "at the origin" or "somewhere in one of these". Reporting the centre
    makes the value a place inside the cell it stands for.
    """
    index = math.floor(value / step)
    return (index + 0.5) * step


class PrecisionLadder:
    """An ordered set of actions, finest first, answering *how* precisely.

    The generalisation of a withheld field from a boolean to a value. A
    withheld coordinate is either present or absent, which is the whole
    vocabulary a serializer normally has; real deployments want a third and a
    fourth answer, and they want the policy engine to choose between them:

    ```python
    ladder = PrecisionLadder(
        ("read_location_exact", None),      # exact
        ("read_location_fine", 1_000),      # 1 km
        ("read_location_coarse", 10_000),   # 10 km
    )
    ```

    Each rung is an ordinary Cedar action and an ordinary Cedar decision, asked
    in order until one permits. A caller permitted none sees no coordinate at
    all — **absent, not null**, which is the argument the camera-trap example
    already makes for a boolean and this extends to a scale.

    Ordering is checked at declaration: a ladder whose rungs coarsen out of
    order, or repeat a resolution, is a mistake that would otherwise show up as
    a caller silently receiving a *finer* answer than the rung above allowed.

    Args:
        rungs: `(action, metres)` finest first; `metres` of `None` means exact
            and is only valid on the first rung.

    Raises:
        ValueError: no rungs, a duplicate action, an exact rung that is not
            first, or resolutions that do not strictly coarsen.
    """

    __slots__ = ("_rungs",)

    def __init__(self, *rungs: tuple[str, float | None]) -> None:
        if not rungs:
            raise ValueError(
                "a PrecisionLadder needs at least one rung; a ladder with none "
                "would withhold every coordinate and say nothing about why"
            )
        checked: list[tuple[str, float | None]] = []
        seen: set[str] = set()
        previous: float | None = None
        for position, rung in enumerate(rungs):
            action, metres = _as_rung(position, rung)
            if action in seen:
                raise ValueError(
                    f"PrecisionLadder repeats the action {action!r}; each rung is "
                    "a distinct question and a repeat can never be reached twice"
                )
            seen.add(action)
            if metres is None:
                if position != 0:
                    raise ValueError(
                        f"the exact rung {action!r} must come first: a ladder is "
                        "asked finest-first, so an exact rung below a coarse one "
                        "is unreachable"
                    )
            elif previous is not None and metres <= previous:
                raise ValueError(
                    f"PrecisionLadder rungs must coarsen strictly: {action!r} at "
                    f"{metres}m does not exceed the {previous}m rung above it"
                )
            if metres is not None:
                previous = metres
            checked.append((action, metres))
        self._rungs = tuple(checked)

    @property
    def rungs(self) -> Sequence[tuple[str, float | None]]:
        """The rungs, finest first, as declared."""
        return self._rungs

    def actions(self) -> tuple[str, ...]:
        """Every action this ladder asks, finest first.

        The vocabulary `declared_actions` needs, so a ladder's rungs are part of
        the permission manifest rather than a second list that drifts from it.
        """
        return tuple(action for action, _ in self._rungs)

    def __iter__(self) -> Iterator[tuple[str, float | None]]:
        return iter(self._rungs)

    def __len__(self) -> int:
        return len(self._rungs)

    def apply(self, point: Coordinate | None, metres: float | None) -> Coordinate | None:
        """`point` at the resolution `metres` names.

        The one place a resolution becomes a value: `None` metres is exact,
        a number coarsens, and `withheld` (expressed by the caller passing no
        rung at all) never reaches here.
        """
        if point is None:
            return None
        return point if metres is None else coarsen(point, metres)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PrecisionLadder({', '.join(a for a, _ in self._rungs)})"


async def resolve_precision(
    request: Any,
    authorizer: Any,
    ladder: PrecisionLadder,
    resource: object,
) -> float | None | _Withheld:
    """Ask `ladder`'s rungs in order and return the first permitted resolution.

    Returns the rung's metres (`None` for exact), or `WITHHELD` when no rung
    permits. Finest first and **first permit wins**, so a caller entitled to
    the exact position never pays for the coarser questions below it.

    The result is cached on `request.state` per `(ladder, resource)`, because a
    list response asks for every row and the answer cannot differ between them
    within one request -- the same argument that resolves flags and regions
    once. That cache is also what keeps a ladder from turning one list response
    into `rows x rungs` authorization calls.

    A denied rung is an ordinary Cedar denial, so a policy set that says
    nothing about these actions withholds everything: default deny reaches the
    resolution as well as the verdict.
    """
    slot = f"_precision_{id(ladder):x}_{id(resource):x}"

    async def ask() -> float | None | _Withheld:
        for action, metres in ladder:
            decision = await authorizer.authorize(
                request, PolicyRequirement(action=action, resource=resource)
            )
            if getattr(decision, "allowed", False) is True:
                return metres
        return WITHHELD

    # `resolve_once_async` reads with a sentinel, so the answer no longer needs
    # boxing to keep `None` (exact) distinguishable from a cache miss -- which
    # is the one confusion here that would publish a precise coordinate to a
    # caller entitled to none.
    return await resolve_once_async(request, slot, ask)


def _as_rung(position: int, rung: Any) -> tuple[str, float | None]:
    """Read one `(action, metres)` rung, refusing anything else by name."""
    if not isinstance(rung, tuple | list) or len(rung) != 2:
        raise ValueError(
            f"PrecisionLadder rung {position} must be an (action, metres) pair; got {rung!r}"
        )
    action, metres = rung
    if not isinstance(action, str) or not action:
        raise ValueError(
            f"PrecisionLadder rung {position} needs a non-empty action name; got {action!r}"
        )
    if metres is None:
        return action, None
    if not isinstance(metres, int | float) or isinstance(metres, bool):
        raise ValueError(
            f"PrecisionLadder rung {action!r} needs numeric metres or None for "
            f"exact; got {metres!r}"
        )
    metres = float(metres)
    if not math.isfinite(metres) or metres <= 0.0:
        raise ValueError(
            f"PrecisionLadder rung {action!r} needs a positive finite resolution; got {metres!r}"
        )
    return action, metres
