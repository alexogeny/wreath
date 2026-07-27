"""A recorded request, turned into a runnable regression test.

The gap this closes: an incident produces a recording, and a recording is only
useful while someone is looking at it. Turning one into a test is twenty minutes
of transcribing headers by hand, so it usually does not happen, and the same bug
comes back.

Wreath records the request, owns the pipeline that served it, and ships the test
client that can drive it again -- so the transcription is a function. What comes
out is a *characterisation* test: it asserts what this request does today. Run it
against the broken build and it encodes the bug; run it after the fix and it
locks the fix in. The tool does not know which you meant, and says so.
"""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.replay import (
    ReplayError,
    generate_test,
    record_transport_segments,
    recorded_request,
)


def _app() -> Wreath:
    app = Wreath()

    @app.get("/llamas/{llama_id}")
    async def llama(request, llama_id: int) -> dict:
        return {"id": llama_id, "name": "Bea"}

    @app.post("/llamas")
    async def create(request) -> dict:
        return {"created": True}

    return app


def _recording(*chunks: bytes):
    return record_transport_segments([*chunks])


GET = (
    b"GET /llamas/7?include=treks HTTP/1.1\r\n"
    b"host: herd.example\r\n"
    b"accept: application/json\r\n"
    b"\r\n"
)
POST = (
    b"POST /llamas HTTP/1.1\r\n"
    b"host: herd.example\r\n"
    b"content-type: application/json\r\n"
    b"content-length: 16\r\n"
    b"\r\n"
    b'{"name": "Bea"}\n'
)


# --- reading the request back out ---------------------------------------------


def test_the_request_line_survives_the_recording() -> None:
    request = recorded_request(_recording(GET))
    assert request.method == "GET"
    assert request.path == "/llamas/7"
    assert request.query_string == b"include=treks"
    assert request.body == b""


def test_headers_come_back_lowercased_and_in_order() -> None:
    request = recorded_request(_recording(GET))
    assert request.headers == (
        (b"host", b"herd.example"),
        (b"accept", b"application/json"),
    )


def test_a_body_is_read_to_its_content_length() -> None:
    request = recorded_request(_recording(POST))
    assert request.method == "POST"
    assert request.body == b'{"name": "Bea"}\n'


def test_a_request_split_across_segments_is_reassembled() -> None:
    """A real connection does not deliver a request in one read."""
    request = recorded_request(_recording(POST[:20], POST[20:45], POST[45:]))
    assert request.path == "/llamas"
    assert request.body == b'{"name": "Bea"}\n'


def test_a_recording_with_no_request_is_refused() -> None:
    with pytest.raises(ReplayError, match="no request"):
        recorded_request(_recording(b""))


def test_a_truncated_request_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ReplayError, match="incomplete"):
        recorded_request(_recording(b"GET /llamas HTTP/1.1\r\nhost: x\r\n"))


def test_a_body_shorter_than_its_content_length_is_refused() -> None:
    truncated = (
        b"POST /llamas HTTP/1.1\r\nhost: x\r\ncontent-length: 99\r\n\r\n{}"
    )
    with pytest.raises(ReplayError, match="incomplete"):
        recorded_request(_recording(truncated))


def test_chunked_encoding_is_refused_by_name() -> None:
    """Better a clear refusal than a test asserting a body we mis-decoded."""
    chunked = (
        b"POST /llamas HTTP/1.1\r\nhost: x\r\n"
        b"transfer-encoding: chunked\r\n\r\n0\r\n\r\n"
    )
    with pytest.raises(ReplayError, match="chunked"):
        recorded_request(_recording(chunked))


# --- generating the test ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_generated_test_reproduces_the_request() -> None:
    source = await generate_test(_app(), _recording(GET), target="herd.app:app")

    assert "from wreath.testing import TestClient" in source
    assert "from herd.app import app" in source
    assert "'GET'" in source and "'/llamas/7?include=treks'" in source
    assert "'accept': 'application/json'" in source
    assert "assert response.status == 200" in source


@pytest.mark.asyncio
async def test_the_generated_test_asserts_the_observed_body() -> None:
    source = await generate_test(_app(), _recording(GET), target="herd.app:app")
    assert b'"name":"Bea"' in source.encode() or '\\"name\\":\\"Bea\\"' in source


@pytest.mark.asyncio
async def test_the_generated_test_is_actually_runnable() -> None:
    """The whole promise. A test that does not run is a transcription exercise."""
    import sys
    import types

    module = types.ModuleType("herd_generated_app")
    module.app = _app()
    sys.modules["herd_generated_app"] = module
    try:
        source = await generate_test(
            _app(), _recording(GET), target="herd_generated_app:app"
        )
        namespace: dict = {}
        exec(compile(source, "generated_test.py", "exec"), namespace)
        test = next(
            value for name, value in namespace.items() if name.startswith("test_")
        )
        await test()                       # must pass, not merely import
    finally:
        del sys.modules["herd_generated_app"]


@pytest.mark.asyncio
async def test_a_post_body_is_carried_into_the_generated_test() -> None:
    source = await generate_test(_app(), _recording(POST), target="herd.app:app")
    assert "'POST'" in source
    assert "content=" in source
    assert "Bea" in source


@pytest.mark.asyncio
async def test_the_name_is_derived_from_the_request() -> None:
    source = await generate_test(_app(), _recording(GET), target="herd.app:app")
    assert "async def test_get_llamas_7(" in source


@pytest.mark.asyncio
async def test_an_explicit_name_wins() -> None:
    source = await generate_test(
        _app(), _recording(GET), target="herd.app:app", name="test_the_incident"
    )
    assert "async def test_the_incident(" in source


@pytest.mark.asyncio
async def test_the_generated_test_says_where_it_came_from() -> None:
    """Someone reading it in six months needs to know it was not hand-written."""
    source = await generate_test(
        _app(), _recording(GET), target="herd.app:app", origin="herd-incident.wtr1"
    )
    assert "herd-incident.wtr1" in source
    assert "wreath replay to-test" in source


@pytest.mark.asyncio
async def test_a_module_only_target_imports_the_module() -> None:
    source = await generate_test(_app(), _recording(GET), target="herd.app")
    assert "import herd.app" in source
