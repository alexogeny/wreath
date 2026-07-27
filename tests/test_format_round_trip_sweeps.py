"""Property sweeps over the two binary formats, rather than chosen examples.

A single well-chosen example passing forever is the failure mode these guard
against. `format_duration` round-tripped correctly for `timedelta(hours=2,
minutes=5)` -- whole minutes, the one shape its float formatting handled -- and
was broken for 19254 of 20012 other durations. The same shape applies here, and
worse: a text formatter emitting nonsense usually raises somewhere, while a
binary encoder writing a subtly wrong byte produces something that decodes
cleanly into a *different value*.

Two formats, two different oracles:

* **MessagePack** is serialize-only in wreath -- responses are encoded and never
  read back -- so there is no in-repo inverse to round-trip against. The oracle
  is `_SpecDecoder` below, transcribed from the format spec and dispatching on a
  tag table rather than mirroring the encoder's branch order, so a shared
  misreading is less likely. The known-answer assertions in
  `test_msgpack_parity.py` guard the residual risk that oracle and encoder are
  wrong together.
* **The WFR1 container** has a real inverse, so it is swept for identity and for
  three refusal properties: truncation, trailing bytes, and bit corruption.

The exhaustive sweeps carry `@pytest.mark.fuzz` and run under `pytest -m ''`;
each leaves a fast representative sample in the default run, which is serial and
~3.5s and needs to stay that way.
"""

from __future__ import annotations

import math
import random
import struct

import pytest

from wreath._flight_schema import MetadataImage, NamedMeta, SchemaError
from wreath._pure.flight import (
    CELL_SIZE,
    SCHEMA_VERSION,
    decode_recording,
    encode_recording,
)
from wreath._pure.msgpack import packb

SEED = 20260727


# --------------------------------------------------------------------------
# MessagePack: an independent decoder, written from the spec
# --------------------------------------------------------------------------


class MsgpackDecodeError(Exception):
    """The encoder produced bytes this decoder will not accept."""


