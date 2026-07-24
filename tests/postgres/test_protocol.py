from __future__ import annotations

import asyncio
import importlib
import struct
from collections import deque
from typing import Any

import pytest

from wreath._pure import postgres as pure_postgres

native_postgres: Any = None
try:
    native_postgres = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

POSTGRES_BACKENDS = [pure_postgres]
if native_postgres is not None:
    POSTGRES_BACKENDS.append(native_postgres)


@pytest.fixture(params=POSTGRES_BACKENDS, ids=lambda backend: backend._implementation)
def postgres(request: pytest.FixtureRequest) -> Any:
    return request.param


def _frontend_types(packet: bytes) -> list[bytes]:
    types: list[bytes] = []
    offset = 0
    while offset < len(packet):
        message_type = packet[offset : offset + 1]
        length = struct.unpack_from("!I", packet, offset + 1)[0]
        types.append(message_type)
        offset += 1 + length
    assert offset == len(packet)
    return types


def _backend_message(message_type: bytes, payload: bytes) -> bytes:
    return message_type + struct.pack("!I", len(payload) + 4) + payload


def _bind_result_format_count(packet: bytes) -> int:
    assert packet[:1] == b"B"
    payload = memoryview(packet)[5 : 1 + struct.unpack_from("!I", packet, 1)[0]]
    offset = bytes(payload).index(b"\x00") + 1
    offset += bytes(payload[offset:]).index(b"\x00") + 1
    parameter_format_count = struct.unpack_from("!H", payload, offset)[0]
    offset += 2 + 2 * parameter_format_count
    parameter_count = struct.unpack_from("!H", payload, offset)[0]
    offset += 2
    for _ in range(parameter_count):
        length = struct.unpack_from("!i", payload, offset)[0]
        offset += 4 + max(length, 0)
    return struct.unpack_from("!H", payload, offset)[0]


def test_cold_query_packet_is_one_extended_protocol_flight(postgres: Any) -> None:
    packet = postgres._build_cold_query_packet(
        b"wreath_1", "select $1::int4", (42,), (23,), "fetch"
    )
    assert _frontend_types(packet) == [b"P", b"D", b"B", b"D", b"E", b"S"]


def test_cached_query_omits_parse_and_describe(postgres: Any) -> None:
    plan = postgres.Plan(b"wreath_1", (23,), (23,), ("value",))
    packet = postgres._build_cached_query_packet(plan, (42,), "fetch")
    assert _frontend_types(packet) == [b"B", b"E", b"S"]
    assert _bind_result_format_count(packet) == 1


def test_execute_packets_omit_unused_result_metadata(postgres: Any) -> None:
    cold = postgres._build_cold_query_packet(
        b"wreath_1", "update things set value = $1", (42,), (23,), "execute"
    )
    plan = postgres.Plan(b"wreath_1", (23,), (), ())
    cached = postgres._build_cached_query_packet(plan, (42,), "execute")

    assert _frontend_types(cold) == [b"P", b"D", b"B", b"E", b"S"]
    assert _frontend_types(cached) == [b"B", b"E", b"S"]
    bind_offset = 1 + struct.unpack_from("!I", cold, 1)[0]
    describe_length = 1 + struct.unpack_from("!I", cold, bind_offset + 1)[0]
    bind_offset += describe_length
    assert _bind_result_format_count(cold[bind_offset:]) == 0
    assert _bind_result_format_count(cached) == 0


def test_query_packet_rejects_unknown_result_mode(postgres: Any) -> None:
    plan = postgres.Plan(b"wreath_1", (23,), (), ())
    with pytest.raises(ValueError, match="result mode"):
        postgres._build_cached_query_packet(plan, (42,), "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", [b"R", b"T", b"D", b"E", b"Z"])
async def test_backend_message_can_split_at_every_byte_boundary(
    postgres: Any, message_type: bytes
) -> None:
    payload = b"fragmented payload"
    encoded = _backend_message(message_type, payload)

    for split in range(len(encoded) + 1):
        reader = asyncio.StreamReader()
        task = asyncio.create_task(postgres._read_message(reader))
        reader.feed_data(encoded[:split])
        await asyncio.sleep(0)
        reader.feed_data(encoded[split:])
        assert await task == (message_type, payload)


