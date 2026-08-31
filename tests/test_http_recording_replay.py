from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from wreath._flight_markers import (
    CAP_OUTBOUND_HTTP_EXCHANGE,
    capture_marker,
    phase_marker,
)
from wreath._flight_schema import (
    CaptureDisposition,
    CaptureField,
    CaptureFieldClass,
    CaptureSlab,
    NamedMeta,
)
from wreath._http_replay import (
    HttpReplayError,
    RecordedHttpExchange,
    _decode_exchange_reference,
    _encode_exchange_reference,
    decode_exchange,
    encode_exchange,
)
from wreath._replay_adapters import ReplayAdapters
from wreath.http_client import ClientResponse, HTTPClient


def test_h2_response_decoder_reassembles_many_data_frames_linearly() -> None:
    from wreath._h2_codec import DATA, FLAG_END_STREAM, decode_response

    payloads = tuple(bytes((index & 0xFF,)) * 257 for index in range(512))

    def frame(payload: bytes, flags: int) -> bytes:
        return (
            len(payload).to_bytes(3, "big")
            + bytes((DATA, flags))
            + (1).to_bytes(4, "big")
            + payload
        )

    frames = b"".join(
        frame(payload, FLAG_END_STREAM if index + 1 == len(payloads) else 0)
        for index, payload in enumerate(payloads)
    )
    stream = decode_response(frames)[1]
    assert type(stream.body) is bytes
    assert stream.body == b"".join(payloads)
    assert stream.ended


def test_h2_response_decoder_allocates_one_body_buffer_per_stream(monkeypatch) -> None:
    import wreath._h2_codec as codec

    allocations = 0
    builtin_bytearray = bytearray

    def counted_bytearray(value=b""):
        nonlocal allocations
        allocations += 1
        return builtin_bytearray(value)

    monkeypatch.setattr(codec, "bytearray", counted_bytearray, raising=False)
    frame = b"\x00\x00\x01" + bytes((codec.DATA, 0)) + b"\x00\x00\x00\x01x"
    assert codec.decode_response(frame * 3)[1].body == b"xxx"
    assert allocations == 1


def exchange() -> RecordedHttpExchange:
    return RecordedHttpExchange(
        dependency_id=3,
        method="POST",
        target="/charge",
        request_headers=((b"content-type", b"application/json"),),
        request_body=b'{"amount": 9}',
        idempotency_key="charge-1",
        response_status=202,
        response_headers=((b"content-type", b"application/json"), (b"x-id", b"7")),
        response_body=b'{"queued": true}',
        http_version="1.1",
        reason=b"Accepted",
    )


def test_outbound_exchange_codec_is_an_exact_inverse() -> None:
    recorded = exchange()
    assert decode_exchange(encode_exchange(recorded)) == recorded
    with pytest.raises(HttpReplayError, match="truncated"):
        decode_exchange(encode_exchange(recorded)[:-1])
    with pytest.raises(HttpReplayError, match="sequence exceeds uint64"):
        encode_exchange(replace(recorded, sequence=-1))


def test_native_outbound_exchange_codec_matches_the_independent_definition() -> None:
    recorded = replace(
        exchange(),
        request_headers=exchange().request_headers
        + (
            (b"Authorization", b"secret"),
            (b"x-long", b"v" * 257),
        ),
        response_headers=exchange().response_headers + ((b"Set-Cookie", b"secret"),),
        request_body=b"request" * 113,
        response_body=b"response" * 127,
        sequence=0xFEDCBA9876543210,
    )
    expected = _encode_exchange_reference(recorded)
    assert encode_exchange(recorded) == expected
    assert decode_exchange(expected) == _decode_exchange_reference(expected)

    for end in range(len(expected)):
        truncated = expected[:end]
        with pytest.raises(HttpReplayError) as reference_error:
            _decode_exchange_reference(truncated)
        with pytest.raises(HttpReplayError) as native_error:
            decode_exchange(truncated)
        assert str(native_error.value) == str(reference_error.value)


def test_outbound_exchange_decoder_preserves_whx1_recordings() -> None:
    recorded = exchange()
    whx2 = encode_exchange(replace(recorded, sequence=17))
    whx1 = b"WHX1" + whx2[12:]
    assert decode_exchange(whx1) == recorded
    with pytest.raises(HttpReplayError, match="shorter than its header"):
        decode_exchange(whx1[:10])


