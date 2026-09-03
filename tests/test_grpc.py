from __future__ import annotations

import pytest

import wreath.grpc as grpc_module
from wreath.exceptions import Forbidden, NotFound, TooManyRequests, UnprocessableEntity
from wreath.grpc import (
    DEFAULT_MAX_MESSAGE_BYTES,
    GrpcError,
    GrpcService,
    Status,
    Unframer,
    frame_message,
    negotiated_encoding,
    parse_timeout,
    percent_encode,
    reply_encoding,
    status_for,
)
from wreath.protobuf import field, message


def _drive(
    app,
    path: str,
    body: bytes,
    *,
    http_version: str = "2",
    content_type: str | None = "application/grpc+proto",
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> list[dict]:
    """Run one request through the ASGI app and return the messages it sent.

    A synthetic HTTP/2 scope, so the dispatch path is exercised without a
    socket, a certificate or the `network` mark.
    """
    import asyncio

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": http_version,
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "https",
        "headers": [
            *([] if content_type is None else [(b"content-type", content_type.encode())]),
            (b"te", b"trailers"),
            *extra_headers,
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 443),
    }
    incoming = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict] = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _grpc_status_headers(sent: list[dict]) -> dict[bytes, bytes]:
    """Return status metadata from an ordinary trailer or Trailers-Only head."""
    for event in reversed(sent):
        headers = dict(event.get("headers", ()))
        if b"grpc-status" in headers:
            return headers
    raise AssertionError("gRPC response carried no grpc-status")


@message
class Ping:
    text: str = field(1)


@message
class Pong:
    text: str = field(1)


class TestFraming:
    def test_the_prefix_is_a_flag_byte_and_a_big_endian_length(self):
        assert frame_message(b"abc") == b"\x00\x00\x00\x00\x03abc"

    def test_an_empty_message_still_carries_a_prefix(self):
        assert frame_message(b"") == b"\x00\x00\x00\x00\x00"

    def test_a_frame_round_trips_through_the_unframer(self):
        unframer = Unframer()
        assert unframer.feed(frame_message(b"hello")) == [b"hello"]

    def test_several_messages_in_one_chunk_all_emerge(self):
        chunk = frame_message(b"one") + frame_message(b"two") + frame_message(b"three")
        assert Unframer().feed(chunk) == [b"one", b"two", b"three"]

    def test_a_message_split_across_chunks_is_reassembled(self):
        whole = frame_message(b"abcdefgh")
        unframer = Unframer()
        assert unframer.feed(whole[:3]) == []
        assert unframer.feed(whole[3:7]) == []
        assert unframer.feed(whole[7:]) == [b"abcdefgh"]

    def test_finishing_a_complete_stream_does_not_raise(self):
        unframer = Unframer()
        unframer.feed(frame_message(b"abc"))
        unframer.finish()

    def test_a_partial_trailing_message_is_refused_not_dropped(self):
        unframer = Unframer()
        unframer.feed(frame_message(b"abc")[:-1])
        with pytest.raises(GrpcError) as caught:
            unframer.finish()
        assert caught.value.status is Status.INTERNAL

    def test_a_length_beyond_the_limit_is_refused_before_allocating(self):
        unframer = Unframer(max_message_bytes=16)
        oversized = b"\x00" + (999_999).to_bytes(4, "big")
        with pytest.raises(GrpcError) as caught:
            unframer.feed(oversized)
        assert caught.value.status is Status.RESOURCE_EXHAUSTED
        assert "999999" in str(caught.value) or "999999" in caught.value.message

    def test_the_default_limit_is_the_four_mebibytes_clients_expect(self):
        assert DEFAULT_MAX_MESSAGE_BYTES == 4 * 1024 * 1024

    def test_a_compressed_message_on_an_identity_call_is_refused_by_name(self):
        unframer = Unframer()
        with pytest.raises(GrpcError) as caught:
            unframer.feed(b"\x01\x00\x00\x00\x03abc")
        assert caught.value.status is Status.INTERNAL
        assert "identity" in caught.value.message

    def test_an_identity_unframer_does_not_allocate_a_gzip_workspace(self, monkeypatch):
        def unexpected_workspace():
            raise AssertionError("identity must not create a gzip decoder")

        monkeypatch.setattr(grpc_module, "_gzip_decoder_new", unexpected_workspace)
        Unframer(encoding="identity")


