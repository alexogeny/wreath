"""Stage 5 slice 5a — native forensic capture-slab core.

Drives the native ``_flight.Recorder`` capture path directly and checks it
byte-for-byte against the pure oracle in ``wreath._pure.flight``. The whole
point of this slice is the deny-by-default, bounded, redact-before-retention
slab mechanism, so the tests lean on secret canaries, slab exhaustion, and
per-field truncation as hard as on the differential parity. The extension is
optional; tests skip cleanly if it was not built.
"""

from __future__ import annotations

import random

import pytest

from wreath import _flight_schema as fs
from wreath._flight_schema import CaptureDisposition as D
from wreath._flight_schema import CaptureFieldClass as FC
from wreath._pure import flight as codec
from wreath._pure.flight import PureRecorder, siphash24

_flight = pytest.importorskip("wreath._native._flight")

# A fixed key so HASHED capture is reproducible and native/pure slabs compare
# byte-for-byte. Production draws a random per-worker key.
KEY = (0xDEADBEEFCAFEF00D, 0x0123456789ABCDEF)


def _native(**kw: object) -> object:
    kw.setdefault("ring_records", 1024)
    kw.setdefault("active_requests", 64)
    kw.setdefault("detailed_sample_rate", 1.0)
    kw.setdefault("capture_hash_key", KEY)
    return _flight.Recorder(_flight.MODE_FORENSIC, **kw)


def _pure(**kw: object) -> PureRecorder:
    kw.setdefault("ring_records", 1024)
    kw.setdefault("active_requests", 64)
    kw.setdefault("detailed_sample_rate", 1.0)
    kw.setdefault("capture_hash_key", KEY)
    return PureRecorder(fs.Mode.FORENSIC, **kw)


# --- SipHash vector ---------------------------------------------------------


def test_siphash_matches_reference_vector() -> None:
    # Canonical SipHash-2-4 test vector (key 00..0f, input bytes(0..14)).
    k0, k1 = 0x0706050403020100, 0x0F0E0D0C0B0A0908
    assert siphash24(bytes(range(15)), k0, k1) == 0xA129CA6149BE45E5


# --- deny by default --------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [_flight.MODE_OFF, _flight.MODE_PULSE, _flight.MODE_DETAILED],
)
def test_non_forensic_modes_capture_nothing(mode: int) -> None:
    rec = _flight.Recorder(
        mode, ring_records=256, active_requests=16, capture_slabs=8,
        slab_bytes=4096, detailed_sample_rate=1.0, capture_hash_key=KEY,
    )
    req = rec.begin(protocol=_flight.PROTO_HTTP1, start_ns=0)
    req.capture(int(FC.REQUEST_BODY), 0, _flight.CAP_RAW, b"secret-payload")
    req.finish(now_ns=1000, status=200)
    assert rec.drain_captures() == []  # nothing captured, no slab consumed
    assert rec.capture_committed == 0
    assert rec.capture_in_use == 0
    assert rec.loss(fs.LossReason.CAPTURE_POOL_FULL) == 0


def test_forensic_but_unarmed_captures_nothing() -> None:
    # rate 0 arms no request, so capture is a no-op even in Forensic mode.
    rec = _native(detailed_sample_rate=0.0, capture_slabs=8, slab_bytes=4096)
    req = rec.begin(protocol=_flight.PROTO_HTTP1, start_ns=0)
    req.capture(int(FC.REQUEST_BODY), 0, _flight.CAP_RAW, b"secret")
    assert req.capture_slot == -1
    req.finish(now_ns=1000, status=200)
    assert rec.drain_captures() == []


# --- differential parity ----------------------------------------------------


def _drive(rec: object, *, raw: int, hashed: int, masked: int, length: int) -> None:
    scenarios = [
        [(FC.REQUEST_HEADER, 3, raw, b"Mozilla/5.0 wreath-test")],
        [
            (FC.REQUEST_BODY, 0, hashed, b"password=hunter2&token=abc"),
            (FC.RESPONSE_HEADER, 4, length, b"x" * 40),
        ],
        [
            (FC.DB_PARAM, 9, masked, b"3141592653"),
            (FC.DB_ROW, 9, hashed, b"row-bytes-here"),
            (FC.OUTBOUND_REQUEST, 2, raw, b"GET /users"),
        ],
        [],  # armed but captures nothing -> no slab committed
    ]
    for i, fields in enumerate(scenarios):
        req = rec.begin(connection_id=100 + i, protocol=1, start_ns=i * 1000)
        req.route(10, 20)
        for field_class, desc, disposition, data in fields:
            req.capture(int(field_class), desc, int(disposition), data)
        req.finish(now_ns=i * 1000 + 500, status=200, bytes_in=8, bytes_out=16)


