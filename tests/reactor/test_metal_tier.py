"""The metal tier, end to end.

`--loop metal` runs the real native server on the reactor loop with wheel-backed
timers. These drive the actual `wreath.server.Server` over real loopback sockets
and assert it serves HTTP/1.1, HTTP/2 (TLS+ALPN), and HTTP/3 (QUIC) with the
hashed wheel as the timer backend. Unlike the exploratory reactor.serve() specs
these replaced, everything here is the shipped path and passes.
"""
from __future__ import annotations

import asyncio
import datetime
import errno
import importlib
import json
import os
import selectors
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))  # tests/http2 codec


def _metal_loop():
    import wreath.reactor as r

    return r.metal_event_loop(diagnostics=True)


def _metal_component_loop(*, adaptive_polling: bool = True):
    import wreath.reactor as r

    return r.EventLoop(
        selectors.EpollSelector(),
        backend="epoll",
        timers="wheel",
        tasks="auto",
        stats=False,
        native_transport=True,
        native_loop=True,
        direct_task_steps=True,
        adaptive_polling=adaptive_polling,
    )


def test_wreath_execution_tier_process_memory_comparison() -> None:
    script = r'''
import asyncio
import gc
import json
import sys
import tracemalloc

tracemalloc.start()
mode = sys.argv[1]
if mode == "wreath":
    from wreath import Wreath
    app = Wreath()
    loop = asyncio.new_event_loop()
elif mode == "wreath-native":
    import wreath.reactor as reactor
    app = None
    loop = reactor.new_event_loop()
else:
    import wreath.reactor as reactor
    app = None
    loop = reactor.metal_event_loop(diagnostics=True)
loop.run_until_complete(asyncio.sleep(0))
gc.collect()
status = {}
with open("/proc/self/status") as source:
    for line in source:
        name, separator, value = line.partition(":")
        if separator and name in {"VmSize", "VmRSS", "RssAnon", "RssFile"}:
            status[name] = int(value.split()[0]) * 1024
current, peak = tracemalloc.get_traced_memory()
poller = getattr(loop, "_poller", None)
print(json.dumps({
    "mode": mode,
    **status,
    "python_traced_heap": current,
    "python_traced_peak": peak,
    "native_mapped": getattr(poller, "native_mapped_bytes", 0),
    "native_heap": getattr(poller, "native_heap_bytes", 0),
    "native_rings": getattr(poller, "native_ring_count", 0),
}))
loop.close()
del app
'''
    rows = []
    for mode in ("wreath", "wreath-native", "wreath-metal"):
        completed = subprocess.run(
            [sys.executable, "-c", script, mode],
            check=True,
            capture_output=True,
            text=True,
        )
        rows.append(json.loads(completed.stdout))

    print(json.dumps(rows, indent=2, sort_keys=True))
    by_mode = {row["mode"]: row for row in rows}
    assert set(by_mode) == {"wreath", "wreath-native", "wreath-metal"}
    for row in rows:
        assert 0 < row["VmRSS"] < 200 * 1024 * 1024, rows
        assert 0 < row["python_traced_heap"] < 64 * 1024 * 1024, rows
    assert by_mode["wreath"]["native_rings"] == 0
    assert by_mode["wreath-native"]["native_rings"] == 0
    assert by_mode["wreath-metal"]["native_rings"] == 1
    assert 16 * 1024 * 1024 <= by_mode["wreath-metal"]["native_mapped"] <= 17 * 1024 * 1024
    assert 220 * 1024 <= by_mode["wreath-metal"]["native_heap"] <= 230 * 1024