def test_outbound_exchange_omits_forbidden_headers_and_marks_the_loss() -> None:
    recorded = replace(
        exchange(),
        request_headers=(
            (b"content-type", b"application/json"),
            (b"Authorization", b"Bearer request-secret"),
        ),
        response_headers=(
            (b"content-type", b"application/json"),
            (b"Set-Cookie", b"session=response-secret"),
        ),
    )
    payload = encode_exchange(recorded)
    assert b"request-secret" not in payload
    assert b"response-secret" not in payload
    decoded = decode_exchange(payload)
    assert decoded.headers_redacted is True
    assert decoded.request_headers == ((b"content-type", b"application/json"),)
    assert decoded.response_headers == ((b"content-type", b"application/json"),)


def test_http_replay_refuses_an_exchange_with_omitted_forbidden_headers() -> None:
    payload = encode_exchange(
        replace(
            exchange(),
            request_headers=((b"authorization", b"Bearer secret"),),
        )
    )
    recording = SimpleNamespace(
        slabs=(
            CaptureSlab(
                42,
                (
                    CaptureField(
                        CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
                        0,
                        CaptureDisposition.RAW,
                        len(payload),
                        payload,
                    ),
                ),
            ),
        ),
        image=SimpleNamespace(clients=(NamedMeta(3, "billing"),)),
    )
    with pytest.raises(HttpReplayError, match="omitted forbidden headers"):
        ReplayAdapters.from_recording(recording)


@pytest.mark.asyncio
async def test_wfr1_exchange_builds_a_validating_http_double() -> None:
    payload = encode_exchange(exchange())
    field = CaptureField(
        CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
        0,
        CaptureDisposition.RAW,
        len(payload),
        payload,
    )
    recording = SimpleNamespace(
        slabs=(CaptureSlab(42, (field,)),),
        image=SimpleNamespace(clients=(NamedMeta(3, "billing"),)),
    )
    adapters = ReplayAdapters.from_recording(recording)
    client = adapters.clients["billing"]
    response = await client.request(
        "post",
        "/charge",
        headers=((b"content-type", b"application/json"),),
        body=b'{"amount": 9}',
        idempotency_key="charge-1",
    )
    assert (response.status, response.body, response.reason) == (
        202,
        b'{"queued": true}',
        b"Accepted",
    )


@pytest.mark.asyncio
async def test_wfr1_http_replay_refuses_request_drift() -> None:
    payload = encode_exchange(exchange())
    recording = SimpleNamespace(
        slabs=(
            CaptureSlab(
                42,
                (
                    CaptureField(
                        CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
                        0,
                        CaptureDisposition.RAW,
                        len(payload),
                        payload,
                    ),
                ),
            ),
        ),
        image=SimpleNamespace(clients=(NamedMeta(3, "billing"),)),
    )
    client = ReplayAdapters.from_recording(recording).clients["billing"]
    with pytest.raises(HttpReplayError, match="body differs"):
        await client.request(
            "POST",
            "/charge",
            headers=((b"content-type", b"application/json"),),
            body=b'{"amount": 10}',
            idempotency_key="charge-1",
        )


def test_http_replay_selects_only_the_named_request_id() -> None:
    first = encode_exchange(exchange())
    second = encode_exchange(replace(exchange(), target="/other", sequence=1))
    recording = SimpleNamespace(
        slabs=(
            CaptureSlab(
                41,
                (
                    CaptureField(
                        CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
                        0,
                        CaptureDisposition.RAW,
                        len(first),
                        first,
                    ),
                ),
            ),
            CaptureSlab(
                42,
                (
                    CaptureField(
                        CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
                        0,
                        CaptureDisposition.RAW,
                        len(second),
                        second,
                    ),
                ),
            ),
        ),
        image=SimpleNamespace(clients=(NamedMeta(3, "billing"),)),
    )

    adapters = ReplayAdapters.from_recording(recording, request_id=42)

    assert [item.target for item in adapters.clients["billing"]._replay_exchanges] == ["/other"]


def test_http_replay_refuses_ambiguous_request_ids() -> None:
    payload = encode_exchange(exchange())
    field = CaptureField(
        CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
        0,
        CaptureDisposition.RAW,
        len(payload),
        payload,
    )
    recording = SimpleNamespace(
        slabs=(CaptureSlab(41, (field,)), CaptureSlab(42, (field,))),
        image=SimpleNamespace(clients=(NamedMeta(3, "billing"),)),
    )

    with pytest.raises(HttpReplayError, match="multiple request ids"):
        ReplayAdapters.from_recording(recording)


