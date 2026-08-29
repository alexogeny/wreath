from __future__ import annotations

import asyncio
import functools
import socket

import pytest

from wreath import Wreath
from wreath.authorization import roles
from wreath.grpc import GrpcError, GrpcService, Status
from wreath.protobuf import decode, encode, field, message

grpc = pytest.importorskip("grpc", reason="grpcio is the reference peer for gRPC interoperability")

pytestmark = [pytest.mark.network, pytest.mark.asyncio]


@message
class Echo:
    text: str = field(1)
    count: int = field(2)


def _ser(msg: object) -> bytes:
    return encode(msg)


def _de(data: bytes) -> Echo:
    return decode(Echo, data)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_app() -> Wreath:
    service = GrpcService("wreath.test.Echoer")

    @service.unary(request=Echo, response=Echo)
    async def Once(request, msg: Echo) -> Echo:
        """Echo the message back with the count doubled."""
        return Echo(text=msg.text, count=msg.count * 2)

    @service.unary(request=Echo, response=Echo)
    async def Refuse(request, msg: Echo) -> Echo:
        """Always refuse, so the status and message can be read off the wire."""
        raise GrpcError(Status.PERMISSION_DENIED, "not for you: \n control chars")

    @service.server_stream(request=Echo, response=Echo)
    async def Many(request, msg: Echo):
        """Yield `count` replies."""
        for index in range(msg.count):
            yield Echo(text=msg.text, count=index)

    @service.client_stream(request=Echo, response=Echo)
    async def Sum(request, messages) -> Echo:
        """Add up everything the client streamed."""
        total = 0
        last = ""
        async for msg in messages:
            total += msg.count
            last = msg.text
        return Echo(text=last, count=total)

    @service.bidi(request=Echo, response=Echo)
    async def Both(request, messages):
        """Echo each message as it arrives."""
        async for msg in messages:
            yield Echo(text=msg.text, count=msg.count + 1)

    @service.unary(request=Echo, response=Echo)
    async def Slow(request, msg: Echo) -> Echo:
        """Sleep past any sane deadline."""
        await asyncio.sleep(5)
        return msg

    @service.unary(request=Echo, response=Echo)
    @roles("ranger")
    async def Guarded(request, msg: Echo) -> Echo:
        """Reachable only by a ranger; the point is that it is never reached."""
        return msg  # pragma: no cover - the guard refuses before this runs

    @service.server_stream(request=Echo, response=Echo)
    async def FailsMidStream(request, msg: Echo):
        """Send one message, then fail -- the hard case for status reporting."""
        yield Echo(text="first", count=1)
        raise GrpcError(Status.ABORTED, "gave up after the first")

    app = Wreath()
    app.include_router(service.router())
    return app


