"""Reference lookups behind a cachetools TTL, and a timestamp layer on arrow.

Both are load-bearing in the original and both have a wreath answer now — the
cache a *better* one, since a TTL is a guess and wreath clears on the committed
write instead. Kept in one module because that is how they arrived: a caching
helper that also normalises times.
"""
import arrow
from cachetools import LRUCache, TTLCache, cached

from .models import Paddock

# Reference data: changes a few times a season, read on nearly every request.
_paddocks = TTLCache(maxsize=256, ttl=900)

# Hot per-request lookups, bounded rather than expiring.
_grades = LRUCache(maxsize=1024)


@cached(_paddocks)
def paddock_label(paddock: Paddock) -> str:
    return f"{paddock.name} ({paddock.hectares:.1f} ha)"


@cached(_grades)
def grade_band(grade: int) -> str:
    if grade >= 4:
        return "prime"
    return "standard" if grade >= 2 else "novice"


def season_start(when=None):
    moment = arrow.get(when) if when else arrow.utcnow()
    return moment.floor("year")


def humanise(when) -> str:
    return arrow.get(when).humanize()


def now_iso() -> str:
    return arrow.now().isoformat()


def next_season(when=None):
    # A calendar shift, not a duration: the thing temporal refuses to guess at.
    return arrow.Arrow.fromdate(when).shift(months=6)
