"""`normalize_key`'s containment gate, proved against its own definition.

The gate is a security control: it is the single place a traversal or a control
character is refused before a key reaches a filesystem or a URL. Its per-segment
scan was a per-character generator expression and is now one compiled-regex
search, which is 3-4x faster and would be a vulnerability rather than a
regression if it accepted one input more.

So these tests are differential rather than exemplary. `_reference` below is the
predicate as it was shipped -- `any(ord(c) < 0x20 or ord(c) == 0x7F for c in
part)`, spelled out longhand so no import can quietly make it agree -- and the
corpus is generated: every C0 code point and DEL in every position of a segment,
the traversal cases the gate exists for, the empty and dot segments, and the
non-ASCII ranges an over-eager character class would swallow (C1 controls,
U+2028/U+2029, NBSP, astral planes). Both the verdict *and* the message are
compared, because every refusal here names the key and a test that asserted only
that would pass on whichever branch fired.
"""
from __future__ import annotations

import pytest

from wreath.objects import ObjectError, normalize_key


def _reference(key: object) -> str:
    """`normalize_key` as it read before the regex, character by character."""
    if not isinstance(key, str):
        raise ObjectError(f"object key must be a string, not {type(key).__name__}")
    if not key:
        raise ObjectError("empty object key")
    if key.startswith("/"):
        raise ObjectError(f"absolute object key not allowed: {key!r}")
    parts = []
    for part in key.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ObjectError(f"object key escapes the store: {key!r}")
        control = False
        for character in part:
            if ord(character) < 0x20 or ord(character) == 0x7F:
                control = True
                break
        if control:
            raise ObjectError(f"control character in object key: {key!r}")
        parts.append(part)
    if not parts:
        raise ObjectError(f"object key resolves to nothing: {key!r}")
    return "/".join(parts)


def _outcome(fn, key):
    """`("ok", value)` or `("refused", message)` -- never an exception."""
    try:
        return ("ok", fn(key))
    except ObjectError as exc:
        return ("refused", str(exc))


def _corpus() -> list[str]:
    keys: list[str] = []

    # Every C0 code point and DEL, in every position of a segment that is
    # otherwise ordinary, and alone as a whole segment.
    for code in [*range(0x00, 0x20), 0x7F]:
        char = chr(code)
        keys += [
            char,
            f"{char}photos/a.png",
            f"photos/{char}a.png",
            f"photos/a{char}.png",
            f"photos/a.png{char}",
            f"photos/{char}/a.png",
            f"tenants/8f3a/{char}",
            f"a/b/c/d/e/f/g/h{char}",
        ]

    # Every printable ASCII code point, 0x20 through 0x7E. This is the half
    # that catches an off-by-one at the *top* of the range: a class of
    # `[\x00-\x20\x7f]` disagrees with the shipped predicate on nothing above
    # unless a key with a space in it is actually in the corpus.
    for code in range(0x20, 0x7F):
        char = chr(code)
        keys += [
            char,
            f"photos/a{char}b.png",
            f"{char}photos/a.png",
            f"photos/{char}",
        ]

    # The traversal and containment cases the gate exists for.
    keys += [
        "..",
        "../x",
        "a/../b",
        "a/../../b",
        "..\\x",
        "a\\..\\b",
        "/abs",
        "/",
        "//",
        "///",
        "",
        ".",
        "./",
        "./.",
        "a/./b",
        "a//b",
        "a/",
        "/a/..",
        "...",
        "....",
        "..a",
        "a..",
        "a/..b/c",
        "\\",
        "\\abs",
        "a\\b",
        "photos/4711.png",
        "tenants/8f3a1c2d/2026/08/02/original.png",
    ]

    # Non-ASCII. None of these is a C0 control or DEL, so every one must be
    # accepted -- this is the half a sloppier character class would break.
    for code in [
        *range(0x80, 0xA0),  # C1 controls: above the range, and stay legal
        0x00A0,  # NBSP
        0x0085,  # NEL, which some line-oriented classes treat as a break
        0x00FF,
        0x0100,
        0x0430,  # Cyrillic
        0x1E9E,
        0x2028,  # LINE SEPARATOR
        0x2029,  # PARAGRAPH SEPARATOR
        0x200B,  # ZERO WIDTH SPACE
        0x3042,  # Hiragana
        0xFEFF,  # BOM
        0xFFFD,
        0x10000,  # first astral code point
        0x1F600,  # emoji
        0x10FFFF,  # last code point
    ]:
        char = chr(code)
        keys += [char, f"photos/{char}.png", f"{char}/a.png", f"a{char}b/c"]

    # Mixed: a legal non-ASCII character beside an illegal control one.
    keys += [
        "photos/é\x00.png",
        "photos/\U0001f600\x7f.png",
        " /\x1f",
        "\U0001f600/ok",
    ]
    return keys


CORPUS = _corpus()


def test_objects_normalize_key_matches_the_per_character_predicate():
    """Verdict and message identical to the scan the regex replaced.

    One test over the whole corpus rather than 864 parametrised ones: the
    failure names every key that disagreed, which is more useful than the first
    one alphabetically, and the suite does not grow by a thousand ids.
    """
    disagreed = [
        (key, _outcome(normalize_key, key), _outcome(_reference, key))
        for key in CORPUS
        if _outcome(normalize_key, key) != _outcome(_reference, key)
    ]
    assert disagreed == []


def test_objects_normalize_key_corpus_exercises_both_verdicts():
    """The corpus would prove nothing if it were all refusals, or all keys."""
    outcomes = [_outcome(normalize_key, key)[0] for key in CORPUS]
    assert outcomes.count("ok") > 50
    assert outcomes.count("refused") > 50


def test_objects_normalize_key_refuses_every_c0_and_del_by_name():
    """The control-character branch fires, rather than some earlier refusal."""
    for code in [*range(0x00, 0x20), 0x7F]:
        key = f"photos/a{chr(code)}b.png"
        with pytest.raises(ObjectError) as excinfo:
            normalize_key(key)
        assert str(excinfo.value) == f"control character in object key: {key!r}"


def test_objects_normalize_key_accepts_the_boundary_code_points():
    """0x20 and 0x21 are legal; 0x7E is legal and 0x7F is not."""
    assert normalize_key("a b") == "a b"
    assert normalize_key("a!b") == "a!b"
    assert normalize_key("a~b") == "a~b"
    with pytest.raises(ObjectError, match="control character"):
        normalize_key("a\x7fb")
    with pytest.raises(ObjectError, match="control character"):
        normalize_key("a\x1fb")


def test_objects_normalize_key_scans_every_segment_not_only_the_first():
    """A control character in a later segment is refused just the same."""
    with pytest.raises(ObjectError, match="control character"):
        normalize_key("a/b/c/d/e/f/g/h/i/\x00")
