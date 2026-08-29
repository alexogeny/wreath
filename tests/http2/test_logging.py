from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath.server import ServerConfig

from . import support
from .conftest import FakeTransport, _settle

_flight = pytest.importorskip("wreath._native._flight")
try:
    from wreath._native._server import Http2Protocol
except ImportError:  # pragma: no cover -- the native h2 build is optional
    Http2Protocol = None

pytestmark = pytest.mark.skipif(Http2Protocol is None, reason="native h2 not built")


async def _drive(recorder: Any, app: Any, streams: tuple[int, ...] = (1,)) -> None:
    loop = asyncio.get_event_loop()
    protocol = Http2Protocol(app, ServerConfig(protocols=("h2",)), loop, set(), recorder=recorder)
    protocol.connection_made(FakeTransport())
    await _settle()
    protocol.data_received(support.PREFACE)
    protocol.data_received(support.encode_settings({}))
    await _settle()
    for sid in streams:
        protocol.data_received(
            support.build_headers_frame(
                sid,
                support.request_headers(method=b"GET", path=b"/x", authority=b"example.com"),
            )
        )
        await _settle()


def _seen_flight() -> tuple[list[Any], Any]:
    """An ASGI app that records what the protocol seeded into its scope."""
    seen: list[Any] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        seen.append(scope.get("_wreath_flight"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return seen, app


@pytest.mark.asyncio
async def test_the_protocol_seeds_the_recorder_request_id() -> None:
    seen, app = _seen_flight()
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    await _drive(rec, app)
    assert len(seen) == 1
    # An int, not None: the id is what lets Python open a log scope, and the
    # completion path still reads it as "unattributed" because it is not a
    # 2-tuple.
    assert isinstance(seen[0], int)
    assert seen[0] != 0


@pytest.mark.asyncio
async def test_the_seeded_id_matches_the_completion_cell() -> None:
    seen, app = _seen_flight()
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    await _drive(rec, app)
    blob = rec.drain()
    completion = fs.CompletionCell.decode(blob[: fs.CELL_SIZE])
    assert completion.protocol is fs.Protocol.HTTP2
    assert seen[0] == completion.request_id


@pytest.mark.asyncio
async def test_an_off_recorder_seeds_nothing_usable() -> None:
    seen, app = _seen_flight()
    rec = _flight.Recorder(_flight.MODE_OFF)
    await _drive(rec, app)
    assert not isinstance(seen[0], int) or seen[0] == 0


@pytest.mark.asyncio
async def test_a_record_emitted_during_an_h2_request_carries_its_id() -> None:
    import wreath

    app = wreath.Wreath()
    site = log.event("h2.during", "during {v}", level=log.WARN, fields=(log.field("v", int),))

    @app.get("/x")
    async def handler(request: wreath.Request) -> wreath.Response:
        site(7)
        return wreath.response.TextResponse("ok")

    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    with log.testing_runtime() as records:
        await _drive(rec, app)
    assert records, "no record was emitted during the HTTP/2 request"
    assert records[0].request_id != 0

    completions = [
        fs.CompletionCell.decode(blob)
        for blob in (
            rec.drain()[i : i + fs.CELL_SIZE]
            for i in range(0, len(rec.drain() or b""), fs.CELL_SIZE)
        )
        if blob and blob[1] == fs.EventKind.COMPLETION
    ]
    if completions:
        assert records[0].request_id == completions[0].request_id
