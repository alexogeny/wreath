from __future__ import annotations

from wreath import _multipart
from wreath._fuzz import FuzzTarget

from ._corpus import load_versioned

_BOUNDARY = b"wreath-fuzz"


def run(data: bytes) -> tuple[str, ...]:
    try:
        parts = _multipart.parse(
            data,
            _BOUNDARY,
            max_parts=64,
            max_part_header_bytes=8_192,
            max_part_bytes=65_536,
        )
    except ValueError as refusal:
        return (f"multipart:refused:{type(refusal).__name__}",)
    for part in parts:
        if any(name.lower() != name for name, _ in part.headers):
            raise AssertionError("multipart parser returned a non-lowercase header name")
    return (
        f"multipart:parts:{len(parts)}",
        "multipart:file"
        if any(part.filename is not None for part in parts)
        else "multipart:no-file",
        "multipart:named" if any(part.name is not None for part in parts) else "multipart:unnamed",
    )


TARGET = FuzzTarget(
    "multipart-parser",
    run,
    seeds=load_versioned("multipart"),
    dictionary=(
        b"--wreath-fuzz\r\n",
        b"--wreath-fuzz--\r\n",
        b"Content-Disposition: form-data; name=\"field\"\r\n",
        b"; filename=\"file.txt\"",
        b"Content-Type: text/plain\r\n",
        b"\r\n",
    ),
    source_files=(
        "src/wreath/_multipart.py",
        "src/wreath/_native/multipart.c",
    ),
    operator_names=(
        "guard.always-fires",
        "guard.never-fires",
        "guard.remove-raise",
        "predicate.always-true",
        "predicate.drop-operand",
        "value.widen-bound",
    ),
)