class TestTimeouts:
    @pytest.mark.parametrize(
        ("value", "seconds"),
        [
            ("1H", 3600.0),
            ("2M", 120.0),
            ("5S", 5.0),
            ("100m", 0.1),
            ("250u", 0.00025),
            ("1000n", 1e-6),
        ],
    )
    def test_every_unit_in_the_specification_parses(self, value, seconds):
        assert parse_timeout(value) == pytest.approx(seconds)

    @pytest.mark.parametrize("value", ["", "5", "S", "-1S", "5X", "1.5S", "999999999S"])
    def test_a_malformed_timeout_is_refused_rather_than_ignored(self, value):
        with pytest.raises(GrpcError) as caught:
            parse_timeout(value)
        assert caught.value.status is Status.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_streaming_without_a_deadline_does_not_arm_a_timer(self, monkeypatch):
        import asyncio

        def unexpected_timeout(_delay):
            raise AssertionError("an absent deadline must not arm a timer")

        async def no_results():
            if False:
                yield Pong(text="unused")

        monkeypatch.setattr(asyncio, "timeout", unexpected_timeout)
        assert [item async for item in grpc_module._frames(no_results(), None, "identity")] == []

    @pytest.mark.asyncio
    async def test_streaming_deadline_covers_waiting_for_the_next_result(self):
        import asyncio

        async def delayed_result():
            await asyncio.sleep(0.01)
            yield Pong(text="late")

        with pytest.raises(GrpcError) as caught:
            [
                item
                async for item in grpc_module._frames(delayed_result(), 0.001, "identity")
            ]
        assert caught.value.status is Status.DEADLINE_EXCEEDED


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (Forbidden(), Status.PERMISSION_DENIED),
            (NotFound(), Status.NOT_FOUND),
            (UnprocessableEntity(), Status.INVALID_ARGUMENT),
            (TooManyRequests(), Status.RESOURCE_EXHAUSTED),
            (TimeoutError(), Status.DEADLINE_EXCEEDED),
        ],
    )
    def test_wreath_refusals_map_to_the_status_a_client_understands(self, exc, expected):
        assert status_for(exc)[0] is expected

    def test_an_unrecognised_exception_is_unknown_not_something_retryable(self):
        status, _ = status_for(RuntimeError("boom"))
        assert status is Status.UNKNOWN

    def test_an_explicit_grpc_error_passes_through_untouched(self):
        status, detail = status_for(GrpcError(Status.ALREADY_EXISTS, "twice"))
        assert (status, detail) == (Status.ALREADY_EXISTS, "twice")

    def test_ok_is_zero_because_success_still_carries_a_status(self):
        assert int(Status.OK) == 0


class TestGrpcMessageEncoding:
    def test_ordinary_text_is_left_alone(self):
        assert percent_encode("not for you") == "not for you"

    def test_a_newline_cannot_forge_a_header(self):
        assert "\n" not in percent_encode("a\nb")
        assert percent_encode("a\nb") == "a%0Ab"

    def test_non_ascii_is_encoded_as_utf8_bytes(self):
        assert percent_encode("é") == "%C3%A9"

    def test_a_percent_is_itself_escaped_so_decoding_is_unambiguous(self):
        assert percent_encode("100%") == "100%25"


