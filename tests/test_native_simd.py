"""Every arm of the dispatched byte scanners answers what the scalar one does.

The dispatcher in `src/wreath/_native/simd.h` picks the widest arm the CPU
offers, which means the narrower ones stop executing the moment a machine is
new enough -- and the widest one never executes on a machine that is older.
Neither gap is visible from behaviour: a scan that stops in the wrong place
still returns *a* number, and the JSON, template, and header suites pass while
one arm is wrong for inputs they happen not to contain.

So the arms are named and crossed here. Two live defects were caught this way
while the header was being written, both in SWAR: a `seen_high` accumulator
that was dropped on an early return, and a mask built from a SWAR equality
test, whose set bits mark *that* a byte matched and not *which* -- it cleared
the flag for a neighbouring 0x08, and a control byte that must be refused
walked through the header-value scan.

An arm the build cannot reach reports `None` and is skipped; that is a real
capability of the machine, not of Wreath.
"""

from __future__ import annotations

import random

import pytest

from wreath._native import _core

# Bytes every scanner has an opinion about, so a random string built from them
# exercises stops, near-stops, and the multi-byte UTF-8 lead bytes that must
# not be mistaken for one.
INTERESTING = bytes(
    [
        0x00, 0x08, 0x09, 0x0A, 0x0D, 0x1F, 0x20, 0x21, 0x22, 0x25, 0x26,
        0x27, 0x2F, 0x3C, 0x3E, 0x5C, 0x61, 0x7E, 0x7F, 0x80, 0xC3, 0xA9,
        0xFF,
    ]
)

KINDS = ("json", "html", "value", "dkim")


@pytest.fixture(scope="module")
def arms() -> tuple[str, ...]:
    available = _core.simd_arms()
    assert available[0] == "scalar", "scalar is the definition and is always present"
    return available


def _cases() -> list[bytes]:
    """Lengths around every block boundary, plus randomised bodies.

    The boundaries matter more than the volume: an arm handles 32, 16, 8, and 1
    bytes per step, so an off-by-one shows up at 15/16/17 and 31/32/33 and
    nowhere else.
    """
    cases: list[bytes] = [b"", b"a", b'"', b"\x00", b"\t", b"\x7f"]
    for n in (*range(0, 40), 63, 64, 65, 127, 128, 255, 256, 1023):
        cases.append(b"a" * n)
        cases.append(b"a" * n + b'"')
        cases.append(b'"' + b"a" * n)
    rng = random.Random(20260731)
    for _ in range(2000):
        n = rng.randrange(0, 300)
        cases.append(bytes(rng.choice(INTERESTING) for _ in range(n)))
    for _ in range(500):
        n = rng.randrange(0, 300)
        cases.append(bytes(rng.randrange(256) for _ in range(n)))
    return cases


CASES = _cases()


@pytest.mark.parametrize("kind", KINDS)
def test_every_arm_agrees_with_scalar(kind: str, arms: tuple[str, ...]) -> None:
    for data in CASES:
        expected = _core.simd_probe(kind, "scalar", data)
        for arm in arms[1:]:
            got = _core.simd_probe(kind, arm, data)
            if got is None:
                continue
            assert got == expected, f"{kind}/{arm} disagreed on {data!r}"


def test_mask_arms_agree_with_scalar(arms: tuple[str, ...]) -> None:
    rng = random.Random(11)
    for data in CASES:
        key = bytes(rng.randrange(256) for _ in range(4))
        expected = _core.simd_probe("xor", "scalar", data, key)
        for arm in arms[1:]:
            got = _core.simd_probe("xor", arm, data, key)
            if got is None:
                continue
            assert got == expected, f"xor/{arm} disagreed on {len(data)} bytes"


def test_masking_is_its_own_inverse(arms: tuple[str, ...]) -> None:
    """The property the arms exist to preserve, stated without a reference."""
    rng = random.Random(5)
    for arm in arms:
        for n in (0, 1, 7, 8, 15, 16, 31, 32, 33, 1000):
            data = bytes(rng.randrange(256) for _ in range(n))
            key = bytes(rng.randrange(256) for _ in range(4))
            once = _core.simd_probe("xor", arm, data, key)
            if once is None:
                continue
            assert _core.simd_probe("xor", arm, once, key) == data


