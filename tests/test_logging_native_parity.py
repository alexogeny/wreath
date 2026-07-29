"""The native log emitter against its pure twin, byte for byte.

`wreath_nfr_log` packs a record straight into a ring cell in C. `pack_value`
plus `LogCell.encode` do the same work in Python. ADR 0005 makes the pure half
the oracle rather than a fallback, so the contract is not "both produce
something readable" -- it is that the 64 bytes are *identical*, for every shape
either can be handed.

The corpus below is deliberately weighted towards the cases where two
independent implementations drift apart: a bool that is also an int, an int one
past the wire slot, a string exactly on the clip boundary, a multi-byte
character straddling it, lone surrogates, bytes that are not valid UTF-8, more
arguments than a cell holds, and fewer arguments than the site declares. Each
was a place a hand-written C packer could plausibly disagree, and the point of
the test is that none of them does.
"""

from __future__ import annotations

import pytest

from wreath import logging as log
from wreath._flight_schema import (
    LOG_ARG_INT_MAX,
    LOG_ARG_INT_MIN,
    LOG_MAX_ARGS,
    CaptureDisposition,
    LogCell,
    Severity,
)
from wreath._logsite import LogField, SiteRegistry, pack_value, spec_blob

_flight = pytest.importorskip("wreath._native._flight", exc_type=ImportError)


def _pure_cell(
    registry: SiteRegistry,
    site_id: int,
    severity: Severity,
    request_id: int,
    fields: tuple[LogField, ...],
    values: tuple[object, ...],
    flags: int = 0,
    dropped: int = 0,
) -> bytes:
    """What the Python packer produces: the oracle these bytes are compared to."""
    packed = []
    for index, spec in enumerate(fields):
        value = values[index] if index < len(values) else None
        arg, _mismatched = pack_value(registry, value, spec)
        if arg.redacted:
            flags |= 0x04  # LOG_FLAG_REDACTED
        packed.append(arg)
    return LogCell(
        request_id=request_id,
        site_id=site_id,
        severity=severity,
        args=tuple(packed),
        flags=flags,
        dropped_siblings=dropped,
    ).encode()


def _native_cell(
    registry: SiteRegistry,
    site_id: int,
    severity: Severity,
    request_id: int,
    fields: tuple[LogField, ...],
    values: tuple[object, ...],
    flags: int = 0,
    dropped: int = 0,
) -> tuple[bytes, int]:
    """What the C emitter puts on the ring, read straight back out of it."""
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    key = registry.key
    outcome = recorder.log(
        site_id,
        int(severity),
        request_id,
        flags,
        dropped,
        spec_blob(fields),
        values,
        key[0],
        key[1],
    )
    assert outcome & 1, "the ring refused the record; the arm is measuring a drop"
    drained = recorder.drain(1)
    assert len(drained) == 64
    return bytes(drained), outcome >> 1


def _field(type_: type, disposition: CaptureDisposition) -> LogField:
    return LogField("value", type_, disposition)


RAW = CaptureDisposition.RAW
HASHED = CaptureDisposition.HASHED
MASKED = CaptureDisposition.MASKED
LENGTH = CaptureDisposition.LENGTH