def test_wreath_execution_tier_request_work_comparison() -> None:
    """Count interpreter work for one equivalent request; this is not a timer."""
    script = r'''
import asyncio
import collections
import json
import socket
import sys
import threading

from wreath.server import Server, ServerConfig

mode = sys.argv[1]
if mode == "wreath":
    loop = asyncio.new_event_loop()
elif mode == "wreath-native":
    import wreath.reactor as reactor
    loop = reactor.new_event_loop()
else:
    import wreath.reactor as reactor
    loop = reactor.metal_event_loop(diagnostics=True)
asyncio.set_event_loop(loop)
app_calls = 0

async def app(scope, receive, send):
    global app_calls
    app_calls += 1
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"work"})

server = Server(
    app,
    ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
    loop,
)
loop.run_until_complete(server._start(ssl=None))
port = server.sockets[0].getsockname()[1]
done = loop.create_future()
responses = []
counts = collections.Counter({
    "python_calls": 0,
    "asyncio_calls": 0,
    "wreath_calls": 0,
    "accept_handoff_calls": 0,
    "handle_run_calls": 0,
    "future_calls": 0,
    "task_calls": 0,
})
poller = getattr(loop, "_poller", None)
ring_fields = (
    "submitted_sqes",
    "submission_batches",
    "sq_tail_publications",
    "cq_head_publications",
    "enter_calls",
    "accept_completions",
    "accept_native_activations",
    "receive_completions",
    "direct_receive_completions",
    "direct_receive_bytes",
    "send_completions",
    "send_zc_notifications",
    "send_zc_copied",
    "blocking_enters",
    "uring_waits",
    "readiness_callbacks",
)
ring_before = {name: getattr(poller, name, 0) for name in ring_fields}
zero_counts = dict(counts)
first_done = threading.Event()
second_done = threading.Event()
phase_rows = []


def capture(scenario):
    global app_calls, ring_before
    row = {
        "mode": mode,
        "scenario": scenario,
        **counts,
        "app_calls": app_calls,
        "response_bytes": len(responses[-1]),
    }
    for name in ring_fields:
        row["ring_" + name] = getattr(poller, name, 0) - ring_before[name]
    counts.clear()
    counts.update(zero_counts)
    app_calls = 0
    ring_before = {name: getattr(poller, name, 0) for name in ring_fields}
    return row


def checkpoint_first_request():
    sys.setprofile(None)
    phase_rows.append(capture("fresh_connection"))
    first_done.set()
    sys.setprofile(profile)


def checkpoint_keepalive_request():
    sys.setprofile(None)
    phase_rows.append(capture("keepalive_increment"))
    second_done.set()


def profile(frame, event, arg):
    if event != "call":
        return
    module = frame.f_globals.get("__name__", "")
    name = frame.f_code.co_name
    counts["python_calls"] += 1
    if module == "asyncio" or module.startswith("asyncio."):
        counts["asyncio_calls"] += 1
    if module == "wreath" or module.startswith("wreath."):
        counts["wreath_calls"] += 1
    if name in {"_metal_accept_connection", "_accept_connection2"}:
        counts["accept_handoff_calls"] += 1
    if name == "_run" and module.startswith("asyncio"):
        counts["handle_run_calls"] += 1
    if "future" in module:
        counts["future_calls"] += 1
    if "task" in module:
        counts["task_calls"] += 1


def read_response(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        data.extend(sock.recv(4096))
    header, body = bytes(data).split(b"\r\n\r\n", 1)
    length = None
    chunked = False
    for line in header.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value.strip())
        elif name.lower() == b"transfer-encoding" and b"chunked" in value.lower():
            chunked = True
    if length is not None:
        while len(body) < length:
            body += sock.recv(4096)
        return header + b"\r\n\r\n" + body[:length]
    if chunked:
        while b"\r\n0\r\n\r\n" not in body:
            body += sock.recv(4096)
        end = body.index(b"\r\n0\r\n\r\n") + 7
        return header + b"\r\n\r\n" + body[:end]
    raise AssertionError("keep-alive response has no explicit body boundary")


def client():
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
        sock.sendall(
            b"GET /work HTTP/1.1\r\nHost: test\r\nConnection: keep-alive\r\n\r\n"
        )
        responses.append(read_response(sock))
        loop.call_soon_threadsafe(checkpoint_first_request)
        if not first_done.wait(2.0):
            raise TimeoutError("first request checkpoint was not dispatched")
        sock.sendall(
            b"GET /work HTTP/1.1\r\nHost: test\r\nConnection: keep-alive\r\n\r\n"
        )
        responses.append(read_response(sock))
        loop.call_soon_threadsafe(checkpoint_keepalive_request)
        if not second_done.wait(2.0):
            raise TimeoutError("keep-alive checkpoint was not dispatched")
        sock.sendall(
            b"GET /work HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n"
        )
        read_response(sock)
    loop.call_soon_threadsafe(done.set_result, None)

thread = threading.Thread(target=client)
sys.setprofile(profile)
thread.start()
loop.run_until_complete(done)
sys.setprofile(None)
thread.join()
loop.run_until_complete(server.close())
print(json.dumps(phase_rows))
loop.close()
'''
    rows = []
    for mode in ("wreath", "wreath-native", "wreath-metal"):
        completed = subprocess.run(
            [sys.executable, "-c", script, mode],
            check=True,
            capture_output=True,
            text=True,
        )
        rows.extend(json.loads(completed.stdout))

    print(json.dumps(rows, indent=2, sort_keys=True))
    print("FLOW_SUMMARY " + json.dumps([
        {
            "mode": row["mode"],
            "scenario": row["scenario"],
            "python": row["python_calls"],
            "asyncio": row["asyncio_calls"],
            "wreath": row["wreath_calls"],
            "accept": row["accept_handoff_calls"],
            "native_accept": row["ring_accept_native_activations"],
            "handles": row["handle_run_calls"],
            "futures": row["future_calls"],
            "tasks": row["task_calls"],
            "sqes": row["ring_submitted_sqes"],
            "enters": row["ring_enter_calls"],
            "blocking_enters": row["ring_blocking_enters"],
            "receives": row["ring_receive_completions"],
            "sends": row["ring_send_completions"],
        }
        for row in rows
    ], sort_keys=True))
    by_mode = {(row["mode"], row["scenario"]): row for row in rows}
    assert all(row["app_calls"] == 1 for row in rows)
    assert len({row["response_bytes"] for row in rows
                if row["scenario"] == "fresh_connection"}) == 1
    for scenario in ("fresh_connection", "keepalive_increment"):
        assert (
            by_mode["wreath-metal", scenario]["python_calls"]
            < by_mode["wreath", scenario]["python_calls"]
        )
        assert (
            by_mode["wreath-metal", scenario]["asyncio_calls"]
            < by_mode["wreath-native", scenario]["asyncio_calls"]
        )
    metal = by_mode["wreath-metal", "fresh_connection"]
    assert by_mode["wreath", "fresh_connection"]["accept_handoff_calls"] == 2
    assert by_mode["wreath-native", "fresh_connection"]["accept_handoff_calls"] == 2
    assert metal["accept_handoff_calls"] == 0
    assert metal["ring_accept_native_activations"] == 1
    assert metal["handle_run_calls"] <= 2
    assert metal["ring_submitted_sqes"] > 0
    assert metal["ring_enter_calls"] == metal["ring_blocking_enters"]
    assert metal["ring_sq_tail_publications"] <= metal["ring_enter_calls"]
    assert metal["ring_cq_head_publications"] <= metal["ring_enter_calls"]
    assert metal["ring_receive_completions"] >= 1
    assert metal["ring_direct_receive_completions"] == 0
    assert metal["ring_direct_receive_bytes"] == 0
    # each response leaves as one async SEND riding the loop's next enter
    assert metal["ring_send_completions"] >= 1
    keepalive = by_mode["wreath-metal", "keepalive_increment"]
    assert keepalive["python_calls"] <= 4
    assert keepalive["asyncio_calls"] <= 2
    assert keepalive["ring_enter_calls"] == keepalive["ring_blocking_enters"]
    assert keepalive["ring_sq_tail_publications"] <= keepalive["ring_enter_calls"]
    assert keepalive["accept_handoff_calls"] == 0
    assert keepalive["ring_receive_completions"] == 1
    assert keepalive["ring_direct_receive_completions"] == 0
    assert keepalive["ring_direct_receive_bytes"] == 0
    assert keepalive["ring_send_completions"] == 1