class _SpecDecoder:
    """Decode MessagePack by tag table. Deliberately not derived from `packb`."""

    def decode(self, data: bytes) -> object:
        value, used = self._read(data, 0)
        if used != len(data):
            raise MsgpackDecodeError(f"trailing bytes: used {used} of {len(data)}")
        return value

    def _need(self, data: bytes, pos: int, n: int) -> None:
        if pos + n > len(data):
            raise MsgpackDecodeError(f"truncated: need {n} at {pos}")

    def _read(self, data: bytes, pos: int) -> tuple[object, int]:
        self._need(data, pos, 1)
        tag = data[pos]
        pos += 1
        if tag <= 0x7F:  # positive fixint
            return tag, pos
        if tag >= 0xE0:  # negative fixint
            return tag - 0x100, pos
        if 0x80 <= tag <= 0x8F:
            return self._map(data, pos, tag & 0x0F)
        if 0x90 <= tag <= 0x9F:
            return self._array(data, pos, tag & 0x0F)
        if 0xA0 <= tag <= 0xBF:
            return self._str(data, pos, tag & 0x1F)
        if tag == 0xC0:
            return None, pos
        if tag == 0xC1:
            raise MsgpackDecodeError("0xc1 is never valid")
        if tag == 0xC2:
            return False, pos
        if tag == 0xC3:
            return True, pos
        if tag in (0xC4, 0xC5, 0xC6):
            n, pos = self._uint(data, pos, {0xC4: 1, 0xC5: 2, 0xC6: 4}[tag])
            self._need(data, pos, n)
            return data[pos : pos + n], pos + n
        if tag == 0xCA:
            self._need(data, pos, 4)
            return struct.unpack_from(">f", data, pos)[0], pos + 4
        if tag == 0xCB:
            self._need(data, pos, 8)
            return struct.unpack_from(">d", data, pos)[0], pos + 8
        if tag in (0xCC, 0xCD, 0xCE, 0xCF):
            return self._uint(data, pos, {0xCC: 1, 0xCD: 2, 0xCE: 4, 0xCF: 8}[tag])
        if tag in (0xD0, 0xD1, 0xD2, 0xD3):
            fmt = {0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}[tag]
            size = struct.calcsize(fmt)
            self._need(data, pos, size)
            return struct.unpack_from(fmt, data, pos)[0], pos + size
        if tag in (0xD9, 0xDA, 0xDB):
            n, pos = self._uint(data, pos, {0xD9: 1, 0xDA: 2, 0xDB: 4}[tag])
            return self._str(data, pos, n)
        if tag in (0xDC, 0xDD):
            n, pos = self._uint(data, pos, 2 if tag == 0xDC else 4)
            return self._array(data, pos, n)
        if tag in (0xDE, 0xDF):
            n, pos = self._uint(data, pos, 2 if tag == 0xDE else 4)
            return self._map(data, pos, n)
        raise MsgpackDecodeError(f"unsupported tag 0x{tag:02x}")

    def _uint(self, data: bytes, pos: int, width: int) -> tuple[int, int]:
        self._need(data, pos, width)
        return int.from_bytes(data[pos : pos + width], "big"), pos + width

    def _str(self, data: bytes, pos: int, n: int) -> tuple[str, int]:
        self._need(data, pos, n)
        try:
            return data[pos : pos + n].decode("utf-8"), pos + n
        except UnicodeDecodeError as exc:
            raise MsgpackDecodeError(f"str payload is not utf-8: {exc}") from exc

    def _array(self, data: bytes, pos: int, n: int) -> tuple[list, int]:
        out = []
        for _ in range(n):
            item, pos = self._read(data, pos)
            out.append(item)
        return out, pos

    def _map(self, data: bytes, pos: int, n: int) -> tuple[dict, int]:
        out: dict = {}
        for _ in range(n):
            key, pos = self._read(data, pos)
            value, pos = self._read(data, pos)
            try:
                out[key] = value
            except TypeError as exc:
                raise MsgpackDecodeError(
                    f"map key decodes to unhashable {type(key).__name__}"
                ) from exc
        return out, pos


def unpackb(data: bytes) -> object:
    return _SpecDecoder().decode(data)