@pytest.mark.parametrize("kind", KINDS)
def test_run_never_passes_a_stop(kind: str, arms: tuple[str, ...]) -> None:
    """A run stops *at* the first byte of interest, never past it.

    Stated against the definition of each scan rather than against the scalar
    arm, so a scalar arm that was wrong in the same direction could not hide
    behind agreement.
    """
    def is_stop(byte: int) -> bool:
        if kind == "json":
            return byte < 0x20 or byte in (0x22, 0x5C)
        if kind == "html":
            return byte in (0x26, 0x3C, 0x3E, 0x22, 0x27)
        if kind == "dkim":
            return byte in (0x20, 0x09, 0x0D, 0x0A)
        return (byte < 0x20 and byte != 0x09) or byte == 0x7F

    for data in CASES:
        for arm in arms:
            run = _core.simd_probe(kind, arm, data)
            if run is None:
                continue
            if kind == "json":
                run, _seen = run
            assert not any(is_stop(b) for b in data[:run]), f"{kind}/{arm} ran past a stop"
            assert run == len(data) or is_stop(data[run]), f"{kind}/{arm} stopped early"


def test_b64url_arms_agree_with_scalar(arms: tuple[str, ...]) -> None:
    """Every base64url arm decodes, and rejects, exactly what scalar does.

    Both halves matter. A vector arm that accepts a byte outside the alphabet
    would let a malformed JWT segment through to the JSON parser, and one that
    rejects a legal character would refuse valid tokens; only crossing accept
    *and* reject against the definition covers both.
    """
    import base64

    rng = random.Random(31337)
    cases: list[bytes] = []
    for n in range(0, 200):
        cases.append(base64.urlsafe_b64encode(bytes(n)).rstrip(b"="))
    for _ in range(3000):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 120)))
        cases.append(base64.urlsafe_b64encode(raw).rstrip(b"="))
    # hostile: the alphabet plus the bytes most likely to be mistaken for it
    hostile = b"ABCzab019-_+/=. \x00\xff\x80\x7f"
    for _ in range(3000):
        cases.append(bytes(rng.choice(hostile) for _ in range(rng.randrange(0, 120))))

    for data in cases:
        expected = _core.simd_probe("b64", "scalar", data)
        for arm in arms[1:]:
            got = _core.simd_probe("b64", arm, data)
            if got is None:
                continue
            assert got == expected, f"b64/{arm} disagreed on {data[:48]!r}"


def test_b64url_matches_the_standard_library(arms: tuple[str, ...]) -> None:
    """The scalar definition is itself checked, against an outside reference.

    Agreement between arms only proves they are the same; it does not prove
    they are right. `base64` decides that.
    """
    import base64

    rng = random.Random(4242)
    for _ in range(3000):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 90)))
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
        for arm in arms:
            got = _core.simd_probe("b64", arm, encoded)
            if got is None:
                continue
            assert got == raw


def test_b64_encode_arms_agree_and_match_the_standard_library(
    arms: tuple[str, ...],
) -> None:
    """The encoder's arms, against each other and against `base64`.

    The vector arm builds each character by adding an offset chosen from a
    sixteen-entry table indexed by an arithmetic bucket, not by the six-bit
    value. Writing that table in value order instead produced 736,000
    differential failures on its first run -- which is the whole argument for
    having this test rather than trusting the transcription.
    """
    import base64

    rng = random.Random(90210)
    cases = [bytes(n) for n in range(0, 200)]
    for _ in range(3000):
        cases.append(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 400))))

    for data in cases:
        expected = base64.b64encode(data)
        for arm in arms:
            got = _core.simd_probe("b64enc", arm, data)
            if got is None:
                continue
            assert got == expected, f"b64enc/{arm} disagreed on {len(data)} bytes"


def test_b64encode_entry_point_matches_the_standard_library() -> None:
    """The public shape: both alphabets, padded and not, returning `str`."""
    import base64

    rng = random.Random(5150)
    for _ in range(2000):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
        assert _core.b64encode(raw) == base64.b64encode(raw).decode("ascii")
        assert _core.b64encode(raw, True) == base64.urlsafe_b64encode(raw).decode("ascii")
        assert _core.b64encode(raw, True, False) == (
            base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        )
        assert isinstance(_core.b64encode(raw), str)