def test_metal_defaults_to_poller_driven_wheel_without_bridge_heartbeat():
    loop = _metal_loop()
    fired: list[bool] = []
    try:
        assert loop.reactor_timers() == "wheel"
        assert loop._poller.connection_capacity == 4096
        assert loop._poller.operation_capacity == 4096
        assert loop._poller.completion_trace() == []
        loop.call_later(0.005, fired.append, True)
        assert loop._wheel_tick_handle is None
        loop.run_until_complete(asyncio.sleep(0.02))
        assert fired == [True]
        assert loop._wheel_tick_handle is None
        assert loop._poller.uring_waits >= 1
        assert loop._poller.uring_timeouts >= 1
        assert loop._poller.epoll_active is False
    finally:
        loop.close()


def test_metal_source_has_no_epoll_aggregation_path() -> None:
    source = Path("src/wreath/_native/_reactormodule.c").read_text()
    assert "<sys/epoll.h>" not in source
    assert "epoll_create1(" not in source
    assert "epoll_ctl(" not in source
    assert "epoll_wait(" not in source


def test_metal_udp_readiness_is_owned_by_the_unified_ring() -> None:
    loop = _metal_loop()
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.setblocking(False)
    receiver.bind(("127.0.0.1", 0))
    received: list[bytes] = []
    try:
        submissions_before = loop._poller.poll_submissions
        completions_before = loop._poller.poll_completions
        callbacks_before = loop._poller.readiness_callbacks

        async def exercise() -> None:
            done = loop.create_future()

            def readable() -> None:
                payload, _address = receiver.recvfrom(2048)
                received.append(payload)
                loop.remove_reader(receiver.fileno())
                done.set_result(None)

            loop.add_reader(receiver.fileno(), readable)
            sender.sendto(b"quic-datagram", receiver.getsockname())
            await asyncio.wait_for(done, 1.0)

        loop.run_until_complete(exercise())
        assert received == [b"quic-datagram"]
        assert loop._poller.poll_submissions > submissions_before
        assert loop._poller.poll_completions > completions_before
        assert loop._poller.poll_cancellations >= 1
        assert loop._poller.readiness_callbacks == callbacks_before + 1
        assert loop._poller.epoll_active is False
    finally:
        receiver.close()
        sender.close()
        loop.close()


def test_metal_negative_fd_removal_is_a_safe_noop() -> None:
    loop = _metal_loop()
    try:
        assert loop._poller._remove_reader(-1) is False
        assert loop._poller._remove_writer(-1) is False
    finally:
        loop.close()


def test_metal_coalesces_pending_eventfd_wakes() -> None:
    loop = _metal_loop()
    try:
        before_requests = loop._poller.wake_requests
        before_writes = loop._poller.wake_writes
        before_coalesced = loop._poller.wake_coalesced
        for _ in range(8):
            loop._poller._wake()
        assert loop._poller.wake_requests - before_requests == 8
        assert loop._poller.wake_writes - before_writes == 1
        assert loop._poller.wake_coalesced - before_coalesced == 7
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()


def test_metal_native_memory_and_kernel_object_baseline() -> None:
    loop = _metal_loop()
    try:
        poller = loop._poller
        assert poller.native_ring_count == 1
        assert 16 * 1024 * 1024 <= poller.native_mapped_bytes <= 17 * 1024 * 1024
        assert 220 * 1024 <= poller.native_heap_bytes <= 230 * 1024
        with pytest.raises(ValueError, match="closed epoll"):
            loop._selector.fileno()
        assert poller.submission_batches == 0
        assert poller.submitted_sqes == 0
    finally:
        loop.close()


def test_metal_close_releases_native_mappings_and_kernel_rings() -> None:
    loop = _metal_loop()
    poller = loop._poller
    assert poller.native_ring_count == 1
    assert poller.native_mapped_bytes > 0
    loop.close()
    assert poller.native_ring_count == 0
    assert poller.native_mapped_bytes == 0


def test_metal_cross_thread_wake_is_an_io_uring_completion() -> None:
    loop = _metal_loop()
    future = loop.create_future()

    def resolve() -> None:
        import time

        time.sleep(0.01)
        loop.call_soon_threadsafe(future.set_result, "awake")

    thread = threading.Thread(target=resolve)
    try:
        thread.start()
        assert loop.run_until_complete(asyncio.wait_for(future, 1.0)) == "awake"
        thread.join()
        assert loop._poller.wake_requests >= 1
        assert loop._poller.wake_completions >= 1
        # multishot wake poll: the initial arm covers every wake unless the
        # kernel downgraded it to one-shot rearms
        assert 1 <= loop._poller.wake_submissions <= loop._poller.wake_completions + 1
    finally:
        loop.close()


