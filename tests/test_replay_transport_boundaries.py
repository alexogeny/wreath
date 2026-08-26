from __future__ import annotations

from typing import Any

import pytest

import wreath.replay as replay
from wreath.replay import (
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    SegmentKind,
    TransportRecording,
    TransportReplayResult,
    TransportSegment,
    record_transport_segments,
)
from wreath.server import ServerConfig


def _result(*, normalized: bytes = b"same", terminal: str = "closed") -> TransportReplayResult:
    return TransportReplayResult(b"raw", normalized, terminal, 1, 1)


def test_transport_result_matches_both_normalized_bytes_and_terminal_state() -> None:
    result = _result()

    assert result.matches(_result())
    assert not result.matches(_result(normalized=b"different"))
    assert not result.matches(_result(terminal="open"))


class _CountedBuffer:
    def __init__(self, *, growing: bool) -> None:
        self.calls = 0
        self.growing = growing

    def __len__(self) -> int:
        self.calls += 1
        return self.calls if self.growing else 0


class _DrainTransport:
    closed = False

    def __init__(self, *, paused: bool, growing: bool) -> None:
        self._paused = paused
        self.buffer = _CountedBuffer(growing=growing)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paused", "growing", "expected_calls"),
    [(False, True, 5), (True, False, 5), (False, False, 3)],
)
async def test_drain_does_not_call_growth_or_backpressure_quiescent(
    monkeypatch: pytest.MonkeyPatch, paused: bool, growing: bool, expected_calls: int
) -> None:
    monkeypatch.setattr(replay, "_MAX_PUMPS", 5)
    monkeypatch.setattr(replay, "_QUIET_PLATEAU", 2)
    transport = _DrainTransport(paused=paused, growing=growing)

    await replay._drain(transport)  # type: ignore[arg-type]

    assert transport.buffer.calls == expected_calls


class _BoundaryProtocol:
    instances: list[_BoundaryProtocol] = []

    def __init__(
        self, app: Any, config: ServerConfig, loop: Any, registry: Any, **kwargs: Any
    ) -> None:
        self.config = config
        self.kwargs = kwargs
        self.transport: Any = None
        self.data: list[bytes] = []
        self.eof_calls = 0
        self.lost: list[Exception | None] = []
        type(self).instances.append(self)

    def connection_made(self, transport: Any) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.data.append(data)

    def eof_received(self) -> None:
        self.eof_calls += 1

    def connection_lost(self, error: Exception | None) -> None:
        self.lost.append(error)


class _SelfClosingProtocol(_BoundaryProtocol):
    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        self.transport.close()


@pytest.fixture(autouse=True)
def _clear_boundary_protocol() -> None:
    _BoundaryProtocol.instances.clear()


@pytest.mark.asyncio
async def test_replay_preserves_explicit_config_protocol_and_recorder() -> None:
    config = ServerConfig(server_header="chosen")
    recorder = object()

    await replay.replay_transport(
        None,
        TransportRecording(()),
        config=config,
        protocol_cls=_BoundaryProtocol,
        recorder=recorder,
    )

    protocol = _BoundaryProtocol.instances[-1]
    assert protocol.config is config
    assert protocol.kwargs == {"recorder": recorder}


@pytest.mark.asyncio
async def test_replay_omits_an_absent_recorder_and_supplies_default_config() -> None:
    await replay.replay_transport(None, TransportRecording(()), protocol_cls=_BoundaryProtocol)

    protocol = _BoundaryProtocol.instances[-1]
    assert isinstance(protocol.config, ServerConfig)
    assert protocol.kwargs == {}


@pytest.mark.asyncio
async def test_empty_fault_reads_are_not_delivered_or_counted() -> None:
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.SHORT_READ), 0, 0),))

    result = await replay.replay_transport(
        None,
        record_transport_segments([b"request"]),
        protocol_cls=_BoundaryProtocol,
        faults=schedule,
    )

    assert _BoundaryProtocol.instances[-1].data == []
    assert result.segments_fed == 0


@pytest.mark.asyncio
async def test_recorded_close_is_delivered_once() -> None:
    recording = TransportRecording((TransportSegment(0, int(SegmentKind.EOF), b""),))

    await replay.replay_transport(None, recording, protocol_cls=_BoundaryProtocol)

    protocol = _BoundaryProtocol.instances[-1]
    assert protocol.eof_calls == 1
    assert protocol.lost == [None]


@pytest.mark.asyncio
async def test_open_recording_receives_final_eof() -> None:
    await replay.replay_transport(None, TransportRecording(()), protocol_cls=_BoundaryProtocol)

    protocol = _BoundaryProtocol.instances[-1]
    assert protocol.eof_calls == 1
    assert protocol.lost == [None]


@pytest.mark.asyncio
async def test_protocol_owned_close_does_not_receive_a_synthetic_eof() -> None:
    await replay.replay_transport(
        None,
        record_transport_segments([b"request"], close=None),
        protocol_cls=_SelfClosingProtocol,
    )

    protocol = _SelfClosingProtocol.instances[-1]
    assert protocol.eof_calls == 0
    assert protocol.lost == []


@pytest.mark.asyncio
async def test_h2_replay_supplies_h2_default_and_preserves_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ServerConfig | None] = []

    async def drive(
        app: Any,
        recording: TransportRecording,
        protocol_cls: type,
        config: ServerConfig | None,
        faults: FaultSchedule | None,
        recorder: object | None = None,
    ) -> tuple[bytes, str, int, int]:
        captured.append(config)
        return b"", "closed", 0, 0

    monkeypatch.setattr(replay, "_drive_connection", drive)
    config = ServerConfig(protocols=("h2",), server_header="chosen")

    await replay.replay_transport_h2(None, TransportRecording(()))
    await replay.replay_transport_h2(None, TransportRecording(()), config=config)

    assert captured[0] is not None
    assert captured[0].protocols == ("h2",)
    assert captured[1] is config


def test_timeout_hook_fires_only_when_callable() -> None:
    called: list[bool] = []

    class CallableHook:
        def _replay_fire_timeout(self) -> None:
            called.append(True)

    class DataHook:
        _replay_fire_timeout = "not callable"

    replay._fire_timeout(CallableHook())
    replay._fire_timeout(DataHook())
    replay._fire_timeout(object())

    assert called == [True]
