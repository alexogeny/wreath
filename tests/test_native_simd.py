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
