from __future__ import annotations

from wreath._fuzz import FuzzTarget
from wreath._h2_codec import parse_frames

from ._corpus import load_versioned


def run(data: bytes) -> tuple[str, ...]:
    frames = parse_frames(data)
    offset = 0
    for frame in frames:
        length = len(frame.payload)
        header = data[offset : offset + 9]
        if (
            int.from_bytes(header[:3], "big") != length
            or header[3] != frame.type
            or header[4] != frame.flags
            or int.from_bytes(header[5:9], "big") & 0x7FFFFFFF != frame.stream_id
            or data[offset + 9 : offset + 9 + length] != frame.payload
        ):
            raise AssertionError("HTTP/2 frame decode disagreed with its source bytes")
        offset += 9 + length
    features = [f"h2:frames:{len(frames)}"]
    features.extend(f"h2:type:{frame.type}" for frame in frames)
    if offset < len(data):
        features.append("h2:trailing-partial")
    return tuple(dict.fromkeys(features))


TARGET = FuzzTarget(
    "h2-frames",
    run,
    seeds=load_versioned("h2"),
    dictionary=(
        b"\x00\x00\x00\x04\x00\x00\x00\x00\x00",
        b"\x00\x00\x00\x06\x01\x00\x00\x00\x00",
        b"\x00\x00\x00\x07\x00\x00\x00\x00\x00",
        b"\x00\x00\x00\x09\x04\x00\x00\x00\x01",
    ),
    source_files=(
        "src/wreath/_h2_codec.py",
        "src/wreath/_native/server_http2.c",
        "src/wreath/_native/server_hpack.c",
    ),
    operator_names=(
        "guard.always-fires",
        "guard.never-fires",
        "predicate.always-true",
        "predicate.drop-operand",
        "value.widen-bound",
    ),
)