def test_native_and_pure_slabs_are_byte_identical() -> None:
    n = _native(capture_slabs=8, slab_bytes=4096)
    p = _pure(capture_slabs=8, slab_bytes=4096)
    _drive(n, raw=_flight.CAP_RAW, hashed=_flight.CAP_HASHED,
           masked=_flight.CAP_MASKED, length=_flight.CAP_LENGTH)
    _drive(p, raw=int(D.RAW), hashed=int(D.HASHED), masked=int(D.MASKED),
           length=int(D.LENGTH))
    assert n.drain() == p.drain()  # completion cells identical too
    assert n.drain_captures() == p.drain_captures()
    assert n.capture_in_use == p.capture_in_use
    assert n.capture_high_water == p.capture_high_water
    for reason in fs.LossReason:
        assert n.loss(int(reason)) == p.loss(int(reason)), reason.name


def test_random_differential_parity() -> None:
    rng = random.Random(0xF11675)
    n = _native(capture_slabs=6, slab_bytes=512)
    p = _pure(capture_slabs=6, slab_bytes=512)
    classes = list(FC)
    for step in range(200):
        cid = rng.randint(0, 1_000_000)
        rn = n.begin(connection_id=cid, protocol=1, start_ns=step * 100)
        rp = p.begin(connection_id=cid, protocol=1, start_ns=step * 100)
        for _ in range(rng.randint(0, 5)):
            fc = int(rng.choice(classes))
            desc = rng.randint(0, 50)
            disp = rng.randint(0, 3)
            data = bytes(rng.randbytes(rng.randint(0, 300)))
            rn.capture(fc, desc, disp, data)
            rp.capture(fc, desc, disp, data)
        # Occasionally drain the sink so slabs recycle through the return path.
        if step % 17 == 0:
            assert n.drain_captures() == p.drain_captures()
        if rng.random() < 0.1:
            rn.abandon()
            rp.abandon()
        else:
            rn.finish(now_ns=step * 100 + 50, status=200)
            rp.finish(now_ns=step * 100 + 50, status=200)
    assert n.drain_captures() == p.drain_captures()
    assert n.drain() == p.drain()
    assert n.capture_in_use == p.capture_in_use
    for reason in fs.LossReason:
        assert n.loss(int(reason)) == p.loss(int(reason)), reason.name


# --- redaction canaries -----------------------------------------------------


def test_hashed_disposition_never_stores_plaintext() -> None:
    rec = _native(capture_slabs=4, slab_bytes=4096)
    secret = b"super-secret-bearer-token-value"
    req = rec.begin(start_ns=0)
    req.capture(int(FC.REQUEST_HEADER), 1, _flight.CAP_HASHED, secret)
    req.finish(now_ns=1000, status=200)
    (slab,) = rec.drain_captures()
    assert secret not in slab
    decoded = fs.CaptureSlab.decode(slab)
    field = decoded.fields[0]
    assert field.disposition is fs.CaptureDisposition.HASHED
    assert field.original_length == len(secret)
    assert len(field.payload) == fs.CAPTURE_HASH_BYTES
    assert field.payload == siphash24(secret, *KEY).to_bytes(8, "little")


def test_masked_and_length_store_only_length() -> None:
    rec = _native(capture_slabs=4, slab_bytes=4096)
    secret = b"1234-5678-9012-3456"
    req = rec.begin(start_ns=0)
    req.capture(int(FC.DB_PARAM), 2, _flight.CAP_MASKED, secret)
    req.capture(int(FC.DB_ROW), 3, _flight.CAP_LENGTH, secret)
    req.finish(now_ns=1000, status=200)
    (slab,) = rec.drain_captures()
    assert secret not in slab
    decoded = fs.CaptureSlab.decode(slab)
    for field in decoded.fields:
        assert field.payload == b""
        assert field.original_length == len(secret)


# --- bounds and truncation --------------------------------------------------


def test_raw_body_truncates_to_slab_and_flags() -> None:
    # slab_bytes is small; a large RAW body cannot fit and is clipped.
    rec = _native(capture_slabs=2, slab_bytes=128)
    pure = _pure(capture_slabs=2, slab_bytes=128)
    big = bytes(range(256)) * 4  # 1 KiB, far over the 128-byte slab
    for r in (rec, pure):
        req = r.begin(start_ns=0)
        req.capture(int(FC.REQUEST_BODY), 0, int(D.RAW)
                    if r is pure else _flight.CAP_RAW, big)
        req.finish(now_ns=1000, status=200)
    (nslab,) = rec.drain_captures()
    (pslab,) = pure.drain_captures()
    assert nslab == pslab
    decoded = fs.CaptureSlab.decode(nslab)
    field = decoded.fields[0]
    assert field.truncated
    assert field.original_length == len(big)
    assert len(field.payload) < len(big)
    assert decoded.flags & fs.FLAG_BODY_TRUNCATED
    assert rec.loss(fs.LossReason.BODY_TRUNCATED) == 1