def _same(a: object, b: object) -> bool:
    """Equality that treats NaN as itself and keeps -0.0 distinct from 0.0."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        if a == 0.0 and b == 0.0:
            return math.copysign(1, a) == math.copysign(1, b)
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, list):
        return len(a) == len(b) and all(
            _same(x, y) for x, y in zip(a, b, strict=True)
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return len(a) == len(b) and all(
            k in b and _same(v, b[k]) for k, v in a.items()
        )
    if isinstance(a, (bytes, bytearray, memoryview)) and isinstance(b, bytes):
        return bytes(a) == b
    return type(a) is type(b) and a == b


def _msgpack_domain(rng: random.Random, *, wide: bool) -> list[object]:
    """Values chosen so every width transition and both signs are covered."""
    cases: list[object] = [
        # integers: every width boundary the tag depends on, both signs
        0, 1, 0x7F, 0x80, 0xFF, 0x100, 0xFFFF, 0x10000, 0xFFFFFFFF,
        0x100000000, 0xFFFFFFFFFFFFFFFF,
        -1, -0x20, -0x21, -0x80, -0x81, -0x8000, -0x8001,
        -0x80000000, -0x80000001, -0x8000000000000000,
        # floats: subnormals, infinities, NaN, the representable extremes
        0.0, -0.0, 1.0, -1.5, math.pi, 1e308, -1e308, 1e-308,
        5e-324, -5e-324, math.inf, -math.inf, math.nan,
        # strings: fixstr/str8/str16 transitions counted in BYTES, not chars
        "", "ascii", "héllo", "日本語", "🎄wreath", "\x00\x01\x1f",
        "a" * 31 + "é", "é" * 128, "\U0010FFFF",
        # bytes and containers at their own transitions
        b"", b"\xab" * 255, b"\xab" * 256,
        [], [0] * 15, [0] * 16,
        {}, {"a": 1}, {str(i): i for i in range(16)},
        # keys that are not strings, all of which must survive a round trip
        {1: "a"}, {1.5: "x"}, {b"k": "v"}, {None: "v"}, {True: "v"},
        (1, 2, 3), bytearray(b"\x00\xff"), memoryview(b"\x00\xff"),
    ]
    if not wide:
        return cases

    for length in (65535, 65536):
        cases.append("x" * length)
        cases.append(b"\xab" * length)
    cases.append([0] * 65536)
    cases.append({str(i): i for i in range(65535)})

    for _ in range(400):
        bits = rng.choice([7, 8, 15, 16, 31, 32, 63, 64])
        n = rng.getrandbits(bits)
        cases.append(n)
        if n <= 0x8000000000000000:
            cases.append(-n)
    for _ in range(400):
        cases.append(struct.unpack(">d", rng.getrandbits(64).to_bytes(8, "big"))[0])
    for _ in range(200):
        cases.append("".join(rng.choice("aé日🎄\x00z") for _ in range(rng.randint(0, 300))))
    for _ in range(150):
        cases.append(bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 300))))

    def document(depth: int) -> object:
        if depth <= 0:
            return rng.choice(
                [None, True, False, rng.randint(-1000, 1000), rng.random(), "s" * 5]
            )
        kind = rng.choice(["list", "dict", "scalar"])
        if kind == "list":
            return [document(depth - 1) for _ in range(rng.randint(0, 6))]
        if kind == "dict":
            return {f"k{i}": document(depth - 1) for i in range(rng.randint(0, 6))}
        return document(0)

    cases.extend(document(4) for _ in range(600))
    return cases


def _sweep_msgpack(cases: list[object]) -> list[str]:
    failures: list[str] = []
    for value in cases:
        try:
            blob = packb(value)
        except Exception as exc:  # noqa: BLE001 - the failure is the finding
            failures.append(f"encode raised for {value!r:.60}: {exc}")
            continue
        try:
            back = unpackb(blob)
        except MsgpackDecodeError as exc:
            failures.append(f"undecodable output for {value!r:.60}: {exc}")
            continue
        if not _same(value, back):
            failures.append(f"round trip changed {value!r:.60} into {back!r:.60}")
    return failures


def test_msgpack_round_trips_through_an_independent_decoder() -> None:
    """The representative sample, in the default (fast) run."""
    rng = random.Random(SEED)
    cases = _msgpack_domain(rng, wide=False)
    failures = _sweep_msgpack(cases)
    assert not failures, f"{len(failures)}/{len(cases)} failed:\n" + "\n".join(
        failures[:10]
    )


@pytest.mark.fuzz
def test_msgpack_round_trip_sweep() -> None:
    """~2200 samples across every width transition, both signs, and the edges."""
    rng = random.Random(SEED)
    cases = _msgpack_domain(rng, wide=True)
    assert len(cases) > 2000, "the wide domain should be substantially wider"
    failures = _sweep_msgpack(cases)
    assert not failures, f"{len(failures)}/{len(cases)} failed:\n" + "\n".join(
        failures[:10]
    )


def test_a_container_key_is_refused_by_both_serializers() -> None:
    """The two serializers agree about what is representable.

    This sweep originally found the one divergence in 2,216 samples: `packb`
    accepted a tuple key and encoded it as a msgpack array, which is *valid*
    msgpack but which no decoder targeting a mapping type can reconstruct --
    Python, Go and JS all need a hashable or primitive key. `json.dumps` refused
    the same value outright, so the same handler return value was a `TypeError`
    on one content type and silently unreadable bytes on the other.

    Both encoders now refuse it, in the same words and with the same exception
    type as `json.dumps`, so a handler returning an unrepresentable value fails
    the same way whichever content type was negotiated. Only `tuple` can reach
    the key position at all -- list and dict are unhashable -- but the encoders
    test an allowlist of scalars rather than denying containers, so a hashable
    container added later is refused by default.
    """
    import json

    expected = "keys must be str, int, float, bool"
    with pytest.raises(TypeError, match=expected):
        packb({(1, 2): "x"})
    with pytest.raises(TypeError, match=expected):
        json.dumps({(1, 2): "x"})

    # Every scalar key the encoder accepts still survives the round trip.
    for key in (1, 1.5, b"k", None, True, "s"):
        assert unpackb(packb({key: "v"})) == {key: "v"}


# --------------------------------------------------------------------------
# The WFR1 container: identity, and three refusal properties
# --------------------------------------------------------------------------


def _image(n: int, rng: random.Random) -> MetadataImage:
    def named(count: int) -> tuple[NamedMeta, ...]:
        return tuple(
            NamedMeta(entry_id=i + 1, name=f"n{i}_" + "x" * rng.randint(0, 12))
            for i in range(count)
        )

    return MetadataImage(
        version=1,
        routes=(),
        plans=(),
        dependencies=named(n % 3),
        middleware=named((n + 1) % 4),
        auth_policies=(),
        serializers=named(n % 2),
        validators=(),
        limits=(),
        clients=named(n % 5),
        databases=(),
        models=named((n * 2) % 3),
    )


def _cells(count: int, rng: random.Random) -> tuple[bytes, ...]:
    return tuple(
        bytes([SCHEMA_VERSION]) + bytes(rng.getrandbits(8) for _ in range(CELL_SIZE - 1))
        for _ in range(count)
    )


def _corpus(rng: random.Random, count: int) -> list[tuple[MetadataImage, tuple[bytes, ...]]]:
    out = []
    for n in range(count):
        out.append((_image(n, rng), ()))
        out.append((_image(n, rng), _cells(rng.randint(1, 6), rng)))
    return out


def test_a_recording_round_trips_to_itself() -> None:
    rng = random.Random(SEED)
    for image, events in _corpus(rng, 8):
        decoded = decode_recording(encode_recording(image, events))
        assert decoded.image == image
        assert decoded.events == events


def test_trailing_bytes_after_the_last_chunk_are_refused() -> None:
    """Two recordings concatenated must not decode as the first one.

    This is the defect the sweep found: every trailing suffix was accepted, and
    the extra bytes were silently discarded. It is the same failure
    `replay.recorded_request` had with pipelined requests -- a buffer holding
    more than one thing, and only the first read, with no signal.
    """
    rng = random.Random(SEED)
    image, events = _corpus(rng, 1)[1]
    blob = encode_recording(image, events)

    for extra in (b"\x00", b"junk", bytes(64), blob):
        with pytest.raises(SchemaError, match="trailing"):
            decode_recording(blob + extra)


@pytest.mark.fuzz
def test_recording_truncation_is_refused_at_every_offset() -> None:
    rng = random.Random(SEED)
    checked = 0
    for image, events in _corpus(rng, 6):
        blob = encode_recording(image, events)
        for cut in range(len(blob)):
            checked += 1
            with pytest.raises(SchemaError):
                decode_recording(blob[:cut])
    assert checked > 1000, "the sweep should cover every offset of every blob"


@pytest.mark.fuzz
def test_recording_bit_corruption_is_refused_at_every_position() -> None:
    """A flipped bit must be rejected, never decoded into a different recording."""
    rng = random.Random(SEED)
    checked = 0
    for image, events in _corpus(rng, 4):
        blob = encode_recording(image, events)
        original = decode_recording(blob)
        for position in range(len(blob)):
            for bit in (0, 3, 7):
                checked += 1
                mutated = bytearray(blob)
                mutated[position] ^= 1 << bit
                try:
                    got = decode_recording(bytes(mutated))
                except SchemaError:
                    continue
                assert got.image == original.image and got.events == original.events, (
                    f"bit {bit} at byte {position} decoded to a different recording"
                )
    assert checked > 1000, "the sweep should cover every bit of every blob"
