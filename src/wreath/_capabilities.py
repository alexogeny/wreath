from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ._capability_data import ROWS

__all__ = ["Capability", "Match", "Reason", "index", "lookup"]

Reason = Literal["subsystem", "module", "replaces", "capability"]

_RANK: tuple[Reason, ...] = ("subsystem", "module", "replaces", "capability")


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    sentence: str
    modules: tuple[str, ...]
    replaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Match:
    capability: Capability
    reason: Reason
    matched: str


def _build() -> tuple[Capability, ...]:
    return tuple(
        Capability(
            name=str(row["name"]),
            sentence=str(row["capability"]),
            modules=tuple(row["modules"]),  # type: ignore[arg-type]
            replaces=tuple(row["replaces"]),  # type: ignore[arg-type]
        )
        for row in ROWS
    )


_INDEX = _build()


def index() -> tuple[Capability, ...]:
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
    capability: Capability,
    needle: str,
    word: re.Pattern[str],
) -> tuple[Reason, str] | None:
    """The strongest reason this capability answers the term, or `None`."""
    if capability.name == needle:
        return "subsystem", capability.name
    qualified = needle if needle.startswith("wreath.") else f"wreath.{needle}"
    for module in capability.modules:
        if module == qualified:
            return "module", module
    if needle in capability.replaces:
        return "replaces", needle
    if word.search(capability.sentence):
        return "capability", needle
    return None