def test_raw_policy_max_bytes_cap_records_true_length() -> None:
    # A max_bytes cap keeps a prefix but records the true original length as
    # truncated (distinct from the slab-overflow bound); native and pure agree.
    rec = _native(capture_slabs=2, slab_bytes=4096)
    pure = _pure(capture_slabs=2, slab_bytes=4096)
    big = b"abcdefghijklmnopqrstuvwxyz"  # 26 bytes, capped to 8
    for r in (rec, pure):
        req = r.begin(start_ns=0)
        req.capture(int(FC.REQUEST_BODY), 0, int(D.RAW), big, max_bytes=8)
        req.finish(now_ns=1000, status=200)
    (nslab,) = rec.drain_captures()
    (pslab,) = pure.drain_captures()
    assert nslab == pslab
    field = fs.CaptureSlab.decode(nslab).fields[0]
    assert field.payload == b"abcdefgh"
    assert field.original_length == 26
    assert field.truncated
    assert rec.loss(fs.LossReason.BODY_TRUNCATED) == 1


def test_slab_pool_exhaustion_counts_loss() -> None:
    # Two slabs, three concurrent armed requests that each capture: the third
    # finds no slab and counts CAPTURE_POOL_FULL.
    rec = _native(capture_slabs=2, slab_bytes=4096)
    pure = _pure(capture_slabs=2, slab_bytes=4096)
    for r, cap in ((rec, _flight.CAP_RAW), (pure, int(D.RAW))):
        reqs = [r.begin(connection_id=i, start_ns=i) for i in range(3)]
        for req in reqs:
            req.capture(int(FC.REQUEST_BODY), 0, cap, b"body-bytes")
        for req in reqs:
            req.finish(now_ns=10_000, status=200)
    assert rec.loss(fs.LossReason.CAPTURE_POOL_FULL) == 1
    assert pure.loss(fs.LossReason.CAPTURE_POOL_FULL) == 1
    assert rec.drain_captures() == pure.drain_captures()


def test_drained_slabs_recycle_through_the_pool() -> None:
    # A tiny pool sustains many sequential requests because the sink returns
    # each slab; without recycling the third request would find none.
    rec = _native(capture_slabs=1, slab_bytes=4096)
    for i in range(5):
        req = rec.begin(connection_id=i, start_ns=i)
        req.capture(int(FC.REQUEST_BODY), 0, _flight.CAP_RAW, b"payload-%d" % i)
        req.finish(now_ns=i + 1, status=200)
        (slab,) = rec.drain_captures()  # frees the slab back to the pool
        assert fs.CaptureSlab.decode(slab).request_id == i + 1
    assert rec.loss(fs.LossReason.CAPTURE_POOL_FULL) == 0


# --- lifecycle edges --------------------------------------------------------


def test_abandon_releases_slab_without_committing() -> None:
    rec = _native(capture_slabs=2, slab_bytes=4096)
    req = rec.begin(start_ns=0)
    req.capture(int(FC.REQUEST_BODY), 0, _flight.CAP_RAW, b"partial")
    assert rec.capture_in_use == 1
    req.abandon()
    assert rec.drain_captures() == []  # nothing committed
    # The slab returned to the free pool immediately (no sink round trip).
    assert rec.capture_in_use == 0


def test_completion_without_summaries_drops_slab() -> None:
    rec = _native(capture_slabs=2, slab_bytes=4096, completion_summaries=False)
    pure = _pure(capture_slabs=2, slab_bytes=4096, completion_summaries=False)
    for r, cap in ((rec, _flight.CAP_RAW), (pure, int(D.RAW))):
        req = r.begin(start_ns=0)
        req.capture(int(FC.REQUEST_BODY), 0, cap, b"payload")
        req.finish(now_ns=1000, status=200)
    assert rec.drain_captures() == []
    assert pure.drain_captures() == []
    assert rec.capture_in_use == 0 and pure.capture_in_use == 0


def test_dropped_request_handle_abandons_capture() -> None:
    rec = _native(capture_slabs=2, slab_bytes=4096)
    req = rec.begin(start_ns=0)
    req.capture(int(FC.REQUEST_BODY), 0, _flight.CAP_RAW, b"payload")
    assert rec.capture_in_use == 1
    del req  # dealloc without finishing models cancellation/teardown
    assert rec.capture_in_use == 0
    assert rec.drain_captures() == []


def test_slab_decode_rejects_short_and_wrong_kind() -> None:
    with pytest.raises(fs.SchemaError):
        fs.CaptureSlab.decode(b"\x00" * 8)
    # A phase batch cell is a valid 64-byte cell but the wrong kind.
    phase = fs.PhaseBatchCell(
        request_id=1, records=(fs.PhaseRecord(fs.PhaseKind.AUTH, duration_us=5),)
    ).encode()
    with pytest.raises(fs.SchemaError):
        fs.CaptureSlab.decode(phase)


def test_capture_slab_bytes_and_capacity_reported() -> None:
    rec = _native(capture_slabs=8, slab_bytes=2048)
    assert rec.capture_capacity == 8
    assert rec.capture_slab_bytes == 2048
    assert codec  # keep the codec import referenced for future WFR1 wiring