#: (label, fields, values). One entry per way the two packers could disagree.
CORPUS: tuple[tuple[str, tuple[LogField, ...], tuple[object, ...]], ...] = (
    ("no arguments", (), ()),
    ("int", (_field(int, RAW),), (17,)),
    ("int zero", (_field(int, RAW),), (0,)),
    ("int negative", (_field(int, RAW),), (-1,)),
    ("int at the wire floor", (_field(int, RAW),), (LOG_ARG_INT_MIN,)),
    ("int at the wire ceiling", (_field(int, RAW),), (LOG_ARG_INT_MAX,)),
    ("int one past the ceiling", (_field(int, RAW),), (LOG_ARG_INT_MAX + 1,)),
    ("int one past the floor", (_field(int, RAW),), (LOG_ARG_INT_MIN - 1,)),
    ("int enormous", (_field(int, RAW),), (2**200,)),
    # bool is an int subclass; each must refuse the other.
    ("bool true", (_field(bool, RAW),), (True,)),
    ("bool false", (_field(bool, RAW),), (False,)),
    ("bool declared, int given", (_field(bool, RAW),), (1,)),
    ("int declared, bool given", (_field(int, RAW),), (True,)),
    ("float declared, bool given", (_field(float, RAW),), (True,)),
    ("float", (_field(float, RAW),), (1.5,)),
    ("float from an int", (_field(float, RAW),), (3,)),
    ("float negative zero", (_field(float, RAW),), (-0.0,)),
    ("float infinity", (_field(float, RAW),), (float("inf"),)),
    ("float nan", (_field(float, RAW),), (float("nan"),)),
    ("float from an int too wide for a double", (_field(float, RAW),), (2**2000,)),
    ("str", (_field(str, RAW),), ("orders",)),
    ("str empty", (_field(str, RAW),), ("",)),
    ("str declared, int given", (_field(str, RAW),), (7,)),
    ("str at the inline boundary", (_field(str, RAW),), ("x" * 30,)),
    ("str one past the boundary", (_field(str, RAW),), ("x" * 31,)),
    ("str far past the boundary", (_field(str, RAW),), ("x" * 400,)),
    # A multi-byte character straddling the clip: the cut must back off to a
    # UTF-8 boundary or the record raises when it is read.
    ("str clipped mid-character", (_field(str, RAW),), ("x" * 29 + "é",)),
    ("str all multi-byte", (_field(str, RAW),), ("é" * 40,)),
    ("str four-byte characters", (_field(str, RAW),), ("\U0001f332" * 20,)),
    ("str lone surrogate", (_field(str, RAW),), ("\ud800",)),
    ("bytes", (_field(bytes, RAW),), (b"orders",)),
    ("bytes invalid utf-8", (_field(bytes, RAW),), (b"\xff\xfe ok",)),
    ("bytes declared, str given", (_field(bytes, RAW),), ("orders",)),
    ("none declared, none given", (_field(type(None), RAW),), (None,)),
    ("none declared, int given", (_field(type(None), RAW),), (7,)),
    ("none declared, str given", (_field(type(None), RAW),), ("x",)),
    ("int declared, none given", (_field(int, RAW),), (None,)),
    ("str declared, none given", (_field(str, RAW),), (None,)),
    ("hashed str", (_field(str, HASHED),), ("s3cret",)),
    ("hashed empty str", (_field(str, HASHED),), ("",)),
    ("hashed bytes", (_field(bytes, HASHED),), (b"s3cret",)),
    ("hashed int", (_field(int, HASHED),), (12345,)),
    ("hashed none", (_field(str, HASHED),), (None,)),
    ("hashed long value", (_field(str, HASHED),), ("s3cret" * 100,)),
    ("masked", (_field(str, MASKED),), ("s3cret",)),
    ("length", (_field(str, LENGTH),), ("s3cret",)),
    ("length of a multi-byte value", (_field(str, LENGTH),), ("é" * 10,)),
    (
        "two arguments",
        (LogField("user", int, RAW), LogField("resource", str, RAW)),
        (17, "orders"),
    ),
    (
        "three that fill the cell",
        (
            LogField("a", int, RAW),
            LogField("b", int, RAW),
            LogField("c", int, RAW),
        ),
        (1, 2, 3),
    ),
    (
        "four that do not fit",
        tuple(LogField(f"f{i}", int, RAW) for i in range(4)),
        (1, 2, 3, 4),
    ),
    (
        "more arguments than a cell holds",
        tuple(LogField(f"f{i}", int, RAW) for i in range(LOG_MAX_ARGS + 2)),
        tuple(range(LOG_MAX_ARGS + 2)),
    ),
    (
        "fewer values than fields",
        (LogField("user", int, RAW), LogField("resource", str, RAW)),
        (17,),
    ),
    (
        "a string that leaves no room for the next argument",
        (LogField("text", str, RAW), LogField("n", int, RAW)),
        ("x" * 30, 5),
    ),
    # The redaction flag is raised when an argument *is* redacted, not when it
    # survives packing, so a hashed argument that does not fit must still leave
    # the record marked. Both halves have to agree about that, and they can only
    # disagree here.
    (
        "a hashed argument with no room left",
        (LogField("text", str, RAW), LogField("secret", str, HASHED)),
        ("x" * 30, "s3cret"),
    ),
    (
        "a length argument with no room left",
        (LogField("text", str, RAW), LogField("secret", str, LENGTH)),
        ("x" * 30, "s3cret"),
    ),
)


@pytest.mark.parametrize("label,fields,values", CORPUS, ids=[row[0] for row in CORPUS])
def test_the_native_emitter_packs_what_the_pure_one_packs(
    label: str, fields: tuple[LogField, ...], values: tuple[object, ...]
) -> None:
    registry = SiteRegistry()
    pure = _pure_cell(registry, 7, Severity.WARN, 99, fields, values)
    native, _mismatches = _native_cell(registry, 7, Severity.WARN, 99, fields, values)
    assert native == pure, (
        f"{label}: the native emitter and the pure packer disagree.\n"
        f"  pure   {pure.hex(' ', 4)}\n  native {native.hex(' ', 4)}"
    )


@pytest.mark.parametrize("label,fields,values", CORPUS, ids=[row[0] for row in CORPUS])
def test_both_halves_decode_to_the_same_record(
    label: str, fields: tuple[LogField, ...], values: tuple[object, ...]
) -> None:
    """Identical bytes are necessary; decoding without raising is the point."""
    registry = SiteRegistry()
    native, _mismatches = _native_cell(registry, 7, Severity.WARN, 99, fields, values)
    decoded = LogCell.decode(native)
    assert decoded.site_id == 7
    assert decoded.request_id == 99
    assert decoded.severity == Severity.WARN


