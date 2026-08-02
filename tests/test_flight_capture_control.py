"""Stage 5 slice 5d: Inspector capture-control commands (arm/disarm/status).

These are the first *mutating* Inspector commands, and the security-critical part
of Stage 5: a runtime arm needs the capability token (separate from read access),
can never exceed the startup redaction/memory ceiling, and is bounded by expiry
and a maximum match count. Everything runs over a real Unix socket in tmp_path.
"""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.inspector import (
    Command,
    InspectorClient,
    InspectorConfig,
    InspectorError,
    serve_inspector,
)
from wreath.recording import (
    ArmRegistry,
    BodyCapture,
    RecordingPolicy,
    RedactionPolicy,
)

_flight = pytest.importorskip("wreath._native._flight")

TOKEN = "capture-token-abcdef123456"


def _app() -> Wreath:
    app = Wreath()

    @app.get("/widgets/{widget_id}")
    async def widget(request, widget_id: int) -> str:
        return "ok"

    app._compile_routes()
    return app


def _recorder():
    return _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)


def _ceiling() -> RecordingPolicy:
    return RecordingPolicy(
        capture_slabs=64,
        max_capture_bytes=8 * 1024 * 1024,
        redaction=RedactionPolicy(
            header_allowlist=frozenset({"x-trace", "x-tenant"}),
            header_hash=frozenset({"x-request-id"}),
            body=BodyCapture.HASHED,
        ),
    )


def _serve(tmp_path, *, token: str | None = TOKEN, registry: bool = True):
    config = InspectorConfig(path=str(tmp_path / "wfi.sock"), capture_token=token)
    arm_registry = ArmRegistry(_ceiling()) if registry else None
    return serve_inspector(
        _recorder(), _app(), config, arm_registry=arm_registry
    )


# --- capability advertisement ------------------------------------------------


@pytest.mark.asyncio
async def test_capture_commands_advertised_only_with_token_and_registry(tmp_path) -> None:
    server = await _serve(tmp_path)
    try:
        async with InspectorClient(server.path) as client:
            caps = (await client.hello())["capabilities"]
        assert {"ARM_CAPTURE", "DISARM_CAPTURE", "CAPTURE_STATUS"} <= set(caps)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_capture_commands_absent_without_a_token(tmp_path) -> None:
    # A registry but no token: capture control stays off (read-only Inspector).
    server = await _serve(tmp_path, token=None, registry=True)
    try:
        async with InspectorClient(server.path) as client:
            caps = (await client.hello())["capabilities"]
            assert "ARM_CAPTURE" not in caps
            with pytest.raises(InspectorError, match="not enabled"):
                await client.capture_status(token="anything-at-all-16chars")
    finally:
        await server.close()


# --- authorization -----------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_requires_the_capability_token(tmp_path) -> None:
    server = await _serve(tmp_path)
    try:
        async with InspectorClient(server.path) as client:
            with pytest.raises(InspectorError, match="invalid or missing capture token"):
                await client.arm_capture(
                    token="wrong-token-1234567890",
                    redaction={"header_allowlist": ["x-trace"]},
                    expiry_seconds=60,
                )
            # A missing token is refused too (not just a wrong one).
            with pytest.raises(InspectorError, match="invalid or missing"):
                await client.call(Command.CAPTURE_STATUS, {})
    finally:
        await server.close()


# --- ceiling enforcement -----------------------------------------------------


@pytest.mark.asyncio
async def test_arm_cannot_exceed_the_startup_ceiling(tmp_path) -> None:
    server = await _serve(tmp_path)
    try:
        async with InspectorClient(server.path) as client:
            # A header the ceiling drops entirely cannot be armed.
            with pytest.raises(InspectorError, match="ceiling"):
                await client.arm_capture(
                    token=TOKEN,
                    redaction={"header_allowlist": ["x-secret"]},
                    expiry_seconds=60,
                )
            # A more revealing body than the ceiling (STRUCTURED > HASHED) is refused.
            with pytest.raises(InspectorError, match="ceiling"):
                await client.arm_capture(
                    token=TOKEN,
                    redaction={"body": "structured", "max_fields": 8, "max_depth": 4,
                               "max_body_bytes": 1024},
                    expiry_seconds=60,
                )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_arm_requires_expiry(tmp_path) -> None:
    server = await _serve(tmp_path)
    try:
        async with InspectorClient(server.path) as client:
            with pytest.raises(InspectorError, match="expiry"):
                await client.arm_capture(
                    token=TOKEN,
                    redaction={"header_allowlist": ["x-trace"]},
                    expiry_seconds=0,
                )
    finally:
        await server.close()


