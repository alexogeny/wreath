from __future__ import annotations

from wreath._fuzz import FuzzTarget
from wreath._http_replay import decode_exchange, encode_exchange
from wreath._replay_errors import HttpReplayError

from ._corpus import load_versioned


def run(data: bytes) -> tuple[str, ...]:
    try:
        exchange = decode_exchange(data)
    except HttpReplayError:
        return ("http-replay:refused",)
    canonical = encode_exchange(exchange)
    reparsed = decode_exchange(canonical)
    if encode_exchange(reparsed) != canonical:
        raise AssertionError("HTTP replay exchange canonicalization is not idempotent")
    return (
        "http-replay:decoded",
        f"http-replay:status-class:{exchange.response_status // 100}",
        "http-replay:redacted" if exchange.headers_redacted else "http-replay:complete",
    )


TARGET = FuzzTarget(
    "http-replay-codec",
    run,
    seeds=load_versioned("http-replay"),
    dictionary=(
        b"WHX1",
        b"WHX2",
        b"GET",
        b"POST",
        b"HTTP/1.1",
        b"content-type",
    ),
    source_files=(
        "src/wreath/_http_replay.py",
        "src/wreath/_native/http_replay.c",
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