@functools.cache
def _cert() -> tuple[str, str, bytes]:
    """A throwaway self-signed certificate for localhost.

    Minted with `cryptography`, exactly as the existing TLS and HTTP/3 suites
    do -- it is in the dev group for this purpose.

    Cached for the module: an RSA-2048 keygen per test is most of this file's
    wall clock, and every test wants the same throwaway identity.
    """
    import datetime
    import tempfile

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    directory = tempfile.mkdtemp()
    cert_path, key_path = f"{directory}/c.pem", f"{directory}/k.pem"
    with open(cert_path, "wb") as handle:
        handle.write(pem)
    with open(key_path, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path, pem


@pytest.fixture
async def endpoint():
    """A real native HTTP/2 listener, over TLS.

    **gRPC on wreath is TLS-only**, and that is a property of the server rather
    than a choice made here: `serve` refuses `h2` without `ssl=` or `tls=`,
    because it negotiates HTTP/2 through ALPN and never sniffs the first
    application bytes. Prior-knowledge h2c -- what `grpc.aio.insecure_channel`
    speaks -- is therefore not available, so the client below is a
    `secure_channel` pinned to this throwaway certificate.
    """
    from wreath.server import ServerConfig, TLSConfig, serve

    cert_path, key_path, pem = _cert()
    port = _free_port()
    server = await serve(
        build_app(),
        ServerConfig(host="127.0.0.1", port=port, protocols=("h2",)),
        tls=TLSConfig(certfile=cert_path, keyfile=key_path),
    )
    try:
        yield f"localhost:{port}", pem
    finally:
        await server.close()
        await server.wait_closed()


def _channel(endpoint):
    """A gRPC channel that trusts only this run's throwaway certificate."""
    target, pem = endpoint
    credentials = grpc.ssl_channel_credentials(root_certificates=pem)
    return grpc.aio.secure_channel(target, credentials)


async def test_a_real_grpc_client_completes_a_unary_call(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_unary(
            "/wreath.test.Echoer/Once",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        reply = await call(Echo(text="hello", count=21))
    assert reply.text == "hello"
    assert reply.count == 42


async def test_a_refusal_arrives_as_a_status_not_a_transport_error(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_unary(
            "/wreath.test.Echoer/Refuse",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await call(Echo(text="x", count=1))
    assert caught.value.code() == grpc.StatusCode.PERMISSION_DENIED
    assert "not for you" in caught.value.details()


async def test_an_unknown_method_is_unimplemented(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_unary(
            "/wreath.test.Echoer/NoSuchMethod",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await call(Echo(text="x", count=1))
    assert caught.value.code() in (
        grpc.StatusCode.UNIMPLEMENTED,
        grpc.StatusCode.NOT_FOUND,
    )


async def test_server_streaming_delivers_every_message(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_stream(
            "/wreath.test.Echoer/Many",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        got = [reply.count async for reply in call(Echo(text="s", count=5))]
    assert got == [0, 1, 2, 3, 4]


async def test_client_streaming_consumes_every_message(endpoint):
    async def outgoing():
        for value in (1, 2, 3, 4):
            yield Echo(text="c", count=value)

    async with _channel(endpoint) as channel:
        call = channel.stream_unary(
            "/wreath.test.Echoer/Sum",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        reply = await call(outgoing())
    assert reply.count == 10


async def test_bidirectional_streaming_interleaves(endpoint):
    async def outgoing():
        for value in (10, 20, 30):
            yield Echo(text="b", count=value)

    async with _channel(endpoint) as channel:
        call = channel.stream_stream(
            "/wreath.test.Echoer/Both",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        got = [reply.count async for reply in call(outgoing())]
    assert got == [11, 21, 31]


async def test_an_auth_decorator_on_a_grpc_method_actually_refuses(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_unary(
            "/wreath.test.Echoer/Guarded",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await call(Echo(text="x", count=1))
    assert caught.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_a_failure_after_the_first_message_still_reports_its_status(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_stream(
            "/wreath.test.Echoer/FailsMidStream",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        stream = call(Echo(text="x", count=1))
        received = []
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            async for reply in stream:
                received.append(reply.text)
    assert received == ["first"]
    assert caught.value.code() == grpc.StatusCode.ABORTED


async def test_a_client_deadline_is_honoured_by_the_server(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_unary(
            "/wreath.test.Echoer/Slow",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await call(Echo(text="x", count=1), timeout=0.3)
    assert caught.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED


async def test_a_real_client_sends_gzip_and_wreath_reads_it(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_unary(
            "/wreath.test.Echoer/Once",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        reply = await call(
            Echo(text="collar " * 2000, count=21),
            compression=grpc.Compression.Gzip,
        )
    assert reply.text == "collar " * 2000
    assert reply.count == 42


async def test_a_client_asking_for_an_unimplemented_coding_is_told_so(endpoint):
    async with _channel(endpoint) as channel:
        call = channel.unary_unary(
            "/wreath.test.Echoer/Once",
            request_serializer=_ser,
            response_deserializer=_de,
        )
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await call(Echo(text="x", count=1), metadata=(("grpc-encoding", "deflate"),))
    assert caught.value.code() == grpc.StatusCode.UNIMPLEMENTED
    assert "deflate" in caught.value.details()