def test_hex_arms_agree_and_match_the_standard_library(arms: tuple[str, ...]) -> None:
    """Hex decoding, arm against arm and against `bytes.fromhex`.

    This is the path every `bytea` column takes: PostgreSQL sends binary in
    text format as two characters per byte, so the scan is as long as the
    value. Rejection matters as much as the decode -- an arm that accepted a
    non-hex byte would hand silently wrong bytes to the application.
    """
    rng = random.Random(6180)
    cases: list[bytes] = [b"", b"0", b"00", b"ff", b"FF", b"0g", b"g0", b"abcdef"]
    for _ in range(3000):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
        cases.append(raw.hex().encode())
        cases.append(raw.hex().upper().encode())
    hostile = b"0123456789abcdefABCDEFghxyz \x00\xff"
    for _ in range(3000):
        cases.append(bytes(rng.choice(hostile) for _ in range(rng.randrange(0, 200))))

    for data in cases:
        expected = _core.simd_probe("hex", "scalar", data)
        for arm in arms[1:]:
            got = _core.simd_probe("hex", arm, data)
            if got is None:
                continue
            assert got == expected, f"hex/{arm} disagreed on {data[:48]!r}"
        # and the scalar definition itself, against an outside reference
        try:
            reference = bytes.fromhex(data.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            reference = False
        if isinstance(reference, bytes) and b" " not in data:
            assert expected == reference


def test_json_run_reports_non_ascii_only_from_bytes_it_passed(
    arms: tuple[str, ...],
) -> None:
    """`seen_high` decides between a one-byte str and a UTF-8 decode.

    A byte at or beyond the stop has not been consumed, so counting it would
    send an all-ASCII string down the decoding path -- correct, but slower --
    and, read the other way, a dropped high bit would build a one-byte str from
    bytes that are not ASCII. The second is the one that corrupts.
    """
    for data in CASES:
        for arm in arms:
            probe = _core.simd_probe("json", arm, data)
            if probe is None:
                continue
            run, seen = probe
            expected = any(b >= 0x80 for b in data[:run])
            assert bool(seen) == expected, f"json/{arm} seen_high wrong on {data[:48]!r}"


# --- hash-table control bytes ----------------------------------------------
#
# The group scan behind `wreath.kv`. It answers two questions -- which lanes
# carry this tag, and which lanes are free -- and both answers must be *exact*
# rather than merely conservative, which is what separates it from the run
# scanners above. An over-reported tag lane costs one wasted key comparison and
# nothing else; an over-reported empty lane ends a probe early and loses a key
# that is really in the table. The SWAR arm cannot use `wreath_swar_eq` for that
# reason: its borrows over-report position, and it disagreed with the byte loop
# on 358 of 200000 random groups when it was tried.

CTRL_GROUP = 32

#: The values a control byte actually takes, so a random group looks like a real
#: one: empty, deleted, and the 0x00-0x7F tag range.
CTRL_INTERESTING = (0x80, 0xFE, 0x00, 0x01, 0x7F, 0x7E, 0x40)


def _ctrl_groups() -> list[bytes]:
    rng = random.Random(4242)
    groups = [
        bytes([0x80]) * CTRL_GROUP,          # a wholly empty group
        bytes([0xFE]) * CTRL_GROUP,          # a wholly tombstoned group
        bytes(range(CTRL_GROUP)),            # every lane a distinct low tag
        bytes([0x7F]) * CTRL_GROUP,          # every lane the same tag
    ]
    for _ in range(400):
        groups.append(
            bytes(
                rng.choice(CTRL_INTERESTING)
                if rng.random() < 0.7
                else rng.randrange(256)
                for _ in range(CTRL_GROUP)
            )
        )
    return groups


CTRL_GROUPS = _ctrl_groups()


def test_ctrl_tag_scan_arms_agree_with_scalar(arms: tuple[str, ...]) -> None:
    for group in CTRL_GROUPS:
        for needle in (0x80, 0xFE, 0x00, 0x7F, 0x2A):
            expected = _core.simd_probe("ctrl", "scalar", group, bytes([needle]))
            for arm in arms[1:]:
                got = _core.simd_probe("ctrl", arm, group, bytes([needle]))
                if got is None:
                    continue
                assert got == expected, (
                    f"ctrl/{arm} disagreed on needle {needle:#04x} over {group.hex()}"
                )


def test_ctrl_free_scan_arms_agree_with_scalar(arms: tuple[str, ...]) -> None:
    for group in CTRL_GROUPS:
        expected = _core.simd_probe("ctrl", "scalar", group)
        for arm in arms[1:]:
            got = _core.simd_probe("ctrl", arm, group)
            if got is None:
                continue
            assert got == expected, f"ctrl/{arm} free scan disagreed on {group.hex()}"


def test_ctrl_scans_are_exact_against_a_plain_byte_loop(arms: tuple[str, ...]) -> None:
    """Stated without reference to the scalar arm, which could itself be wrong."""
    for group in CTRL_GROUPS:
        free = sum(1 << i for i, byte in enumerate(group) if byte & 0x80)
        for needle in (0x80, 0xFE, 0x13):
            tagged = sum(1 << i for i, byte in enumerate(group) if byte == needle)
            for arm in arms:
                match = _core.simd_probe("ctrl", arm, group, bytes([needle]))
                if match is not None:
                    assert match == tagged, f"ctrl/{arm} tag mask is not exact"
                empty = _core.simd_probe("ctrl", arm, group)
                if empty is not None:
                    assert empty == free, f"ctrl/{arm} free mask is not exact"


def test_a_tag_scan_can_never_match_a_free_lane(arms: tuple[str, ...]) -> None:
    """The invariant that lets one kernel answer both questions.

    A stored tag is the low seven bits of a hash, so its high bit is clear;
    empty (0x80) and deleted (0xFE) both have it set. If that ever stopped
    holding, a probe would confirm a key against an entry that is not there.
    """
    for group in CTRL_GROUPS:
        free = _core.simd_probe("ctrl", "scalar", group)
        for tag in (0x00, 0x01, 0x40, 0x7F):
            for arm in arms:
                match = _core.simd_probe("ctrl", arm, group, bytes([tag]))
                if match is None:
                    continue
                assert match & free == 0, f"ctrl/{arm} matched a free lane on tag {tag:#04x}"


def test_a_group_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(ValueError, match="32-byte group"):
        _core.simd_probe("ctrl", "scalar", b"\x80" * 16, b"\x00")


# --- substring search ------------------------------------------------------
#
# The one kernel that answers "where" rather than "which bytes". An arm that
# reported a *later* match than the scalar one would still look like a match to
# its caller, and would split a multipart body at the wrong offset -- so these
# cross against `bytes.find`, which is a reference outside Wreath entirely,
# rather than only against the scalar arm.

FIND_ALPHABET = b"ab\r\n-"


def _find_cases() -> list[tuple[bytes, bytes]]:
    rng = random.Random(0x5EA4C)
    cases: list[tuple[bytes, bytes]] = [
        (b"", b"x"),                       # empty haystack
        (b"abc", b""),                     # empty needle
        (b"abc", b"abcd"),                 # needle longer than haystack
        (b"abc", b"abc"),                  # exact
        (b"\r\n\r\n", b"\r\n\r\n"),        # the HTTP head terminator, exactly
        (b"x" * 64 + b"\r\n\r\n", b"\r\n\r\n"),
        (b"\r\n\r\n" + b"x" * 64, b"\r\n\r\n"),
        (b"\r" * 200, b"\r\n"),            # first byte matches everywhere, never the pair
        (b"a" * 100, b"aa"),               # overlapping matches
        (b"x" * 31 + b"ab" + b"x" * 31, b"ab"),   # straddles a 32-byte stride
        (b"x" * 63 + b"ab", b"ab"),               # in the tail after the last stride
    ]
    for _ in range(600):
        hay = bytes(rng.choice(FIND_ALPHABET) for _ in range(rng.randrange(0, 300)))
        length = rng.randrange(1, 10)
        if hay and rng.random() < 0.5 and len(hay) > length:
            start = rng.randrange(len(hay) - length)
            needle = hay[start : start + length]
        else:
            needle = bytes(rng.choice(FIND_ALPHABET) for _ in range(length))
        cases.append((hay, needle))
    return cases


FIND_CASES = _find_cases()


def test_find_arms_agree_with_the_standard_library(arms: tuple[str, ...]) -> None:
    for hay, needle in FIND_CASES:
        if not needle:
            continue  # an empty needle is not a question the kernel is asked
        expected = hay.find(needle)
        for arm in arms:
            got = _core.simd_probe("find", arm, hay, needle)
            if got is None:
                continue
            assert got == expected, (
                f"find/{arm} said {got}, bytes.find said {expected}, "
                f"for {needle!r} in {hay[:64]!r}"
            )


def test_find_never_reports_a_later_match_than_the_first(arms: tuple[str, ...]) -> None:
    """Stated as a property, so it holds even if `bytes.find` were wrong.

    A search that finds *a* match rather than the *first* one passes every
    round-trip test and still cuts a multipart body in the wrong place.
    """
    for hay, needle in FIND_CASES:
        if not needle:
            continue
        for arm in arms:
            got = _core.simd_probe("find", arm, hay, needle)
            if got is None or got < 0:
                continue
            assert hay[got : got + len(needle)] == needle, f"find/{arm} matched nothing"
            assert needle not in hay[: got + len(needle) - 1], (
                f"find/{arm} skipped an earlier match"
            )


def test_a_needle_that_is_absent_is_reported_absent(arms: tuple[str, ...]) -> None:
    for arm in arms:
        got = _core.simd_probe("find", arm, b"a" * 200, b"ab")
        if got is None:
            continue
        assert got == -1


def test_the_dispatched_search_agrees_with_the_arms_on_both_sides_of_the_threshold(
    arms: tuple[str, ...],
) -> None:
    """`wreath_memmem` routes short needles to the kernel and long ones to libc.

    The threshold is a performance decision, so the two sides must be
    indistinguishable in behaviour -- otherwise a boundary one byte longer
    would parse differently.
    """
    body = (b"x" * 63 + b"\r") * 40
    for length in (2, 4, 8, 15, 16, 17, 24, 48, 70):
        needle = (b"\r\n--" + bytes(range(97, 123)) * 4)[:length]
        for hay in (body + needle, body + needle + body, body):
            assert _core.simd_probe("memmem", "scalar", hay, needle) == hay.find(needle)


# --- declaration order, for the arms this machine cannot compile -----------


def test_no_arm_calls_a_helper_defined_below_it() -> None:
    """`simd.h` must compile on every target, including ones absent here.

    Each architecture's arms live behind an `#if`, so a NEON-only mistake is
    preprocessed away on x86 and an x86-only mistake is preprocessed away on
    ARM. That is not a warning-level problem: C assumes an undeclared function
    returns `int`, which then *conflicts* with the real `static inline
    ptrdiff_t` definition further down, and the translation unit fails to
    compile outright.

    This found three of them -- `wreath_html_run_neon`, `wreath_value_run_neon`
    and `wreath_xor_mask_neon` each calling their SWAR tail about 150 lines
    before it was declared. The extension would not have built on aarch64 at
    all, and nothing on an x86 machine would have said so.
    """
    import pathlib
    import re

    header = pathlib.Path(__file__).resolve().parents[1] / "src/wreath/_native/simd.h"
    text = header.read_text(encoding="utf-8")

    def line_of(offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    #: A definition starts at column zero with the name, because the file puts
    #: the return type on its own line throughout.
    definitions = {
        m.group(1): m.start()
        for m in re.finditer(r"^(wreath_\w+)\s*\(", text, re.M)
    }
    #: A forward declaration is a whole `static inline ... ;` statement at the
    #: start of a line. Anchoring on that is what distinguishes it from a call:
    #: an earlier version matched any `wreath_x(...);`, which is also the shape
    #: of every ordinary call statement, so each call registered *itself* as the
    #: point the function became available and the check could never fail.
    #: Verified by deleting a real declaration and watching this go red.
    declarations: dict[str, int] = {}
    blanked = list(text)
    for match in re.finditer(
        r"^static\s+inline\s+[^;{]*?\b(wreath_\w+)\s*\([^;{]*\)\s*;", text, re.M | re.S
    ):
        declarations.setdefault(match.group(1), match.start())
        for index in range(match.start(), match.end()):
            if blanked[index] != "\n":
                blanked[index] = " "
    source = "".join(blanked)

    available = dict(definitions)
    for name, offset in declarations.items():
        available[name] = min(available.get(name, offset), offset)

    current: str | None = None
    current_at = -1
    offenders: list[str] = []
    for match in re.finditer(r"^(wreath_\w+)\s*\(|\b(wreath_\w+)\s*\(", source, re.M):
        if match.group(1) is not None:
            current, current_at = match.group(1), match.start()
            continue
        callee = match.group(2)
        if current is None or callee == current or callee not in available:
            continue
        if available[callee] > match.start():
            offenders.append(
                f"{current} (line {line_of(current_at)}) calls {callee} at line "
                f"{line_of(match.start())}, first available at line "
                f"{line_of(available[callee])}"
            )

    assert not offenders, (
        "simd.h calls a helper before it is declared, which is a compile error "
        "on whichever target the calling arm belongs to:\n  "
        + "\n  ".join(dict.fromkeys(offenders))
    )
