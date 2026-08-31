from __future__ import annotations

import pytest

from wreath._auth.cedar_schema import _without_comments
from wreath.authorization import CedarSchema


def test_comment_markers_inside_action_names_remain_text() -> None:
    schema = CedarSchema('action "https://example.test/a/*literal*/b";')

    assert schema.actions == ("https://example.test/a/*literal*/b",)


def test_schema_comments_are_removed_outside_strings() -> None:
    schema = CedarSchema(
        '/* lead */ action "read"; // between declarations\n'
        'action "write" in [Action::"read"]; /* tail */'
    )

    assert schema.actions == ("read", "write")
    assert schema.action_parents("write") == ("read",)


def test_comment_scanner_preserves_escaped_strings_and_ordinary_slashes() -> None:
    source = '"quote\\\"//still text" / value'

    assert _without_comments(source) == source


def test_comment_scanner_keeps_tokens_separate_and_handles_an_eof_line_comment() -> None:
    assert _without_comments("left/* hidden */right// tail") == "left right "


def test_unclosed_block_comment_is_refused_at_the_scanner_boundary() -> None:
    with pytest.raises(ValueError, match=r"missing '\*/'"):
        _without_comments("action /* unclosed")