def test_metal_signal_wake_is_an_io_uring_completion() -> None:
    loop = _metal_loop()
    seen = []
    try:
        loop.add_signal_handler(signal.SIGUSR1, seen.append, signal.SIGUSR1)
        os.kill(os.getpid(), signal.SIGUSR1)
        loop.run_until_complete(asyncio.sleep(0.01))
        assert seen == [signal.SIGUSR1]
        assert loop._poller.signal_completions >= 1
        # multishot signal poll: one arm covers every delivery unless the
        # kernel downgraded it to one-shot rearms
        assert (
            1
            <= loop._poller.signal_submissions
            <= loop._poller.signal_completions + 1
        )
    finally:
        loop.remove_signal_handler(signal.SIGUSR1)
        loop.close()


def test_metal_trace_memory_is_absent_unless_diagnostics_are_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _metal_loop()
    baseline = loop._poller.native_heap_bytes
    loop.close()

    monkeypatch.setenv("WREATH_METAL_TRACE", "1")
    traced = _metal_loop()
    try:
        trace_bytes = traced._poller.native_heap_bytes - baseline
        assert 4 * 1024 <= trace_bytes <= 8 * 1024
    finally:
        traced.close()


def test_metal_directly_dispatches_c_task_steps_with_their_context(monkeypatch):
    import contextvars
    from asyncio import events

    original_run = events.Handle._run

    def reject_python_task_step(self):
        if type(self._callback).__name__ == "TaskStepMethWrapper":
            raise AssertionError("C task step went through Handle._run")
        return original_run(self)

    monkeypatch.setattr(events.Handle, "_run", reject_python_task_step)
    value: contextvars.ContextVar[str] = contextvars.ContextVar("value", default="outside")
    loop = _metal_loop()
    try:
        async def resumed():
            value.set("captured")
            await asyncio.sleep(0)
            return value.get()

        assert loop.run_until_complete(resumed()) == "captured"
    finally:
        loop.close()


def test_metal_task_step_dispatch_is_not_runtime_selectable(monkeypatch):
    monkeypatch.setenv("WREATH_METAL_TASK_STEPS", "handle")
    loop = _metal_loop()
    try:
        assert loop._direct_task_steps is True
    finally:
        loop.close()


def _dev_cert() -> tuple[str, str]:
    crypto = pytest.importorskip("cryptography")  # noqa: F841
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cf, cp = tempfile.mkstemp(suffix=".pem")
    kf, kp = tempfile.mkstemp(suffix=".pem")
    os.write(cf, cert.public_bytes(serialization.Encoding.PEM))
    os.close(cf)
    os.write(kf, key.private_bytes(serialization.Encoding.PEM,
             serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    os.close(kf)
    return cp, kp


async def _echo(scope, receive, send):
    while True:
        m = await receive()
        if m["type"] == "http.disconnect":
            return
        if not m.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"metal-" + scope["type"].encode()})


def _serve(loop, protocols, *, ssl_ctx=None, tls=None):
    from wreath.server import Server, ServerConfig

    cfg = ServerConfig(protocols=protocols, host="127.0.0.1", port=0, lifespan="off")
    srv = Server(_echo, cfg, loop)
    loop.run_until_complete(srv._start(ssl=ssl_ctx, tls=tls))
    return srv


def test_native_transport_fuses_http1_ingress_without_python_buffer_callbacks():
    """The metal transport and native HTTP/1 protocol meet through their C API."""
    Http1Protocol = importlib.import_module("wreath._native._server").Http1Protocol
    ServerConfig = importlib.import_module("wreath.server").ServerConfig
    callbacks: list[str] = []

    class ObservedProtocol(Http1Protocol):
        def get_buffer(self, sizehint):
            callbacks.append("get_buffer")
            return super().get_buffer(sizehint)

        def buffer_updated(self, nbytes):
            callbacks.append("buffer_updated")
            return super().buffer_updated(nbytes)

    loop = _metal_loop()
    client, server = socket.socketpair()
    client.setblocking(False)
    try:
        protocol = ObservedProtocol(
            _echo, ServerConfig(lifespan="off"), loop, set()
        )
        transport = loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        assert transport._fused_http1 is True
        assert transport._metal_connection_token != 0
        assert transport._metal_worker_id == 0
        receives_before = loop._poller.receive_completions
        client.sendall(b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0.01))
        response = client.recv(4096)

        assert b"200 OK" in response
        assert b"metal-http" in response
        assert callbacks == []
        assert 1 <= loop._poller.receive_completions - receives_before <= 2
        # the fused response leaves as one async SEND completion
        assert loop._poller.send_completions == 1
        assert transport._direct_protocol_writes >= 1
        assert transport._zero_copy_cork_writes >= 1
        assert transport._metal_operation_exhaustions == 0
        assert transport._metal_cross_worker_rejections == 0
        transport.close()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        client.close()
        loop.close()


