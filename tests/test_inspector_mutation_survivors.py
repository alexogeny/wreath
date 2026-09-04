from __future__ import annotations

import asyncio
import os
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from wreath._flight_schema import (
    FLAG_AI_SCRAPING_REFUSED,
    FLAG_POLICY_REFUSED,
    Protocol,
    TerminalStatus,
)
from wreath.inspector import (
    FLAG_TRUNCATED,
    MAX_PAGE_ROWS,
    Command,
    InspectorClient,
    InspectorConfig,
    InspectorError,
    InspectorServer,
    _capture_policy_from_payload,
    _InspectorProtocol,
    _peer_authorized,
    _trace_payload,
)


class Recorder:
    requests = 7

    def __init__(self, active: list[tuple[int, int, int, int]] | None = None) -> None:
        self.active = active or []

    def active_snapshot(self):
        return self.active


def _server(*, token: str | None = None, registry: Any = None) -> InspectorServer:
    return InspectorServer(
        Recorder(),
        object(),
        InspectorConfig("inspector.sock", capture_token=token),
        arm_registry=registry,
    )


def test_config_repr_does_not_expose_capture_token() -> None:
    token = "inspector-capture-secret"

    assert token not in repr(InspectorConfig("inspector.sock", capture_token=token))


def test_config_enforces_both_payload_bounds_and_capture_token_length() -> None:
    with pytest.raises(ValueError, match="max_payload_bytes"):
        InspectorConfig("x.sock", max_payload_bytes=MAX_PAGE_ROWS * 1024 + 1)
    with pytest.raises(ValueError, match="at least 16"):
        InspectorConfig("x.sock", capture_token="short")
    defaults = InspectorConfig("x.sock", capture_token=None)
    assert defaults.capture_token is None
    assert defaults.idle_timeout == 30.0


def test_capture_authorization_requires_both_registry_and_token() -> None:
    with pytest.raises(InspectorError, match="not enabled"):
        _server(token=None, registry=object())._authorize_capture({"token": "x" * 16})
    server = _server(token="x" * 16, registry=None)
    with pytest.raises(InspectorError, match="not enabled"):
        server._authorize_capture({"token": "x" * 16})
    missing_token = _server()
    missing_token._arm_registry = object()
    with pytest.raises(InspectorError, match="not enabled"):
        missing_token._authorize_capture({"token": "x" * 16})


def test_capture_command_disarms_only_with_a_non_boolean_integer() -> None:
    registry = SimpleNamespace(disarm=lambda arm_id: arm_id == 3)
    server = _server(token="x" * 16, registry=registry)
    for arm_id in (True, "3", None):
        with pytest.raises(InspectorError, match="integer arm_id"):
            server._capture_command(
                Command.DISARM_CAPTURE,
                {"token": "x" * 16, "arm_id": arm_id},
            )
    assert server._capture_command(
        Command.DISARM_CAPTURE,
        {"token": "x" * 16, "arm_id": 3},
    ) == {"disarmed": True}
    with pytest.raises(InspectorError, match="unknown capture command"):
        server._capture_command(999, {"token": "x" * 16})


def test_paged_traces_reports_both_truncated_states() -> None:
    server = _server()
    server._projector = object()
    snapshot = SimpleNamespace(
        assembled=3,
        loss=SimpleNamespace(
            orphan_phase=0,
            orphan_correlation=0,
            pending_evicted=0,
            decode_error=0,
            export_error=0,
            recent_evicted=0,
        ),
    )
    rows = [_trace(index) for index in range(3)]
    body, flags = server._paged_traces({"limit": 2}, rows, snapshot)
    assert body["truncated"] is True
    assert flags == FLAG_TRUNCATED
    body, flags = server._paged_traces({"limit": 3}, rows, snapshot)
    assert body["truncated"] is False
    assert flags == 0