# --- arm / status / disarm round trip ---------------------------------------


@pytest.mark.asyncio
async def test_arm_status_disarm_round_trip(tmp_path) -> None:
    server = await _serve(tmp_path)
    try:
        async with InspectorClient(server.path) as client:
            armed = await client.arm_capture(
                token=TOKEN,
                redaction={"header_allowlist": ["x-trace"], "header_hash": ["x-request-id"]},
                budget={"slabs": 8, "slab_bytes": 4096},
                expiry_seconds=120,
                max_matches=50,
            )
            arm_id = armed["arm_id"]
            assert armed["remaining_matches"] == 50
            assert set(armed["headers"]) == {"x-trace", "x-request-id"}

            status = await client.capture_status(token=TOKEN)
            assert status["ceiling"]["capture_slabs"] == 64
            assert [a["arm_id"] for a in status["arms"]] == [arm_id]

            disarmed = await client.disarm_capture(token=TOKEN, arm_id=arm_id)
            assert disarmed["disarmed"] is True
            # A second disarm reports it was already gone.
            assert (await client.disarm_capture(token=TOKEN, arm_id=arm_id))["disarmed"] is False

            status = await client.capture_status(token=TOKEN)
            assert status["arms"] == []
    finally:
        await server.close()


# --- registry unit behavior (expiry, max matches, ceiling) ------------------


def test_registry_prunes_on_expiry_and_match_exhaustion() -> None:
    clock = [1000.0]
    registry = ArmRegistry(_ceiling(), clock=lambda: clock[0])
    from wreath.recording import CaptureBudget, CapturePolicy

    arm = registry.arm(
        CapturePolicy(
            redaction=RedactionPolicy(header_allowlist=frozenset({"x-trace"})),
            budget=CaptureBudget(slabs=1, slab_bytes=4096),
            expiry_seconds=10,
            max_matches=2,
        )
    )
    assert len(registry.active()) == 1
    # Two matches exhaust it.
    assert registry.note_match(arm.arm_id) is True
    assert registry.note_match(arm.arm_id) is False  # second match hits the cap
    assert registry.active() == []
    # A fresh arm expires with the clock.
    arm2 = registry.arm(
        CapturePolicy(
            redaction=RedactionPolicy(header_allowlist=frozenset({"x-trace"})),
            budget=CaptureBudget(slabs=1, slab_bytes=4096),
            expiry_seconds=10,
        )
    )
    assert len(registry.active()) == 1
    clock[0] += 11
    assert registry.active() == []
    assert registry.note_match(arm2.arm_id) is False


# --- CLI ---------------------------------------------------------------------


def test_capture_cli_arm_status_disarm(tmp_path, capsys) -> None:
    import asyncio
    import threading

    from wreath._cli import main as cli_main

    sock = str(tmp_path / "wfi.sock")
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(
        serve_inspector(
            _recorder(), _app(),
            InspectorConfig(path=sock, capture_token=TOKEN),
            arm_registry=ArmRegistry(_ceiling()),
        ),
        loop,
    ).result(5)
    try:
        rc = cli_main(["capture", sock, "--token", TOKEN, "arm",
                       "--allow-header", "x-trace", "--expiry", "60",
                       "--max-matches", "10"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "armed capture #1" in out and "x-trace" in out

        rc = cli_main(["capture", sock, "--token", TOKEN, "status"])
        assert rc == 0
        assert "1 active arm(s)" in capsys.readouterr().out

        rc = cli_main(["capture", sock, "--token", TOKEN, "disarm", "--arm-id", "1"])
        assert rc == 0
        assert "disarmed" in capsys.readouterr().out

        rc = cli_main(["capture", sock, "--token", TOKEN, "disarm", "--arm-id", "1"])
        assert rc == 0
        assert "no such arm" in capsys.readouterr().out

        # A wrong token exits non-zero with an error, not a traceback.
        rc = cli_main(["capture", sock, "--token", "wrong-token-1234567890", "status"])
        assert rc == 1
    finally:
        asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def test_registry_caps_concurrent_arms() -> None:
    from wreath.recording import CaptureBudget, CapturePolicy, RecordingPolicyError

    registry = ArmRegistry(_ceiling(), max_arms=2)

    def _arm():
        return registry.arm(
            CapturePolicy(
                redaction=RedactionPolicy(header_allowlist=frozenset({"x-trace"})),
                budget=CaptureBudget(slabs=1, slab_bytes=4096),
                expiry_seconds=60,
            )
        )

    _arm()
    _arm()
    with pytest.raises(RecordingPolicyError, match="too many"):
        _arm()