def test_external_buffer_ingress_preserves_fragmented_request_body():
    Http1Protocol = importlib.import_module("wreath._native._server").Http1Protocol
    ServerConfig = importlib.import_module("wreath.server").ServerConfig

    async def echo_body(scope, receive, send):
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": body})

    loop = _metal_loop()
    client, server = socket.socketpair()
    client.setblocking(False)
    body = b"external-buffer-body"
    try:
        protocol = Http1Protocol(
            echo_body, ServerConfig(lifespan="off"), loop, set()
        )
        loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        head = (
            b"POST / HTTP/1.1\r\nHost: test\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nConnection: close\r\n\r\n"
        )
        client.sendall(head + body[:5])
        loop.run_until_complete(asyncio.sleep(0.002))
        client.sendall(body[5:])
        loop.run_until_complete(asyncio.sleep(0.01))
        response = client.recv(4096)
        assert response.endswith(body)
    finally:
        client.close()
        loop.close()


def test_metal_connection_slots_are_per_worker_and_generation_validated():
    import wreath.reactor as reactor

    class Protocol(asyncio.Protocol):
        pass

    loop = reactor.metal_event_loop(worker_id=7)
    client1, server1 = socket.socketpair()
    client2 = server2 = None
    try:
        first = loop._make_socket_transport(server1, Protocol())
        loop.run_until_complete(asyncio.sleep(0))
        token1 = first._metal_connection_token
        assert first._metal_worker_id == 7
        first.abort()
        loop.run_until_complete(asyncio.sleep(0))

        client2, server2 = socket.socketpair()
        second = loop._make_socket_transport(server2, Protocol())
        loop.run_until_complete(asyncio.sleep(0))
        token2 = second._metal_connection_token

        assert token2 != token1
        assert token2 & 0xFFFFFFFF == token1 & 0xFFFFFFFF
        assert token2 >> 32 > token1 >> 32
        second.abort()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        client1.close()
        if client2 is not None:
            client2.close()
        loop.close()


def test_metal_io_uring_owns_listener_accept_and_drains_cqes():
    import wreath.reactor as reactor
    from wreath.server import Server, ServerConfig

    try:
        loop = reactor.metal_event_loop(diagnostics=True)
    except OSError as exc:
        assert exc.errno in {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOMEM}
        return

    async def exercise():
        server = Server(
            _echo,
            ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
            loop,
        )
        await server._start(ssl=None)
        try:
            port = server.sockets[0].getsockname()[1]
            clients = await asyncio.gather(*(
                asyncio.open_connection("127.0.0.1", port) for _ in range(4)
            ))
            for _reader, writer in clients:
                writer.write(
                    b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n"
                )
            wake_seen = []
            wake_thread = threading.Thread(
                target=lambda: loop.call_soon_threadsafe(wake_seen.append, True)
            )
            wake_thread.start()
            await asyncio.gather(*(writer.drain() for _reader, writer in clients))
            responses = await asyncio.gather(*(reader.read() for reader, _writer in clients))
            for _reader, writer in clients:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for _reader, writer in clients))
            wake_thread.join()
            await asyncio.sleep(0)
            assert wake_seen == [True]
            assert all(b"200 OK" in response for response in responses)
            assert all(b"metal-http" in response for response in responses)
            assert loop._poller.native_ring_count == 1
            assert loop._poller.accept_submissions >= 1
            assert loop._poller.accept_completions >= 4
            assert loop._poller.wake_completions >= 1
            if loop._poller.receive_enabled:
                assert loop._poller.provided_buffer_count == 1024
                assert loop._poller.receive_completions >= 4
                assert loop._poller.provided_buffer_recycles >= 4
                assert loop._poller.buffer_descriptor_occupancy == 1024
                assert loop._poller.buffer_descriptor_high_water == 1024
                assert loop._poller.buffer_descriptor_exhaustions == 0
                assert loop._poller.provided_buffer_exhaustions == 0
                assert loop._poller.buffer_descriptor_stale == 0
                assert loop._poller.buffer_descriptor_generation_wraps == 0
            if loop._poller.send_enabled:
                assert loop._poller.send_submissions >= 4
                assert loop._poller.send_completions >= 4
                assert loop._poller.send_copy_bytes > 0
                assert loop._poller.send_zc_bytes == 0
                assert loop._poller.send_zc_notifications == 0
                assert loop._poller.send_zc_copied == 0
            assert loop._poller.submitted_sqes > loop._poller.submission_batches
        finally:
            await server.close()

    try:
        loop.run_until_complete(exercise())
    finally:
        loop.close()