class TestServiceDeclaration:
    def test_a_non_message_request_type_is_refused_at_declaration(self):
        service = GrpcService("t.S")
        with pytest.raises(TypeError, match="not a @message"):

            @service.unary(request=dict, response=Pong)
            async def M(request, msg):  # pragma: no cover - never registered
                ...

    def test_a_service_name_that_looks_like_a_path_is_refused(self):
        with pytest.raises(ValueError, match="bare protobuf name"):
            GrpcService("/t.S")

    def test_every_method_becomes_one_post_route_at_the_grpc_path(self):
        service = GrpcService("camera.Tracker")

        @service.unary(request=Ping, response=Pong)
        async def GetPosition(request, msg):
            """Doc."""
            return Pong(text="x")

        routes = list(service.router().routes)
        assert len(routes) == 1
        assert routes[0].path == "/camera.Tracker/GetPosition"
        assert routes[0].methods == ("POST",)

    def test_route_metadata_reaches_the_definition_unchanged(self):
        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong, permissions=("track:read",))
        async def M(request, msg):
            """Doc."""
            return Pong(text="x")

        route = next(iter(service.router().routes))
        assert any("track:read" in check.values for check in route.requirement.permission_checks)

    def test_an_auth_decorator_on_the_method_survives_the_wrapper(self):
        from wreath.authorization import roles

        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong)
        @roles("ranger")
        async def M(request, msg):
            """Doc."""
            return Pong(text="x")

        # `RouteDefinition.requirement` carries only the router-level and
        # `permissions=` parts; the application merges the endpoint's own
        # marker at compile time (`app.py`'s
        # `merge_requirements(route.requirement, requirement_for(route.endpoint))`).
        # Asserting the merged result is asserting what actually enforces.
        from wreath._auth.requirements import merge_requirements, requirement_for

        route = next(iter(service.router().routes))
        merged = merge_requirements(route.requirement, requirement_for(route.endpoint))
        assert merged.authenticated is True
        assert any("ranger" in check.values for check in merged.role_checks)

    def test_grpc_dispatch_over_asgi_answers_a_framed_reply_and_a_status(self):
        from wreath import Wreath
        from wreath.grpc import frame_message
        from wreath.protobuf import encode

        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong)
        async def M(request, msg):
            """Doc."""
            return Pong(text=msg.text.upper())

        app = Wreath()
        app.include_router(service.router())
        sent = _drive(app, "/t.S/M", frame_message(encode(Ping(text="hi"))))

        start = sent[0]
        assert start["status"] == 200
        assert start["trailers"] is True
        assert (b"content-type", b"application/grpc+proto") in start["headers"]
        assert sent[-1]["type"] == "http.response.trailers"
        assert (b"grpc-status", b"0") in sent[-1]["headers"]
        payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert payload == frame_message(encode(Pong(text="HI")))

    def test_a_call_over_http1_is_refused_naming_the_transport(self):
        from wreath import Wreath
        from wreath.grpc import frame_message
        from wreath.protobuf import encode

        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong)
        async def M(request, msg):
            """Doc."""
            return Pong(text="x")  # pragma: no cover - refused before this runs

        app = Wreath()
        app.include_router(service.router())
        sent = _drive(app, "/t.S/M", frame_message(encode(Ping(text="hi"))), http_version="1.1")
        trailers = _grpc_status_headers(sent)
        assert trailers[b"grpc-status"] == str(int(Status.UNIMPLEMENTED)).encode()
        assert b"HTTP/2" in trailers[b"grpc-message"]

    def test_a_wrong_content_type_is_refused(self):
        from wreath import Wreath

        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong)
        async def M(request, msg):
            """Doc."""
            return Pong(text="x")  # pragma: no cover - refused before this runs

        app = Wreath()
        app.include_router(service.router())
        sent = _drive(app, "/t.S/M", b"", content_type="application/json")
        trailers = _grpc_status_headers(sent)
        assert trailers[b"grpc-status"] == str(int(Status.INTERNAL)).encode()

    def test_a_unary_call_carrying_two_messages_is_refused(self):
        from wreath import Wreath
        from wreath.grpc import frame_message
        from wreath.protobuf import encode

        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong)
        async def M(request, msg):
            """Doc."""
            return Pong(text="x")  # pragma: no cover - refused before this runs

        app = Wreath()
        app.include_router(service.router())
        body = frame_message(encode(Ping(text="a"))) + frame_message(encode(Ping(text="b")))
        sent = _drive(app, "/t.S/M", body)
        trailers = _grpc_status_headers(sent)
        assert trailers[b"grpc-status"] == str(int(Status.INVALID_ARGUMENT)).encode()

    def _echo_app(self):
        from wreath import Wreath

        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong)
        async def M(request, msg):
            """Doc."""
            return Pong(text="x")

        app = Wreath()
        app.include_router(service.router())
        return app

    def test_a_request_with_no_content_type_at_all_is_refused(self):
        sent = _drive(self._echo_app(), "/t.S/M", b"", content_type=None)
        trailers = _grpc_status_headers(sent)
        assert trailers[b"grpc-status"] == str(int(Status.INTERNAL)).encode()

    def test_an_unsupported_request_encoding_is_refused_by_name(self):
        from wreath.grpc import frame_message
        from wreath.protobuf import encode

        sent = _drive(
            self._echo_app(),
            "/t.S/M",
            frame_message(encode(Ping(text="a"))),
            extra_headers=((b"grpc-encoding", b"deflate"),),
        )
        trailers = _grpc_status_headers(sent)
        assert trailers[b"grpc-status"] == str(int(Status.UNIMPLEMENTED)).encode()
        assert b"deflate" in trailers[b"grpc-message"]
        assert (b"grpc-accept-encoding", b"identity,gzip") in sent[0]["headers"]

    @pytest.mark.parametrize(
        "headers",
        [
            ((b"content-type", b"application/grpc+proto"),),
            ((b"te", b"trailers"),),
            ((b"grpc-encoding", b"identity"), (b"grpc-encoding", b"gzip")),
            ((b"grpc-timeout", b"1S"), (b"grpc-timeout", b"2S")),
        ],
    )
    def test_duplicate_singleton_transport_headers_are_refused(self, headers):
        from wreath.protobuf import encode

        sent = _drive(
            self._echo_app(),
            "/t.S/M",
            frame_message(encode(Ping(text="a"))),
            extra_headers=headers,
        )

        trailers = _grpc_status_headers(sent)
        assert trailers[b"grpc-status"] == str(int(Status.INVALID_ARGUMENT)).encode()
        assert b"occurs more than once" in trailers[b"grpc-message"]

    def test_a_unary_call_carrying_no_message_is_refused(self):
        sent = _drive(self._echo_app(), "/t.S/M", b"")
        trailers = _grpc_status_headers(sent)
        assert trailers[b"grpc-status"] == str(int(Status.INVALID_ARGUMENT)).encode()
        assert b"none" in trailers[b"grpc-message"]

    def test_grpc_routes_stay_out_of_the_openapi_document(self):
        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong)
        async def M(request, msg):
            """Doc."""
            return Pong(text="x")

        assert next(iter(service.router().routes)).include_in_schema is False


