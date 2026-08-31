from __future__ import annotations

import pytest

from wreath._safe_pattern import UnsafePatternError
from wreath.binding import Field


def test_field_refuses_backtracking_patterns_at_declaration_time() -> None:
    with pytest.raises(UnsafePatternError, match=r"\^\(a\+\)\+\$"):
        Field(pattern=r"^(a+)+$")