def test_metal_direct_send_emits_large_immutable_body() -> None:
    from wreath.server import Server, ServerConfig

    body = b"x" * (32 * 1024)

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": body})

    loop = _metal_loop()

    async def exercise() -> None:
        server = Server(
            app,
            ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
            loop,
        )
        await server._start(ssl=None)
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            writer.write(
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            assert response.endswith(body)
            # the head leaves through the async SEND path; the large
            # immutable body still exits via the vectored sendmsg fast path
            assert loop._poller.send_submissions >= 1
            assert loop._poller.send_completions >= 1
        finally:
            await server.close()

    try:
        loop.run_until_complete(exercise())
    finally:
        loop.close()


def test_io_uring_http1_completion_trace_accounts_for_request_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WREATH_METAL_TRACE", "1")
    Http1Protocol = importlib.import_module("wreath._native._server").Http1Protocol
    ServerConfig = importlib.import_module("wreath.server").ServerConfig
    loop = _metal_loop()
    client, server = socket.socketpair()
    client.setblocking(False)
    try:
        protocol = Http1Protocol(
            _echo, ServerConfig(lifespan="off"), loop, set()
        )
        loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        request = b"GET /trace HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n"
        client.sendall(request)
        loop.run_until_complete(asyncio.sleep(0.01))
        response = client.recv(4096)
        rows = [
            (kind, result, flags)
            for _seq, _token, result, kind, _backend, flags
            in loop._poller.completion_trace()
            if kind in (1, 2) and result >= 0
        ]
        kinds = [kind for kind, _result, _flags in rows]
        totals = {
            kind: sum(result for row_kind, result, _flags in rows
                      if row_kind == kind)
            for kind in (1, 2)
        }
        assert b"200 OK" in response
        # one ingress RECV row, then the response's async SEND row
        assert kinds == [1, 2]
        assert totals[1] == len(request)
        assert totals[2] == len(response)
    finally:
        client.close()
        loop.close()


def test_metal_native_arena_capacities_are_bounded_and_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.reactor as reactor

    monkeypatch.setenv("WREATH_METAL_CONNECTION_CAPACITY", "32")
    monkeypatch.setenv("WREATH_METAL_OPERATION_CAPACITY", "64")
    monkeypatch.setenv("WREATH_METAL_RECV_BUFFERS", "32")
    loop = reactor.metal_event_loop(diagnostics=True)
    try:
        assert loop._poller.connection_capacity == 32
        assert loop._poller.operation_capacity == 64
        assert loop._poller.buffer_descriptor_capacity == 32
        assert loop._poller.native_heap_bytes < 64 * 1024
        assert 512 * 1024 <= loop._poller.native_mapped_bytes <= 640 * 1024
    finally:
        loop.close()

    monkeypatch.setenv("WREATH_METAL_RECV_BUFFERS", "24")
    with pytest.raises(ValueError, match="power of two"):
        reactor.metal_event_loop()


def test_adaptive_polling_component_can_be_disabled_for_differential_coverage():
    try:
        loop = _metal_component_loop(adaptive_polling=False)
    except OSError as exc:
        assert exc.errno in {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOMEM}
        return
    try:
        loop.run_until_complete(asyncio.sleep(0.002))
        assert loop._poller.adaptive_polling is False
        assert loop._poller.spin_attempts == 0
        assert loop._poller.spin_hits == 0
        assert loop._poller.spin_nanoseconds == 0
        assert loop._poller.blocking_enters >= 1
    finally:
        loop.close()


def test_adaptive_polling_records_arrival_samples() -> None:
    """A blocked wait woken by a completion feeds the spin predictor's EWMA."""
    loop = _metal_loop()
    try:
        assert loop._poller.adaptive_polling is True

        async def wake_while_blocked():
            future = loop.create_future()

            def resolve():
                loop.call_soon_threadsafe(future.set_result, "awake")

            timer = threading.Timer(0.02, resolve)
            timer.start()
            try:
                return await asyncio.wait_for(future, 1.0)
            finally:
                timer.join()

        assert loop.run_until_complete(wake_while_blocked()) == "awake"
        assert loop._poller.arrival_samples >= 1
        assert loop._poller.arrival_ewma_ns > 0.0
    finally:
        loop.close()


def test_metal_async_send_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    import wreath.reactor as reactor

    Http1Protocol = importlib.import_module("wreath._native._server").Http1Protocol
    ServerConfig = importlib.import_module("wreath.server").ServerConfig

    monkeypatch.setenv("WREATH_METAL_ASYNC_SEND", "0")
    loop = _metal_loop()
    client, server = socket.socketpair()
    client.setblocking(False)
    try:
        assert loop._poller.send_enabled is False
        protocol = Http1Protocol(_echo, ServerConfig(lifespan="off"), loop, set())
        loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        client.sendall(b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0.01))
        response = client.recv(4096)
        assert b"200 OK" in response
        # every byte left through the synchronous fallback
        assert loop._poller.send_submissions == 0
        assert loop._poller.send_completions == 0
    finally:
        client.close()
        loop.close()

    monkeypatch.setenv("WREATH_METAL_ASYNC_SEND", "2")
    with pytest.raises(ValueError, match="WREATH_METAL_ASYNC_SEND"):
        reactor.metal_event_loop()


def test_native_accept_resolves_lazy_peer_addresses() -> None:
    """sockname/peername defer out of the accept path but still resolve."""
    from wreath.server import Server, ServerConfig

    captured = {}

    async def app(scope, receive, send):
        captured["client"] = scope.get("client")
        captured["server"] = scope.get("server")
        await receive()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    loop = _metal_loop()

    async def exercise():
        server = Server(
            app, ServerConfig(host="127.0.0.1", port=0, lifespan="off"), loop
        )
        await server._start(ssl=None)
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()

    try:
        loop.run_until_complete(exercise())
    finally:
        loop.close()
    assert captured["client"] is not None
    assert captured["client"][0] == "127.0.0.1"
    assert captured["server"] is not None
    assert captured["server"][0] == "127.0.0.1"


def test_metal_completion_trace_records_timer_wait_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WREATH_METAL_TRACE", "1")
    loop = _metal_loop()
    try:
        fired = loop.create_future()
        loop.call_later(0.002, fired.set_result, None)
        loop.run_until_complete(fired)
        assert any(kind == 6 and result == 0
                   for _seq, _token, result, kind, _backend, _flags
                   in loop._poller.completion_trace())
    finally:
        loop.close()


