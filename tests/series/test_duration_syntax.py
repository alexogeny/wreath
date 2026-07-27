"""The two compact duration spellings are one syntax, asserted not asserted-in-prose.

`series.py` carried a comment saying its compact spelling was "the same compact
spelling ``ChunkedPass(within=...)`` takes, so one codebase has one duration
syntax". It was not: `series` accepted ``d`` and `_passes.duration` did not, so
``seal(after="3d")`` parsed and ``Rows(within="3d")`` raised.

A claim spanning two modules is one edit from being false again, and prose does
not fail a build. These tests do.
"""

from __future__ import annotations

import pytest

from wreath._passes import duration as _passes_duration
from wreath._passes.keyset import PassDeclarationError
from wreath.series import _COMPACT_SCALE


def test_the_two_scale_tables_are_the_same_set() -> None:
    """Same units, same values -- not merely overlapping."""
    assert _COMPACT_SCALE == _passes_duration._SCALE


@pytest.mark.parametrize("unit", sorted(_COMPACT_SCALE))
def test_every_series_unit_is_accepted_by_the_pass_parser(unit: str) -> None:
    """The direction the old comment got wrong: `d` parsed in one and not the other."""
    seconds = _passes_duration.seconds(f"3{unit}", what="test")
    assert seconds == pytest.approx(3 * _COMPACT_SCALE[unit])


def test_a_day_is_a_fixed_number_of_seconds() -> None:
    """Why `d` is admissible where `mo`/`y` are not.

    `temporal.parse_duration` refuses months and years because they are not a
    fixed span, and `Series.compare(previous=Bucket)` depends on that refusal.
    A day is 86,400 seconds by definition here -- these are elapsed budgets, not
    calendar arithmetic, so no DST reasoning applies.
    """
    assert _passes_duration.seconds("1d", what="test") == 86_400.0


@pytest.mark.parametrize("bad", ["1mo", "1y", "1w", "3 fortnights"])
def test_calendar_units_are_still_refused(bad: str) -> None:
    """Adding `d` must not have opened the door to units with no fixed length."""
    with pytest.raises(PassDeclarationError):
        _passes_duration.seconds(bad, what="test")
