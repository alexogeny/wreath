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
    SegmentKind,
    TransportRecording,
    TransportSegment,
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


def test_non_data_segments_never_become_request_bytes() -> None:
    recording = TransportRecording(
        (
            TransportSegment(0, int(SegmentKind.DATA), GET),
            TransportSegment(1, int(SegmentKind.RESET), b"not request bytes"),
        )
    )

    assert recorded_request(recording).path == "/llamas/7"


def test_an_empty_content_length_is_zero() -> None:
    raw = b"GET /llamas/7 HTTP/1.1\r\nhost: x\r\ncontent-length:\r\n\r\n"

    assert recorded_request(_recording(raw)).body == b""


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
async def test_generated_paths_preserve_query_presence_exactly() -> None:
    queried = await generate_test(_app(), _recording(GET), target="herd.app:app")
    plain = await generate_test(
        _app(),
        _recording(b"GET /llamas/7 HTTP/1.1\r\nhost: herd.example\r\n\r\n"),
        target="herd.app:app",
    )

    assert "'/llamas/7?include=treks'" in queried
    assert "'/llamas/7'" in plain
    assert "'/llamas/7?'" not in plain


@pytest.mark.asyncio
async def test_generation_replays_the_query_when_observing_the_response() -> None:
    app = Wreath()

    @app.get("/echo")
    async def echo(request) -> dict:
        return {"query": request.query_string.decode("latin-1")}

    recording = _recording(
        b"GET /echo?mode=deep HTTP/1.1\r\nhost: example.test\r\n\r\n"
    )
    source = await generate_test(app, recording, target="echo.app:app")

    response_assertion = source.split("assert response.body ==", 1)[1]
    assert '"query":"mode=deep"' in response_assertion


@pytest.mark.asyncio
async def test_generated_headers_exclude_transport_framing() -> None:
    source = await generate_test(
        _app(),
        _recording(
            b"GET /llamas/7 HTTP/1.1\r\n"
            b"host: herd.example\r\n"
            b"content-length: 0\r\n"
            b"connection: close\r\n"
            b"accept: application/json\r\n\r\n"
        ),
        target="herd.app:app",
    )

    request_headers = source.split("headers=", 1)[1].split(",\n", 1)[0]
    assert "accept" in request_headers
    assert "host" not in request_headers
    assert "content-length" not in request_headers
    assert "connection" not in request_headers


@pytest.mark.asyncio
async def test_generation_does_not_replay_the_recorded_host_header() -> None:
    app = Wreath()

    @app.get("/host")
    async def host(request) -> dict:
        return {"host": request.header("host")}

    recording = _recording(
        b"GET /host HTTP/1.1\r\nhost: recorded.example\r\nconnection: close\r\n\r\n"
    )
    source = await generate_test(app, recording, target="host.app:app")

    assert "recorded.example" not in source.split("assert response.body ==", 1)[1]


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
        exec(compile(source, "generated_test.py", "exec"), namespace)  # noqa: S102 - executing the generated test is what this asserts
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
async def test_an_empty_body_does_not_emit_a_content_argument() -> None:
    source = await generate_test(_app(), _recording(GET), target="herd.app:app")

    request_call = source.split("response = await client.request(", 1)[1].split(")", 1)[0]
    assert "content=" not in request_call


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
async def test_generated_test_without_origin_does_not_invent_one() -> None:
    source = await generate_test(_app(), _recording(GET), target="herd.app:app")

    assert "Captured from" not in source


@pytest.mark.asyncio
async def test_a_module_only_target_imports_the_module() -> None:
    source = await generate_test(_app(), _recording(GET), target="herd.app")
    assert "import herd.app" in source
    assert "TestClient(herd.app.app)" in source


@pytest.mark.asyncio
async def test_explicit_target_uses_the_imported_attribute_directly() -> None:
    source = await generate_test(_app(), _recording(GET), target="herd.app:application")

    assert "from herd.app import application" in source
    assert "TestClient(application)" in source


@pytest.mark.asyncio
async def test_a_malformed_generated_test_target_is_refused() -> None:
    with pytest.raises(ValueError, match="module:attribute"):
        await generate_test(_app(), _recording(GET), target="herd.app:bad:name")


# --- a connection that carried more than one request (design 22 item 18) -----


