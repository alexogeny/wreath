"""Versioned outbound-HTTP exchange records for forensic replay.

This codec runs only for an armed forensic request.  It deliberately records one
self-contained exchange after the response has arrived: concurrent outbound calls
may interleave their ordinary request/response body captures, while a single field
cannot.  The dependency redaction policy still owns whether these bytes are retained;
replay refuses hashes, masks, lengths, and truncated records rather than inventing a
response from incomplete evidence.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ._native import _core
from ._replay_errors import HttpReplayError
from .recording import NEVER_CAPTURE_HEADERS

_MAGIC_V1 = b"WHX1"
_MAGIC = b"WHX2"
_HEAD_V1 = struct.Struct("<4s8H2I")
_HEAD = struct.Struct("<4sQ8H2I")
_HEADER = struct.Struct("<II")
_HEADERS_REDACTED = 1
_KNOWN_FLAGS = _HEADERS_REDACTED
_NEVER_CAPTURE_HEADER_BYTES = frozenset(name.encode("ascii") for name in NEVER_CAPTURE_HEADERS)


@dataclass(frozen=True, slots=True)
class RecordedHttpExchange:
    """The caller-visible request and complete response at one HTTP boundary."""

    dependency_id: int
    method: str
    target: str
    request_headers: tuple[tuple[bytes, bytes], ...]
    request_body: bytes
    idempotency_key: str | None
    response_status: int
    response_headers: tuple[tuple[bytes, bytes], ...]
    response_body: bytes
    http_version: str
    reason: bytes = b""
    headers_redacted: bool = False
    sequence: int = 0


def _encode_exchange_reference(exchange: RecordedHttpExchange) -> bytes:
    """Encode one exchange without JSON/base64 expansion.

    Forbidden header classes are removed here, at the codec boundary, so no
    caller can accidentally place credentials in a recording. The wire flag
    makes that loss explicit and prevents an incomplete exchange being replayed
    as though it were exact evidence.
    """
    method = exchange.method.upper().encode("ascii")
    target = exchange.target.encode("ascii")
    version = exchange.http_version.encode("ascii")
    idempotency = (
        b"" if exchange.idempotency_key is None else exchange.idempotency_key.encode("utf-8")
    )
    reason = bytes(exchange.reason)
    request_headers, request_redacted = _safe_headers(exchange.request_headers)
    response_headers, response_redacted = _safe_headers(exchange.response_headers)
    flags = (
        _HEADERS_REDACTED
        if (exchange.headers_redacted or request_redacted or response_redacted)
        else 0
    )
    short = {
        "dependency id": exchange.dependency_id,
        "response status": exchange.response_status,
        "method": len(method),
        "target": len(target),
        "HTTP version": len(version),
        "reason": len(reason),
        "idempotency key": len(idempotency),
        "header count": len(request_headers) + len(response_headers),
    }
    for name, value in short.items():
        if not 0 <= value <= 0xFFFF:
            raise HttpReplayError(f"outbound exchange {name} exceeds 65535")
    if len(exchange.request_body) > 0xFFFFFFFF or len(exchange.response_body) > 0xFFFFFFFF:
        raise HttpReplayError("outbound exchange body exceeds the 4 GiB wire limit")
    if not 0 <= exchange.sequence <= 0xFFFFFFFFFFFFFFFF:
        raise HttpReplayError("outbound exchange sequence exceeds uint64")
    out = bytearray(
        _HEAD.pack(
            _MAGIC,
            exchange.sequence,
            exchange.dependency_id,
            exchange.response_status,
            len(method),
            len(target),
            len(version),
            len(reason),
            len(idempotency),
            len(request_headers),
            len(exchange.request_body),
            len(exchange.response_body),
        )
    )
    out.extend(method)
    out.extend(target)
    out.extend(version)
    out.extend(reason)
    out.extend(idempotency)
    out.extend(_encode_headers(request_headers))
    out.extend(_HEADER.pack(len(response_headers), flags))
    out.extend(_encode_headers(response_headers))
    out.extend(exchange.request_body)
    out.extend(exchange.response_body)
    return bytes(out)


def _safe_headers(
    headers: tuple[tuple[bytes, bytes], ...],
) -> tuple[tuple[tuple[bytes, bytes], ...], bool]:
    """Return replay-safe headers and whether a forbidden value was omitted."""
    safe = tuple(
        (bytes(name), bytes(value))
        for name, value in headers
        if bytes(name).lower() not in _NEVER_CAPTURE_HEADER_BYTES
    )
    return safe, len(safe) != len(headers)


def _encode_headers(headers: tuple[tuple[bytes, bytes], ...]) -> bytes:
    out = bytearray()
    for name, value in headers:
        raw_name = bytes(name)
        raw_value = bytes(value)
        if len(raw_name) > 0xFFFFFFFF or len(raw_value) > 0xFFFFFFFF:
            raise HttpReplayError("outbound exchange header exceeds the 4 GiB wire limit")
        out.extend(_HEADER.pack(len(raw_name), len(raw_value)))
        out.extend(raw_name)
        out.extend(raw_value)
    return bytes(out)


def _decode_exchange_reference(data: bytes) -> RecordedHttpExchange:
    """Decode exactly one exchange, refusing every ambiguous/truncated form."""
    if len(data) < 4:
        raise HttpReplayError("outbound exchange is shorter than its header")
    magic = data[:4]
    if magic == _MAGIC:
        if len(data) < _HEAD.size:
            raise HttpReplayError("outbound exchange is shorter than its header")
        (
            _magic,
            sequence,
            dependency_id,
            status,
            method_len,
            target_len,
            version_len,
            reason_len,
            idempotency_len,
            request_header_count,
            request_body_len,
            response_body_len,
        ) = _HEAD.unpack_from(data)
        offset = _HEAD.size
    elif magic == _MAGIC_V1:
        if len(data) < _HEAD_V1.size:
            raise HttpReplayError("outbound exchange is shorter than its header")
        (
            _magic,
            dependency_id,
            status,
            method_len,
            target_len,
            version_len,
            reason_len,
            idempotency_len,
            request_header_count,
            request_body_len,
            response_body_len,
        ) = _HEAD_V1.unpack_from(data)
        sequence = 0
        offset = _HEAD_V1.size
    else:
        raise HttpReplayError(f"bad outbound exchange magic {magic!r}; expected WHX1 or WHX2")

    def take(length: int, label: str) -> bytes:
        nonlocal offset
        end = offset + length
        if end > len(data):
            raise HttpReplayError(f"outbound exchange {label} is truncated")
        value = data[offset:end]
        offset = end
        return value

    try:
        method = take(method_len, "method").decode("ascii")
        target = take(target_len, "target").decode("ascii")
        version = take(version_len, "HTTP version").decode("ascii")
        reason = take(reason_len, "reason")
        idempotency_data = take(idempotency_len, "idempotency key")
        idempotency = idempotency_data.decode("utf-8") if idempotency_data else None
    except UnicodeDecodeError as exc:
        raise HttpReplayError("outbound exchange text has the wrong encoding") from exc
    request_headers, offset = _decode_headers(data, offset, request_header_count)
    if offset + _HEADER.size > len(data):
        raise HttpReplayError("outbound exchange response-header count is truncated")
    response_header_count, flags = _HEADER.unpack_from(data, offset)
    offset += _HEADER.size
    unknown_flags = flags & ~_KNOWN_FLAGS
    if unknown_flags:
        raise HttpReplayError(f"outbound exchange has unknown flags 0x{unknown_flags:x}")
    response_headers, offset = _decode_headers(data, offset, response_header_count)
    request_body = data[offset : offset + request_body_len]
    offset += request_body_len
    response_body = data[offset : offset + response_body_len]
    offset += response_body_len
    if len(request_body) != request_body_len or len(response_body) != response_body_len:
        raise HttpReplayError("outbound exchange body is truncated")
    if offset != len(data):
        raise HttpReplayError("outbound exchange has trailing bytes")
    return RecordedHttpExchange(
        dependency_id=dependency_id,
        method=method,
        target=target,
        request_headers=request_headers,
        request_body=request_body,
        idempotency_key=idempotency,
        response_status=status,
        response_headers=response_headers,
        response_body=response_body,
        http_version=version,
        reason=reason,
        headers_redacted=bool(flags & _HEADERS_REDACTED),
        sequence=sequence,
    )


def _decode_headers(
    data: bytes, offset: int, count: int
) -> tuple[tuple[tuple[bytes, bytes], ...], int]:
    headers: list[tuple[bytes, bytes]] = []
    for _ in range(count):
        if offset + _HEADER.size > len(data):
            raise HttpReplayError("outbound exchange header length is truncated")
        name_len, value_len = _HEADER.unpack_from(data, offset)
        offset += _HEADER.size
        end_name = offset + name_len
        end_value = end_name + value_len
        if end_value > len(data):
            raise HttpReplayError("outbound exchange header is truncated")
        headers.append((data[offset:end_name], data[end_name:end_value]))
        offset = end_value
    return tuple(headers), offset


def encode_exchange(exchange: RecordedHttpExchange) -> bytes:
    """Encode one complete exchange in the native wire kernel."""
    return _core.http_exchange_encode(exchange, _NEVER_CAPTURE_HEADER_BYTES, HttpReplayError)


def decode_exchange(data: bytes) -> RecordedHttpExchange:
    """Decode one complete exchange at its public dataclass boundary."""
    return _core.http_exchange_decode(bytes(data), RecordedHttpExchange, HttpReplayError)