def test_active_requests_reports_both_truncated_states(monkeypatch) -> None:
    recorder = Recorder([(1, 1, int(Protocol.HTTP1), 9), (2, 2, int(Protocol.HTTP2), 10)])
    server = InspectorServer(recorder, object(), InspectorConfig("x.sock"))
    monkeypatch.setattr("wreath.inspector.time.monotonic_ns", lambda: 5_000)
    body, flags = server._active_requests({"limit": 1})
    assert body["truncated"] is True
    assert flags == FLAG_TRUNCATED
    assert body["requests"][0]["age_us"] == 4
    body, flags = server._active_requests({"limit": 2})
    assert body["truncated"] is False
    assert flags == 0


def test_active_requests_default_page_is_bounded_to_256_rows(monkeypatch) -> None:
    active = [(index, 0, int(Protocol.HTTP1), 1) for index in range(257)]
    server = InspectorServer(Recorder(active), object(), InspectorConfig("x.sock"))
    monkeypatch.setattr("wreath.inspector.time.monotonic_ns", lambda: 0)
    body, flags = server._active_requests({})
    assert len(body["requests"]) == 256
    assert body["truncated"] is True
    assert flags == FLAG_TRUNCATED


def _metadata_server() -> InspectorServer:
    server = _server()
    image = SimpleNamespace(
        routes=(
            SimpleNamespace(route_id=1, method="GET", path="/a", operation_id="a", plan_id=7),
            SimpleNamespace(route_id=2, method="POST", path="/b", operation_id="b", plan_id=8),
        ),
        plans=(
            SimpleNamespace(plan_id=7, params=("id",)),
            SimpleNamespace(plan_id=8, params=()),
        ),
        dependencies=(SimpleNamespace(entry_id=1, name="dep"),),
    )
    server._image = image
    server._routes_by_id = {}
    server._routes_by_key = {}
    server._plans_by_id = {}
    server._metadata_names = {}
    return server


def test_explain_route_requires_both_method_and_path() -> None:
    server = _metadata_server()
    for payload in ({"method": "GET"}, {"path": "/a"}, {"method": 3, "path": "/a"}):
        with pytest.raises(InspectorError, match=r"method\+path"):
            server._explain_route(payload)


def test_explain_plan_refuses_an_absent_plan() -> None:
    server = _metadata_server()
    with pytest.raises(InspectorError, match="plan not found"):
        server._explain_plan({"plan_id": 99})


def test_metadata_distinguishes_plans_generic_rows_and_truncation() -> None:
    server = _metadata_server()
    plans, plan_flags = server._metadata({"table": "plans", "limit": 1})
    assert plans["rows"] == [{"id": 7, "params": ["id"]}]
    assert plan_flags == FLAG_TRUNCATED
    dependencies, dependency_flags = server._metadata({"table": "dependencies"})
    assert dependencies["rows"] == [{"id": 1, "name": "dep"}]
    assert dependency_flags == 0


def _trace(request_id: int, *, flags: int = 0, correlated: bool = False):
    return SimpleNamespace(
        request_id=request_id,
        connection_id=2,
        route_id=3,
        plan_id=4,
        worker_id=5,
        duration_us=6,
        status=200,
        terminal=TerminalStatus.OK,
        protocol=Protocol.HTTP1,
        error_class=0,
        flags=flags,
        bytes_in=7,
        bytes_out=8,
        is_failure=False,
        trace_id=9,
        span_id=10,
        has_correlation=correlated,
        observed_unix_nano=11,
        phases=(),
    )


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (0, None),
        (FLAG_POLICY_REFUSED, "refused"),
        (FLAG_POLICY_REFUSED | FLAG_AI_SCRAPING_REFUSED, "ai_scraping"),
    ],
)
def test_trace_payload_distinguishes_policy_dispositions(flags: int, expected: str | None) -> None:
    assert _trace_payload(_trace(1, flags=flags))["policy_disposition"] == expected


