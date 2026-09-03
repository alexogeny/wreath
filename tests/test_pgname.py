from __future__ import annotations

from typing import Any

import pytest

from wreath._pgname import quote_identifier, validate_identifier


def test_quoted_identifier_validation_refuses_a_non_string() -> None:
    value: Any = object()

    with pytest.raises(ValueError, match="channel must be 1..63 bytes"):
        validate_identifier(value, "channel")


def test_identifier_quoting_refuses_a_non_string() -> None:
    value: Any = object()

    with pytest.raises(ValueError, match="unusable SQL identifier"):
        quote_identifier(value)


def test_identifier_quoting_refuses_an_empty_name() -> None:
    with pytest.raises(ValueError, match="unusable SQL identifier"):
        quote_identifier("")


def test_identifier_quoting_refuses_a_nul_byte() -> None:
    with pytest.raises(ValueError, match="unusable SQL identifier"):
        quote_identifier("bad\x00name")
