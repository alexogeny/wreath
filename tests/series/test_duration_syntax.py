from __future__ import annotations

import pytest

from wreath._passes import duration as _passes_duration
from wreath._passes.keyset import PassDeclarationError
from wreath.series import _COMPACT_SCALE


def test_the_two_scale_tables_are_the_same_set() -> None:
    assert _COMPACT_SCALE == _passes_duration._SCALE


@pytest.mark.parametrize("unit", sorted(_COMPACT_SCALE))
def test_every_series_unit_is_accepted_by_the_pass_parser(unit: str) -> None:
    seconds = _passes_duration.seconds(f"3{unit}", what="test")
    assert seconds == pytest.approx(3 * _COMPACT_SCALE[unit])


def test_a_day_is_a_fixed_number_of_seconds() -> None:
    assert _passes_duration.seconds("1d", what="test") == 86_400.0


@pytest.mark.parametrize("bad", ["1mo", "1y", "1w", "3 fortnights"])
def test_calendar_units_are_still_refused(bad: str) -> None:
    with pytest.raises(PassDeclarationError):
        _passes_duration.seconds(bad, what="test")