@pytest.mark.asyncio
async def test_backend_message_can_arrive_one_byte_at_a_time(postgres: Any) -> None:
    payload = b"one byte chunks"
    encoded = _backend_message(b"D", payload)
    reader = asyncio.StreamReader()
    task = asyncio.create_task(postgres._read_message(reader))
    for byte in encoded:
        reader.feed_data(bytes((byte,)))
        await asyncio.sleep(0)
    assert await task == (b"D", payload)


@pytest.mark.asyncio
async def test_invalid_backend_message_length_is_rejected(postgres: Any) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"D\x00\x00\x00\x03")
    with pytest.raises(postgres.ProtocolError, match="length"):
        await postgres._read_message(reader)


def test_scram_sha256_proof_and_server_signature_are_verified(postgres: Any) -> None:
    state, first = postgres._scram_start("user", "fixed-client-nonce")
    assert first == "n,,n=user,r=fixed-client-nonce"
    final, expected_signature = postgres._scram_continue(
        state,
        "pencil",
        "r=fixed-client-nonce-server,s=QSXCR+Q6sek8bf92,i=4096",
    )
    assert final.startswith("c=biws,r=fixed-client-nonce-server,p=")
    server_final = "v=" + expected_signature
    postgres._scram_finish(expected_signature, server_final)
    with pytest.raises(postgres.OperationalError, match="signature"):
        postgres._scram_finish(expected_signature, "v=invalid")


def test_scram_rejects_nonce_downgrade_and_invalid_iterations(postgres: Any) -> None:
    state, _ = postgres._scram_start("user", "client")
    with pytest.raises(postgres.OperationalError):
        postgres._scram_continue(state, "password", "r=other,s=QSXCR+Q6sek8bf92,i=4096")
    with pytest.raises(postgres.OperationalError):
        postgres._scram_continue(state, "password", "r=client-server,s=QSXCR+Q6sek8bf92,i=0")


# --- asynchronous backend messages -------------------------------------------


def _operation(mode: str = "execute") -> Any:
    loop = asyncio.new_event_loop()
    try:
        return pure_postgres.Operation(
            sequence=1,
            sql="SET default_transaction_read_only = on",
            args=(),
            mode=mode,  # ty: ignore[invalid-argument-type]
            future=loop.create_future(),
            deadline=None,
        )
    finally:
        loop.close()


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        # ParameterStatus: the server reports any GUC_REPORT setting that
        # changes mid-session. `SET default_transaction_read_only = on`, which
        # Database._open runs for every read pool, is one of these from
        # PostgreSQL 14 on -- so this arrives during a normal operation, not
        # just at startup.
        (b"S", b"default_transaction_read_only\x00on\x00"),
        (b"N", b"\x00"),  # NoticeResponse
        (b"A", b"\x00\x00\x00\x01chan\x00\x00"),  # NotificationResponse
    ],
)
def test_asynchronous_messages_are_ignored_during_an_operation(
    kind: bytes, payload: bytes
) -> None:
    """These may arrive at any time and belong to no operation.

    tests/postgres/ otherwise drives a FakeConnection that never emits them, so
    this is the only place that pins the behaviour without a real server.
    """
    connection = pure_postgres.Connection.__new__(pure_postgres.Connection)
    # A NotificationResponse ('A') is now captured into the per-connection ring
    # rather than discarded, so give the bare connection the notify slots that
    # __init__ would set. It still must not become an error on the operation.
    connection._notifications = deque()
    connection._notifications_dropped = 0
    connection._notify_event = asyncio.Event()
    operation = _operation()
    connection._consume_message(operation, kind, payload)
    assert operation.error is None


def test_an_unknown_backend_message_is_still_a_protocol_error() -> None:
    connection = pure_postgres.Connection.__new__(pure_postgres.Connection)
    with pytest.raises(pure_postgres.ProtocolError, match="unexpected backend message"):
        connection._consume_message(_operation(), b"\x7f", b"")