@pytest.mark.parametrize("label,fields,values", CORPUS, ids=[row[0] for row in CORPUS])
def test_both_halves_count_the_same_type_mismatches(
    label: str, fields: tuple[LogField, ...], values: tuple[object, ...]
) -> None:
    """A mismatch is counted on both paths, never raised on either.

    The count is what tells an operator a call site is lying about its types, so
    a native emitter that packed identical bytes while counting differently
    would be a silent regression in the only signal there is.
    """
    registry = SiteRegistry()
    pure = sum(
        pack_value(registry, values[i] if i < len(values) else None, spec)[1]
        for i, spec in enumerate(fields)
    )
    _native, mismatches = _native_cell(registry, 7, Severity.WARN, 99, fields, values)
    assert mismatches == pure, f"{label}: {mismatches} native vs {pure} pure"


def test_the_fingerprint_key_is_the_registrys_own() -> None:
    """Both halves must hash with one key, or correlation breaks in-process.

    A fingerprint exists so two occurrences of one value can be recognised as
    the same within a recording. If the native emitter hashed with the worker's
    key while the pure path used the registry's, records from the two would
    never match -- and nothing would raise to say so.
    """
    fields = (_field(str, HASHED),)
    first = SiteRegistry()
    second = SiteRegistry()

    same = _native_cell(first, 7, Severity.WARN, 99, fields, ("tenant",))[0]
    also_same = _pure_cell(first, 7, Severity.WARN, 99, fields, ("tenant",))
    assert same == also_same

    different = _native_cell(second, 7, Severity.WARN, 99, fields, ("tenant",))[0]
    assert different != same, (
        "two registries fingerprinted one value identically; the key is not "
        "process-local after all"
    )


def test_a_full_ring_is_a_counted_drop_not_an_error() -> None:
    """The same posture the recorder takes for a completion it cannot fit."""
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=2, active_requests=4)
    registry = SiteRegistry()
    key = registry.key
    blob = spec_blob((_field(int, RAW),))
    published = [
        recorder.log(1, int(Severity.INFO), 0, 0, 0, blob, (index,), key[0], key[1]) & 1
        for index in range(8)
    ]
    assert published[0] == 1
    assert 0 in published, "a ring of two accepted eight records"
    assert recorder.loss(_flight.LOSS_RING_FULL) > 0


def test_the_emitter_refuses_a_malformed_call_rather_than_packing_garbage() -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=8, active_requests=4)
    with pytest.raises(TypeError):
        recorder.log(1, 9, 0, 0, 0, "not bytes", (), 0, 0)
    with pytest.raises(TypeError):
        recorder.log(1, 9, 0, 0, 0, b"\x20", [1], 0, 0)
    with pytest.raises(TypeError):
        recorder.log(1, 9, 0, 0, 0, b"\x20")


def test_the_installed_runtime_uses_the_native_emitter_when_one_exists() -> None:
    """The wiring, not just the packer: a recorder-backed runtime must use it."""
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    runtime = log.LogRuntime(
        log.recorder_sink(recorder),
        level=log.INFO,
        native=log.recorder_emitter(recorder),
    )
    assert runtime.native is not None
    previous = log.install(runtime)
    try:
        site = log.event(
            "parity.wired",
            "user {user} denied {resource}",
            level=log.WARN,
            fields=(log.field("user", int), log.field("resource", str, log.RAW)),
        )
        site(17, "orders")
    finally:
        log.install(previous)
    cell = LogCell.decode(bytes(recorder.drain(1)))
    assert cell.site_id == site.site_id
    assert cell.severity == Severity.WARN


def test_a_template_whose_value_types_drift_still_packs_the_value_passed() -> None:
    """The kwargs tier interns on template *text*, which does not pin the types.

    `log.info("v is {v}", v=1)` and the same line with a string reach one
    interned site, whose declared fields are whichever call arrived first.
    Packing the second call against the first call's types would turn its value
    into a counted mismatch and lose it -- which the pure packer never did, so
    the native emitter must not either.
    """
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    runtime = log.LogRuntime(
        log.recorder_sink(recorder),
        level=log.INFO,
        native=log.recorder_emitter(recorder),
    )
    previous = log.install(runtime)
    try:
        log.info("drifting value is {v}", v=17)
        log.info("drifting value is {v}", v="seventeen")
    finally:
        log.install(previous)
    drained = bytes(recorder.drain(2))
    first = LogCell.decode(drained[:64])
    second = LogCell.decode(drained[64:128])
    assert first.args[0].number == 17
    # A string under a site that declared int: still the value that was passed,
    # fingerprinted because an undeclared string is deny-by-default.
    assert second.args[0].type is not first.args[0].type
    assert runtime.counters.type_mismatch == 0


def test_a_recorder_without_a_native_emitter_falls_back_to_the_pure_packer() -> None:
    class PureRecorder:
        def __init__(self) -> None:
            self.cells: list[bytes] = []

        def publish_log(self, cell: bytes, /) -> bool:
            self.cells.append(cell)
            return True

    recorder = PureRecorder()
    assert log.recorder_emitter(recorder) is None
    runtime = log.LogRuntime(
        log.recorder_sink(recorder), level=log.INFO, native=log.recorder_emitter(recorder)
    )
    previous = log.install(runtime)
    try:
        log.info("cache miss for {key}", key=42)
    finally:
        log.install(previous)
    assert len(recorder.cells) == 1
    assert LogCell.decode(recorder.cells[0]).severity == Severity.INFO
