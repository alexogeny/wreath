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

KINDS = ("json", "html", "value")


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