class TestCompression:
    """`grpc-encoding: gzip` in both directions.

    gRPC compresses per *message*, not per body: the flag byte in each five-byte
    prefix says whether that message is compressed with the coding the call
    declared. So there are two independent negotiations -- what the client sent
    (`grpc-encoding`) and what it will accept back (`grpc-accept-encoding`) --
    and a refusal at each of the two layers a compressed message passes through.
    """

    def _gzip_frame(self, payload: bytes) -> bytes:
        from wreath.compression import gzip_compress

        return frame_message(gzip_compress(payload), compressed=True)

    def test_a_gzip_message_is_decompressed_by_the_unframer(self):
        unframer = Unframer(encoding="gzip")
        assert unframer.feed(self._gzip_frame(b"hello" * 20)) == [b"hello" * 20]

    def test_an_uncompressed_message_is_still_read_on_a_gzip_call(self):
        unframer = Unframer(encoding="gzip")
        assert unframer.feed(frame_message(b"plain")) == [b"plain"]

    def test_a_gzip_message_exceeding_the_limit_is_refused_after_decoding(self):
        bomb = self._gzip_frame(b"\x00" * 2_000_000)
        assert len(bomb) < 8192, "the wire length must stay inside the ceiling"
        unframer = Unframer(max_message_bytes=8192, encoding="gzip")
        with pytest.raises(GrpcError) as caught:
            unframer.feed(bomb)
        assert caught.value.status is Status.RESOURCE_EXHAUSTED
        assert "decompress" in caught.value.message

    def test_a_corrupt_gzip_message_is_a_status_not_a_zlib_error(self):
        unframer = Unframer(encoding="gzip")
        with pytest.raises(GrpcError) as caught:
            unframer.feed(frame_message(b"not gzip at all", compressed=True))
        assert caught.value.status is Status.INTERNAL

    def test_the_two_compression_refusals_do_not_share_a_message(self):
        with pytest.raises(GrpcError) as flagged:
            Unframer().feed(b"\x01\x00\x00\x00\x03abc")
        with pytest.raises(GrpcError) as unsupported:
            negotiated_encoding("deflate")
        assert flagged.value.message != unsupported.value.message
        assert flagged.value.status is not unsupported.value.status

    def _echo_app(self):
        from wreath import Wreath

        service = GrpcService("t.S")

        @service.unary(request=Ping, response=Pong)
        async def M(request, msg):
            """Doc."""
            return Pong(text=msg.text)

        app = Wreath()
        app.include_router(service.router())
        return app

    def _payload(self, sent) -> bytes:
        return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")

    def test_a_gzip_request_body_reaches_the_handler_decoded(self):
        from wreath.compression import gzip_compress
        from wreath.protobuf import encode

        sent = _drive(
            self._echo_app(),
            "/t.S/M",
            frame_message(gzip_compress(encode(Ping(text="squeezed"))), compressed=True),
            extra_headers=((b"grpc-encoding", b"gzip"),),
        )
        trailers = dict(sent[-1]["headers"])
        assert trailers[b"grpc-status"] == b"0"
        assert self._payload(sent) == frame_message(encode(Pong(text="squeezed")))

    def test_a_client_that_accepts_gzip_gets_a_compressed_reply(self):
        from wreath.compression import gzip_decompress
        from wreath.protobuf import encode

        big = "x" * 4096
        sent = _drive(
            self._echo_app(),
            "/t.S/M",
            frame_message(encode(Ping(text=big))),
            extra_headers=((b"grpc-accept-encoding", b"gzip"),),
        )
        assert (b"grpc-encoding", b"gzip") in sent[0]["headers"]
        body = self._payload(sent)
        assert body[0] == 1
        assert len(body) < len(encode(Pong(text=big)))
        assert gzip_decompress(body[5:], max_output_bytes=1 << 20) == encode(Pong(text=big))

    def test_a_client_that_did_not_ask_for_gzip_gets_identity(self):
        from wreath.protobuf import encode

        big = "x" * 4096
        sent = _drive(self._echo_app(), "/t.S/M", frame_message(encode(Ping(text=big))))
        assert not any(name == b"grpc-encoding" for name, _ in sent[0]["headers"])
        assert self._payload(sent) == frame_message(encode(Pong(text=big)))

    def test_an_incompressible_reply_is_sent_uncompressed(self):
        from wreath.protobuf import encode

        sent = _drive(
            self._echo_app(),
            "/t.S/M",
            frame_message(encode(Ping(text="a"))),
            extra_headers=((b"grpc-accept-encoding", b"gzip"),),
        )
        body = self._payload(sent)
        assert body[0] == 0
        assert body == frame_message(encode(Pong(text="a")))

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (None, "identity"),
            ("", "identity"),
            ("identity", "identity"),
            ("gzip", "gzip"),
            ("deflate,gzip", "gzip"),
            ("GZIP", "gzip"),
            (" gzip , identity ", "gzip"),
            ("snappy", "identity"),
        ],
    )
    def test_the_reply_coding_is_read_from_what_the_client_accepts(self, header, expected):
        assert reply_encoding(header) == expected

    def test_the_server_advertises_both_codings_it_accepts(self):
        from wreath.protobuf import encode

        sent = _drive(self._echo_app(), "/t.S/M", frame_message(encode(Ping(text="a"))))
        assert (b"grpc-accept-encoding", b"identity,gzip") in sent[0]["headers"]

    def test_a_coding_in_accept_that_this_server_lacks_is_simply_not_used(self):
        from wreath.protobuf import encode

        sent = _drive(
            self._echo_app(),
            "/t.S/M",
            frame_message(encode(Ping(text="a" * 4096))),
            extra_headers=((b"grpc-accept-encoding", b"snappy,deflate"),),
        )
        assert not any(name == b"grpc-encoding" for name, _ in sent[0]["headers"])
        assert self._payload(sent)[0] == 0