def test_a_pipelined_recording_is_refused_rather_than_half_tested():
    """Two requests on one keep-alive connection must not silently become one.

    `recorded_request` joins every DATA segment and parses one request, so the
    second request's bytes were dropped past `content-length` without a word --
    generating a regression test that covers half of what was recorded. Refused
    for the same reason a chunked body is.
    """
    with pytest.raises(ReplayError) as caught:
        recorded_request(_recording(GET + POST))

    message = str(caught.value)
    assert "more than one request" in message
    assert "wreath replay transport" in message


def test_the_dropped_bytes_are_counted_in_the_refusal():
    with pytest.raises(ReplayError) as caught:
        recorded_request(_recording(POST + GET))

    assert f"{len(GET)} bytes past" in str(caught.value)


def test_pipelining_is_caught_when_split_across_reads():
    """A real capture splits mid-request; the join must not hide the second one."""
    joined = GET + POST
    with pytest.raises(ReplayError):
        recorded_request(_recording(joined[:20], joined[20:]))


def test_a_single_request_with_a_trailing_newline_is_still_accepted():
    """Not everything past the body is another request."""
    request = recorded_request(_recording(POST + b"\r\n"))

    assert request.method == "POST"
    assert request.body == b'{"name": "Bea"}\n'


# --- every refusal it advertises, and nothing it does not --------------------
#
# `recorded_request` refuses rather than guesses, which only means something if
# every refusal is reachable. The table below is checked against the number of
# `raise ReplayError` sites in the function itself, so adding a refusal without
# a case for it turns this red instead of shipping an unreachable one.

REFUSALS = {
    "no request bytes at all": (b"", "no request"),
    "a head with no terminator": (
        b"GET /llamas HTTP/1.1\r\nhost: x\r\n",
        "incomplete",
    ),
    "a request line missing its version": (
        b"GET /llamas\r\nhost: x\r\n\r\n",
        "malformed",
    ),
    "a header line with no colon": (
        b"GET /llamas HTTP/1.1\r\nhost: x\r\njust-a-word\r\n\r\n",
        "malformed header",
    ),
    "a chunked body": (
        b"POST /llamas HTTP/1.1\r\nhost: x\r\ntransfer-encoding: chunked\r\n\r\n0\r\n\r\n",
        "chunked",
    ),
    "a body shorter than its content-length": (
        b"POST /llamas HTTP/1.1\r\nhost: x\r\ncontent-length: 99\r\n\r\n{}",
        "incomplete",
    ),
    "two requests on one connection": (GET + POST, "more than one request"),
}


@pytest.mark.parametrize(("case", "payload"), sorted(REFUSALS.items()))
def test_every_advertised_refusal_actually_fires(case: str, payload) -> None:
    raw, match = payload
    with pytest.raises(ReplayError, match=match):
        recorded_request(_recording(raw))


def test_the_refusal_table_covers_every_refusal_in_the_function() -> None:
    """A coverage gate that cannot drift, because it reads the code.

    Three of this repository's seven "checks with nothing to check" were
    refusals nobody had ever seen fire. Counting the `raise` sites in
    `recorded_request` and comparing them with the cases above means a new
    refusal ships with a case for it or turns this red.
    """
    import inspect

    from wreath.replay import recorded_request as function

    source = inspect.getsource(function)
    assert source.count("raise ReplayError(") == len(REFUSALS), (
        "recorded_request has a refusal with no case in REFUSALS (or one case "
        "too many); every refusal must be shown to fire"
    )


def test_a_recording_it_does_not_refuse_is_read_correctly() -> None:
    """The control the table needs. A `recorded_request` that refused
    everything would satisfy all seven cases above."""
    request = recorded_request(_recording(POST))
    assert (request.method, request.path) == ("POST", "/llamas")
    assert request.body == b'{"name": "Bea"}\n'


# --- the headers the generator drops, and the ones it must not ---------------


HOP_BY_HOP = (
    b"GET /llamas/7 HTTP/1.1\r\n"
    b"host: herd.example\r\n"
    b"content-length: 0\r\n"
    b"connection: keep-alive\r\n"
    b"accept: application/json\r\n"
    b"x-request-id: 9f51\r\n"
    b"\r\n"
)