def test_trace_payload_distinguishes_absent_and_present_correlation_ids() -> None:
    absent = _trace_payload(_trace(1))
    assert absent["trace_id"] is None
    assert absent["span_id"] is None
    present = _trace_payload(_trace(1, correlated=True))
    assert present["trace_id"] == format(9, "032x")
    assert present["span_id"] == format(10, "016x")


def test_capture_policy_refuses_non_object_sections_and_normalizes_blank_expiry() -> None:
    with pytest.raises(InspectorError, match="redaction must be an object"):
        _capture_policy_from_payload({"redaction": []})
    with pytest.raises(InspectorError, match="budget must be an object"):
        _capture_policy_from_payload({"budget": []})
    assert _capture_policy_from_payload({"expiry_seconds": ""}).expiry_seconds == 0.0


class Transport(asyncio.Transport):
    def __init__(self, sock: Any = None) -> None:
        self.closed = False
        self.sock = sock

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self.sock if name == "socket" else default


@pytest.mark.asyncio
async def test_protocol_rejects_unauthorized_and_non_transport_peers(monkeypatch) -> None:
    protocol = _InspectorProtocol(_server())
    unauthorized = Transport()
    monkeypatch.setattr("wreath.inspector._peer_authorized", lambda transport: False)
    protocol.connection_made(unauthorized)
    assert unauthorized.closed is True
    assert protocol._transport is None

    class Base(asyncio.BaseTransport):
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    other = Base()
    monkeypatch.setattr("wreath.inspector._peer_authorized", lambda transport: True)
    protocol.connection_made(other)
    assert other.closed is True
    assert protocol._transport is None


def test_protocol_timer_replacement_and_idle_expiry(monkeypatch) -> None:
    cancelled: list[bool] = []
    scheduled: list[Any] = []
    previous = SimpleNamespace(cancel=lambda: cancelled.append(True))
    loop = SimpleNamespace(
        call_later=lambda delay, callback: scheduled.append((delay, callback)) or previous
    )
    protocol = _InspectorProtocol(_server())
    protocol._idle_handle = previous
    monkeypatch.setattr("wreath.inspector.asyncio.get_running_loop", lambda: loop)
    protocol._reset_idle_timer()
    assert cancelled == [True]
    assert scheduled[0][0] == 30.0

    protocol._idle_expired()
    transport = Transport()
    protocol._transport = transport
    protocol._idle_expired()
    assert transport.closed is True


def test_peer_authorization_requires_a_socket_and_matching_uid(monkeypatch) -> None:
    assert _peer_authorized(Transport()) is False

    class PeerSocket:
        def __init__(self, uid: int | None) -> None:
            self.uid = uid

        def getsockopt(self, *args: Any) -> bytes:
            if self.uid is None:
                raise OSError("closed")
            return struct.pack("3i", 1, self.uid, 2)

    assert _peer_authorized(Transport(PeerSocket(os.getuid()))) is True
    assert _peer_authorized(Transport(PeerSocket(0))) is True
    assert _peer_authorized(Transport(PeerSocket(os.getuid() + 1))) is False
    assert _peer_authorized(Transport(PeerSocket(None))) is False

    monkeypatch.setattr("wreath.inspector.socket", SimpleNamespace())
    assert _peer_authorized(Transport(PeerSocket(None))) is True


@pytest.mark.asyncio
async def test_client_arm_capture_sends_only_present_optional_sections(monkeypatch) -> None:
    client = InspectorClient("x.sock")
    calls: list[tuple[Any, Any]] = []

    async def call(command: Any, payload: Any = None) -> dict[str, Any]:
        calls.append((command, payload))
        return {}

    monkeypatch.setattr(client, "call", call)
    await client.arm_capture(token="x", expiry_seconds=1.0)
    await client.arm_capture(
        token="x",
        redaction={"body": "none"},
        budget={"slabs": 1},
        expiry_seconds=1.0,
    )
    assert "redaction" not in calls[0][1]
    assert "budget" not in calls[0][1]
    assert calls[1][1]["redaction"] == {"body": "none"}
    assert calls[1][1]["budget"] == {"slabs": 1}