def test_http_replay_ignores_other_capture_field_classes() -> None:
    payload = encode_exchange(exchange())
    recording = SimpleNamespace(
        slabs=(
            CaptureSlab(
                42,
                (
                    CaptureField(
                        CaptureFieldClass.REQUEST_BODY,
                        0,
                        CaptureDisposition.RAW,
                        7,
                        b"not WHX",
                    ),
                    CaptureField(
                        CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
                        0,
                        CaptureDisposition.RAW,
                        len(payload),
                        payload,
                    ),
                ),
            ),
        ),
        image=SimpleNamespace(clients=(NamedMeta(3, "billing"),)),
    )

    adapters = ReplayAdapters.from_recording(recording)

    assert len(adapters.clients["billing"]._replay_exchanges) == 1


def test_http_replay_refuses_redacted_truncated_and_unknown_dependency_captures() -> None:
    payload = encode_exchange(exchange())
    base = CaptureField(
        CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
        0,
        CaptureDisposition.RAW,
        len(payload),
        payload,
    )
    cases = [
        (
            replace(base, disposition=CaptureDisposition.HASHED),
            "redacted",
            (NamedMeta(3, "billing"),),
        ),
        (
            replace(base, original_length=len(payload) + 1),
            "truncated",
            (NamedMeta(3, "billing"),),
        ),
        (base, "unknown client id", ()),
    ]

    for field, message, clients in cases:
        recording = SimpleNamespace(
            slabs=(CaptureSlab(42, (field,)),),
            image=SimpleNamespace(clients=clients),
        )
        with pytest.raises(HttpReplayError, match=message):
            ReplayAdapters.from_recording(recording)


@pytest.mark.asyncio
async def test_concurrent_http_exchanges_replay_in_invocation_order() -> None:
    release_first = asyncio.Event()

    class InvertedClient(HTTPClient):
        async def _request_timed(self, method, target, *, headers, body, idempotency_key):
            if target == "/first":
                await release_first.wait()
            else:
                release_first.set()
            return ClientResponse(200, (), target.encode("ascii"), "1.1")

    captured: list[bytes] = []

    def capture(field_class: int, payload: bytes) -> None:
        if field_class == CAP_OUTBOUND_HTTP_EXCHANGE:
            captured.append(payload)

    client = InvertedClient("billing", base_url="https://billing.example")
    phase_token = phase_marker.set(lambda *_args: None)
    capture_token = capture_marker.set(capture)
    try:
        first = asyncio.create_task(client.request("GET", "/first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(client.request("GET", "/second"))
        await asyncio.gather(first, second)
    finally:
        capture_marker.reset(capture_token)
        phase_marker.reset(phase_token)

    completed = tuple(decode_exchange(payload) for payload in captured)
    assert [item.target for item in completed] == ["/second", "/first"]
    assert [item.sequence for item in completed] == [1, 0]

    fields = tuple(
        CaptureField(
            CaptureFieldClass.OUTBOUND_HTTP_EXCHANGE,
            0,
            CaptureDisposition.RAW,
            len(payload),
            payload,
        )
        for payload in captured
    )
    recording = SimpleNamespace(
        slabs=(CaptureSlab(42, fields),),
        image=SimpleNamespace(clients=(NamedMeta(0, "billing"),)),
    )
    replay = ReplayAdapters.from_recording(recording).clients["billing"]
    assert (await replay.request("GET", "/first")).body == b"/first"
    assert (await replay.request("GET", "/second")).body == b"/second"


@pytest.mark.asyncio
async def test_http_capture_refuses_a_sequence_that_exceeds_its_wire_field() -> None:
    client = HTTPClient("billing", base_url="https://billing.example")
    client._capture_sequence = 0x10000000000000000
    phase_token = phase_marker.set(lambda *_args: None)
    capture_token = capture_marker.set(lambda *_args: None)
    try:
        with pytest.raises(OverflowError, match="forensic sequence exceeds uint64"):
            await client.request("GET", "/")
    finally:
        capture_marker.reset(capture_token)
        phase_marker.reset(phase_token)