@pytest.mark.asyncio
async def test_transport_headers_are_dropped_from_the_generated_call() -> None:
    """`host`, `content-length` and `connection` are the test client's business.

    Carrying them into the generated call would assert on transport rather than
    on behaviour: `TestClient` sets its own `host`, computes its own framing,
    and a pinned `connection: keep-alive` would make the test fail the day the
    client changed how it pools. The rule is in `wreath.replay`'s docstring and
    in the subsystem manifest; this is where it is enforced.
    """
    source = await generate_test(_app(), _recording(HOP_BY_HOP), target="herd.app:app")
    assert "'host'" not in source
    assert "'content-length'" not in source
    assert "'connection'" not in source


@pytest.mark.asyncio
async def test_every_other_header_survives_into_the_generated_call() -> None:
    """The other half, without which the test above passes for a generator that
    drops every header."""
    source = await generate_test(_app(), _recording(HOP_BY_HOP), target="herd.app:app")
    assert "'accept': 'application/json'" in source
    assert "'x-request-id': '9f51'" in source


@pytest.mark.asyncio
async def test_a_header_named_like_a_dropped_one_is_kept() -> None:
    """The drop is by exact name. `x-connection` is not `connection`, and a
    prefix or substring match would quietly delete a caller's header."""
    raw = (
        b"GET /llamas/7 HTTP/1.1\r\nhost: x\r\n"
        b"x-connection: pooled\r\nhost-region: eu\r\n\r\n"
    )
    source = await generate_test(_app(), _recording(raw), target="herd.app:app")
    assert "'x-connection': 'pooled'" in source
    assert "'host-region': 'eu'" in source


# --- record -> generate -> run -> the same observation -----------------------


async def _run_generated(source: str, app_factory) -> None:
    """Compile and run a generated module against a module-level app."""
    import sys
    import types

    module = types.ModuleType("herd_roundtrip_app")
    module.app = app_factory()
    sys.modules["herd_roundtrip_app"] = module
    try:
        namespace: dict = {}
        exec(compile(source, "generated_test.py", "exec"), namespace)  # noqa: S102 - executing the generated test is what this asserts
        test = next(value for name, value in namespace.items() if name.startswith("test_"))
        await test()
    finally:
        del sys.modules["herd_roundtrip_app"]


@pytest.mark.asyncio
async def test_the_generated_test_asserts_what_the_client_actually_observes() -> None:
    """The round trip, closed: record, generate, run, and the same answer.

    Not "it produces a file that imports". The generated assertion has to be the
    observation a direct `TestClient` call makes for the same request, or the
    tool has transcribed something other than what happened -- which is the one
    failure mode that would make every generated regression test worthless
    while all of them passed.
    """
    from wreath.testing import TestClient

    async with TestClient(_app()) as client:
        direct = await client.request("GET", "/llamas/7?include=treks")

    source = await generate_test(_app(), _recording(GET), target="herd_roundtrip_app:app")
    assert f"assert response.status == {direct.status}" in source
    assert f"assert response.body == {direct.body!r}" in source
    await _run_generated(source, _app)


@pytest.mark.asyncio
async def test_the_generated_assertion_fails_when_the_behaviour_changes() -> None:
    """Proof the assertion is load-bearing rather than decorative.

    A characterisation test that passes against a *different* application is not
    a characterisation of anything. Generated against one app and run against
    one that answers differently, it must fail -- and this is the only way to
    know the generated `assert` is comparing something real.
    """
    source = await generate_test(_app(), _recording(GET), target="herd_roundtrip_app:app")

    def changed() -> Wreath:
        app = Wreath()

        @app.get("/llamas/{llama_id}")
        async def llama(request, llama_id: int) -> dict:
            return {"id": llama_id, "name": "Someone Else"}

        return app

    with pytest.raises(AssertionError):
        await _run_generated(source, changed)


@pytest.mark.asyncio
async def test_a_generated_test_is_stable_across_regenerations() -> None:
    """Two generations of the same recording produce the same source.

    A generator whose output varied -- header order, dict ordering, a timestamp
    -- would show up as a diff on every regeneration, and a file that always
    diffs is a file nobody reads.
    """
    first = await generate_test(_app(), _recording(POST), target="herd.app:app")
    second = await generate_test(_app(), _recording(POST), target="herd.app:app")
    assert first == second


@pytest.mark.asyncio
async def test_the_generated_source_is_valid_python_and_says_it_is_generated() -> None:
    import ast

    source = await generate_test(_app(), _recording(POST), target="herd.app:app")
    ast.parse(source)  # a file that does not parse is not a test
    assert source.splitlines()[0].startswith("# Generated by")
    assert "Re-generate rather than edit" in source
