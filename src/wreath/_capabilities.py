"""What answers a word the reader already knows -- the `wreath capabilities` lookup.

Wreath ships fifty-seven subsystems, and the expensive mistake it invites is not
misusing one: it is hand-rolling a rate limiter, an idempotency key, a chart
bucket or a distributed lock that was already in the package. That mistake is
made in the first ten minutes, by somebody who does not yet know the vocabulary
well enough to search for it, and it is made in the vocabulary they *do* know --
`celery`, `redis`, `alembic`.

So the lookup is a reverse index over exactly that: the distribution names the
manifest already lists in `replaces`, plus the subsystem and module names, plus
the capability sentences as a last resort.

**A word answered in several places returns all of them.** `redis` is locks and
jobs and memory and cache here, and stopping at the first is how somebody
reimplements the other three. Ranking, not filtering, is the whole design: a
match's `reason` says how strong it is and the caller sees the weak ones too.

The data is generated -- see `wreath._devtools.capability_index` for why the
manifest itself cannot be read at runtime, and `MAP015` for what keeps the copy
honest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ._capability_data import ROWS

__all__ = ["Capability", "Match", "Reason", "index", "lookup"]

#: How a term matched, strongest first. The order *is* the ranking -- `lookup`
#: sorts on this tuple's index, so adding a reason means deciding where it sits.
Reason = Literal["subsystem", "module", "replaces", "capability"]

_RANK: tuple[Reason, ...] = ("subsystem", "module", "replaces", "capability")


@dataclass(frozen=True, slots=True)
class Capability:
    """One subsystem, as the index carries it."""

    name: str
    sentence: str
    modules: tuple[str, ...]
    guides: tuple[str, ...]
    replaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Match:
    """One capability, and why the term reached it."""

    capability: Capability
    reason: Reason
    matched: str


def _build() -> tuple[Capability, ...]:
    return tuple(
        Capability(
            name=str(row["name"]),
            sentence=str(row["capability"]),
            modules=tuple(row["modules"]),  # type: ignore[arg-type]
            guides=tuple(row["guides"]),  # type: ignore[arg-type]
            replaces=tuple(row["replaces"]),  # type: ignore[arg-type]
        )
        for row in ROWS
    )


_INDEX = _build()


def index() -> tuple[Capability, ...]:
    """Every capability wreath ships, in manifest order."""
    return _INDEX


def lookup(term: str) -> tuple[Match, ...]:
    """Every capability the term reaches, strongest reason first.

    One capability yields at most one match, carrying its *strongest* reason: a
    term that is both a subsystem's name and a word in its own sentence has not
    found two things.
    """
    needle = term.strip().lower()
    if not needle:
        return ()
    # A word rather than a substring: `orm` must not match "platform", and the
    # prose tier is the one where that would otherwise happen constantly.
    word = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
    found: list[tuple[int, int, Match]] = []
    for position, capability in enumerate(_INDEX):
        matched = _reason(capability, needle, word)
        if matched is None:
            continue
        reason, token = matched
        found.append((_RANK.index(reason), position, Match(capability, reason, token)))
    return tuple(match for _, _, match in sorted(found, key=lambda row: row[:2]))


def _reason(
    capability: Capability, needle: str, word: re.Pattern[str],
) -> tuple[Reason, str] | None:
    """The strongest reason this capability answers the term, or `None`."""
    if capability.name == needle:
        return "subsystem", capability.name
    # `messaging` and `wreath.messaging` are the same question asked two ways.
    qualified = needle if needle.startswith("wreath.") else f"wreath.{needle}"
    for module in capability.modules:
        if module == qualified:
            return "module", module
    if needle in capability.replaces:
        return "replaces", needle
    if word.search(capability.sentence):
        return "capability", needle
    return None
