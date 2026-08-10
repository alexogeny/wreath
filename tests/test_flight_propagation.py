"""Stage 2 (part): strict W3C traceparent parsing + correlation cells.

The parser must reject every malformed value without raising or reflecting the
input, agree byte-for-byte with `wreath._flight_reference.parse_traceparent`
(including under fuzzing), and a propagated request must emit a paired
correlation cell carrying the incoming trace.
"""

from __future__ import annotations

import random

import pytest

from wreath import _flight_schema as fs
from wreath._flight_reference import parse_traceparent as pure_parse

_flight = pytest.importorskip("wreath._native._flight")

_VALID = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_valid_traceparent_parses() -> None:
    result = _flight.parse_traceparent(_VALID)
    assert result is not None
    hi, lo, parent, sampled = result
    assert hi == 0x4BF92F3577B34DA6
    assert lo == 0xA3CE929D0E0E4736
    assert parent == 0x00F067AA0BA902B7
    assert sampled is True
    assert result == pure_parse(_VALID)


@pytest.mark.parametrize(
    "bad",
    [
        b"",  # empty
        b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0",  # short
        b"ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # forbidden ver
        b"00-00000000000000000000000000000000-00f067aa0ba902b7-01",  # zero trace
        b"00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",  # zero parent
        b"00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",  # uppercase
        b"00_4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # bad sep
        b"00-4bf92f3577b34da6a3ce929d0e0e473g-00f067aa0ba902b7-01",  # non-hex
        b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",  # long
    ],
)
def test_malformed_traceparent_is_rejected(bad: bytes) -> None:
    assert _flight.parse_traceparent(bad) is None
    assert pure_parse(bad) is None


def test_parser_matches_pure_under_fuzzing() -> None:
    rng = random.Random(2024)
    alphabet = b"0123456789abcdefABCDEF-_ "
    for _ in range(5000):
        n = rng.randrange(0, 60)
        candidate = bytes(rng.choice(alphabet) for _ in range(n))
        assert _flight.parse_traceparent(candidate) == pure_parse(candidate), candidate
    # Also fuzz around the valid template by mutating single bytes.
    for _ in range(2000):
        buf = bytearray(_VALID)
        pos = rng.randrange(len(buf))
        buf[pos] = rng.randrange(256)
        candidate = bytes(buf)
        assert _flight.parse_traceparent(candidate) == pure_parse(candidate), candidate


def test_propagated_request_emits_correlation_cell() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    req = rec.begin(connection_id=1, protocol=_flight.PROTO_HTTP1, start_ns=0)
    req.propagate(_VALID)
    req.finish(now_ns=1000, status=200)
    blob = rec.drain()
    assert len(blob) == 2 * fs.CELL_SIZE  # completion + correlation
    completion = fs.CompletionCell.decode(blob[: fs.CELL_SIZE])
    assert completion.flags & fs.FLAG_HAS_CORRELATION
    assert completion.flags & fs.FLAG_PROPAGATION_VALID
    assert completion.flags & fs.FLAG_SAMPLED
    correlation = fs.CorrelationCell.decode(blob[fs.CELL_SIZE :])
    assert correlation.trace_id == (0x4BF92F3577B34DA6 << 64) | 0xA3CE929D0E0E4736
    assert correlation.parent_span_id == 0x00F067AA0BA902B7
    assert correlation.span_id != 0  # a fresh child span was generated
    assert correlation.request_id == completion.request_id


def test_invalid_propagation_is_counted_and_uncorrelated() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    req = rec.begin(start_ns=0)
    req.propagate(b"not-a-traceparent")
    req.finish(now_ns=1000, status=200)
    assert rec.loss(fs.LossReason.PROPAGATION_INVALID) == 1
    blob = rec.drain()
    assert len(blob) == fs.CELL_SIZE  # completion only, no correlation
    completion = fs.CompletionCell.decode(blob)
    assert not (completion.flags & fs.FLAG_HAS_CORRELATION)


def test_unpropagated_request_has_no_correlation() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=8)
    rec.record(start_ns=0, end_ns=1000, status=200)
    assert len(rec.drain()) == fs.CELL_SIZE  # Pulse stays one cell without propagation