def test_metal_completion_trace_records_receive_cancellation_and_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WREATH_METAL_TRACE", "1")
    loop = _metal_loop()

    class Protocol(asyncio.Protocol):
        def __init__(self) -> None:
            self.eof = False

        def eof_received(self) -> None:
            self.eof = True

    client, server = socket.socketpair()
    client.setblocking(False)
    protocol = Protocol()
    try:
        transport = loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        transport.pause_reading()
        loop.run_until_complete(asyncio.sleep(0.01))
        transport.resume_reading()
        loop.run_until_complete(asyncio.sleep(0))
        client.close()
        loop.run_until_complete(asyncio.sleep(0.01))

        trace = loop._poller.completion_trace()
        assert any(kind == 4 and result >= 0
                   for _seq, _token, result, kind, _backend, _flags in trace)
        assert any(kind == 1 and result == 0 and flags & 0x1
                   for _seq, _token, result, kind, _backend, flags in trace)
        assert protocol.eof is True
    finally:
        client.close()
        loop.close()


def test_metal_io_uring_receive_pause_cancels_and_resume_rearms():
    import wreath.reactor as reactor

    class PausingProtocol(asyncio.Protocol):
        def __init__(self):
            self.transport = None
            self.chunks = []

        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            self.chunks.append(data)
            if len(self.chunks) == 1:
                self.transport.pause_reading()

    try:
        loop = reactor.metal_event_loop(diagnostics=True)
    except OSError as exc:
        assert exc.errno in {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOMEM}
        return
    if not loop._poller.receive_enabled:
        loop.close()
        return

    client, server = socket.socketpair()
    client.setblocking(False)
    protocol = PausingProtocol()
    try:
        transport = loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        client.sendall(b"first")
        loop.run_until_complete(asyncio.sleep(0.01))
        assert b"".join(protocol.chunks) == b"first"
        assert not transport.is_reading()

        client.sendall(b"second")
        loop.run_until_complete(asyncio.sleep(0.01))
        assert b"".join(protocol.chunks) == b"first"

        transport.resume_reading()
        loop.run_until_complete(asyncio.sleep(0.01))
        assert b"".join(protocol.chunks) == b"firstsecond"
        assert loop._poller.receive_completions >= 2
        assert loop._poller.provided_buffer_recycles >= 2
        transport.close()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        client.close()
        loop.close()


def test_metal_io_uring_send_completion_retains_payload_and_drains_before_close():
    import wreath.reactor as reactor

    class Protocol(asyncio.Protocol):
        def __init__(self):
            self.lost = False

        def connection_lost(self, exc):
            assert exc is None
            self.lost = True

    try:
        loop = reactor.metal_event_loop(diagnostics=True)
    except OSError as exc:
        assert exc.errno in {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOMEM}
        return
    if not loop._poller.send_enabled:
        loop.close()
        return

    client, server = socket.socketpair()
    client.setblocking(False)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    protocol = Protocol()
    first = b"a" * (256 * 1024)
    second = b"b" * (64 * 1024)
    received = bytearray()
    try:
        transport = loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        transport.write(first)
        transport.write(second)
        assert transport.get_write_buffer_size() > 0
        transport.close()
        assert not protocol.lost

        for _ in range(1000):
            loop.run_until_complete(asyncio.sleep(0.001))
            while True:
                try:
                    chunk = client.recv(64 * 1024)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                received.extend(chunk)
            if protocol.lost:
                break

        assert bytes(received) == first + second
        assert protocol.lost
        assert transport.get_write_buffer_size() == 0
        assert loop._poller.send_submissions >= 2
        assert loop._poller.send_completions >= 2
    finally:
        client.close()
        loop.close()


def test_metal_workers_can_own_a_reuseport_listener_group():
    import wreath.reactor as reactor
    from wreath.server import Server, ServerConfig

    loop1 = reactor.metal_event_loop(worker_id=1, reuse_port=True)
    loop2 = reactor.metal_event_loop(worker_id=2, reuse_port=True)
    server1 = server2 = None
    try:
        server1 = Server(
            _echo,
            ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
            loop1,
        )
        loop1.run_until_complete(server1._start(ssl=None))
        port = server1.sockets[0].getsockname()[1]

        server2 = Server(
            _echo,
            ServerConfig(host="127.0.0.1", port=port, lifespan="off"),
            loop2,
        )
        loop2.run_until_complete(server2._start(ssl=None))

        assert server1.sockets[0].getsockname()[1] == port
        assert server2.sockets[0].getsockname()[1] == port
        assert loop1._worker_id == 1
        assert loop2._worker_id == 2
    finally:
        if server2 is not None:
            loop2.run_until_complete(server2.close())
        if server1 is not None:
            loop1.run_until_complete(server1.close())
        loop2.close()
        loop1.close()


def test_metal_http1_io_uring_arm_is_explicit_and_operational_when_available():
    import wreath.reactor as reactor

    Http1Protocol = importlib.import_module("wreath._native._server").Http1Protocol
    ServerConfig = importlib.import_module("wreath.server").ServerConfig
    try:
        loop = reactor.metal_event_loop(diagnostics=True)
    except OSError as exc:
        # Explicit selection reports host/kernel policy; it must never fall back
        # silently to epoll.
        assert exc.errno in {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOMEM}
        return

    client, server = socket.socketpair()
    client.setblocking(False)
    try:
        protocol = Http1Protocol(_echo, ServerConfig(lifespan="off"), loop, set())
        transport = loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        assert transport._metal_io_backend == "io_uring"

        client.sendall(b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0.01))
        response = client.recv(4096)
        assert b"200 OK" in response
        assert b"metal-http" in response
        # one async SEND operation carried the response
        assert transport._metal_submissions == 1
        assert transport._metal_completions == 1
        if loop._poller.receive_enabled:
            assert loop._poller.provided_buffer_count == 1024
            assert loop._poller.receive_completions >= 1
            assert loop._poller.direct_receive_completions == 0
            assert loop._poller.direct_receive_bytes == 0
            assert loop._poller.provided_buffer_recycles >= 1
    finally:
        client.close()
        loop.close()


def test_native_transport_uses_vectored_io_for_large_writelines():
    class Protocol(asyncio.Protocol):
        pass

    loop = _metal_loop()
    client, server = socket.socketpair()
    client.setblocking(False)
    try:
        transport = loop._make_socket_transport(server, Protocol())
        loop.run_until_complete(asyncio.sleep(0))
        parts = [b"a" * 32768, b"b" * 32768]
        transport.writelines(parts)
        received = bytearray()
        while len(received) < 65536:
            received += loop.run_until_complete(loop.sock_recv(client, 65536))

        assert received == b"".join(parts)
        assert transport._direct_writelines == 1
        transport.close()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        client.close()
        loop.close()


def test_native_transport_is_collected_after_close():
    """The native SocketTransport must not leak: its self-referential bound
    methods have to be visited by GC so a closed connection is collected."""
    import gc

    loop = _metal_loop()

    def live():
        return sum(1 for o in gc.get_objects()
                   if type(o).__name__ == "SocketTransport")

    class P(asyncio.Protocol):
        def connection_made(self, t):
            pass

        def connection_lost(self, e):
            pass

    try:
        base = live()

        async def churn():
            peers = []
            for _ in range(30):
                a, b = socket.socketpair()
                tr = loop._make_socket_transport(a, P())
                peers.append(b)
                await asyncio.sleep(0)
                tr.close()
            await asyncio.sleep(0.05)
            for b in peers:
                b.close()

        loop.run_until_complete(churn())
        gc.collect()
        assert live() <= base
    finally:
        loop.close()


def test_metal_serves_http1_on_the_wheel():
    loop = _metal_loop()
    try:
        assert loop.reactor_timers() == "wheel"
        srv = _serve(loop, ("http/1.1",))
        port = srv.sockets[0].getsockname()[1]
        out: list = []

        def client():
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
                      b"Connection: close\r\n\r\n")
            data = b""
            while True:
                c = s.recv(4096)
                if not c:
                    break
                data += c
            out.append(data)
            s.close()

        async def drive():
            t = threading.Thread(target=client)
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await srv.close()

        loop.run_until_complete(drive())
        assert out[0].startswith(b"HTTP/1.1 200")
        assert out[0].endswith(b"metal-http")
        assert loop.reactor_timers() == "wheel"  # deadlines rode the native wheel
    finally:
        loop.close()


def test_metal_serves_http2_over_tls():
    pytest.importorskip("cryptography")
    from http2 import support as h2  # type: ignore

    from wreath.server import TLSConfig

    cp, kp = _dev_cert()
    loop = _metal_loop()
    try:
        ssl_ctx = TLSConfig(certfile=cp, keyfile=kp).build_ssl_context(("h2",))
        srv = _serve(loop, ("h2",), ssl_ctx=ssl_ctx)
        port = srv.sockets[0].getsockname()[1]
        out: dict = {}

        def client():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_alpn_protocols(["h2"])
            s = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5),
                                server_hostname="localhost")
            out["alpn"] = s.selected_alpn_protocol()
            s.sendall(h2.PREFACE + h2.encode_settings({}))
            s.sendall(h2.build_headers_frame(1, h2.request_headers(), end_stream=True))
            data = b""
            s.settimeout(3.0)
            try:
                while b"metal-http" not in data:
                    c = s.recv(4096)
                    if not c:
                        break
                    data += c
            except OSError:
                pass
            out["raw"] = data
            s.close()

        async def drive():
            t = threading.Thread(target=client)
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await srv.close()

        loop.run_until_complete(drive())
        assert out["alpn"] == "h2"
        parser = h2.FrameParser()
        parser.feed(out["raw"])
        assert any(f.type == h2.DATA and b"metal-http" in f.payload for f in parser.frames())
    finally:
        loop.close()
        os.unlink(cp)
        os.unlink(kp)


def _curl_has_http3() -> bool:
    curl = shutil.which("curl")
    if not curl:
        return False
    try:
        out = subprocess.run([curl, "--version"], capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return "http3" in out.stdout.lower()


def test_metal_serves_http3_over_quic():
    pytest.importorskip("cryptography")
    import importlib.util

    if importlib.util.find_spec("wreath._native._http3") is None:
        pytest.skip("native HTTP/3 extension not built")
    if not _curl_has_http3():
        pytest.skip("curl without HTTP/3 support")

    from wreath.server import TLSConfig

    cp, kp = _dev_cert()
    loop = _metal_loop()
    try:
        srv = _serve(loop, ("h3",), tls=TLSConfig(certfile=cp, keyfile=kp))
        udp_port = srv.datagram_addresses[0][1]
        out: dict = {}

        def client():
            out["proc"] = subprocess.run(
                ["curl", "--http3-only", "-sk", f"https://127.0.0.1:{udp_port}/"],
                capture_output=True, timeout=15)

        async def drive():
            t = threading.Thread(target=client)
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await srv.close()

        loop.run_until_complete(drive())
        proc = out["proc"]
        assert proc.returncode == 0
        assert b"metal-http" in proc.stdout
    finally:
        loop.close()
        os.unlink(cp)
        os.unlink(kp)
