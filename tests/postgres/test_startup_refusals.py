from __future__ import annotations

import asyncio
import hashlib
import struct

import pytest

from wreath import _pgdriver as postgres


class _Writer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
async def test_backend_key_data_must_carry_both_protocol_integers() -> None:
    reader = asyncio.StreamReader()
    payload = b"short"
    reader.feed_data(b"K" + struct.pack("!I", len(payload) + 4) + payload)
    writer = _Writer()
    info = postgres._parse_dsn("postgresql://wreath@127.0.0.1/wreath_test")

    with pytest.raises(postgres.ProtocolError, match="invalid BackendKeyData"):
        await postgres._authenticate(reader, writer, info)

    assert writer.writes, "the startup packet is sent before the backend can answer"


def _md5_challenge(salt: bytes) -> bytes:
    """One AuthenticationMD5Password message, as the backend frames it."""
    payload = struct.pack("!I", 5) + salt
    return b"R" + struct.pack("!I", len(payload) + 4) + payload


def _md5_response(user: str, password: str, salt: bytes) -> bytes:
    """What PostgreSQL specifies the client must answer with."""
    inner = hashlib.md5(f"{password}{user}".encode()).hexdigest()
    return b"md5" + hashlib.md5(inner.encode() + salt).hexdigest().encode()


@pytest.mark.asyncio
async def test_md5_authentication_is_refused_without_the_legacy_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(postgres.LEGACY_MD5_ENV, raising=False)
    reader = asyncio.StreamReader()
    reader.feed_data(_md5_challenge(b"salt"))
    reader.feed_eof()
    writer = _Writer()
    info = postgres._parse_dsn("postgresql://wreath:secret@127.0.0.1/wreath_test")

    with pytest.raises(postgres.OperationalError, match="md5 authentication is refused"):
        await postgres._authenticate(reader, writer, info)


@pytest.mark.asyncio
async def test_md5_authentication_is_refused_off_loopback_even_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(postgres.LEGACY_MD5_ENV, postgres.LEGACY_MD5_VALUE)
    reader = asyncio.StreamReader()
    reader.feed_data(_md5_challenge(b"salt"))
    reader.feed_eof()
    writer = _Writer()
    info = postgres._parse_dsn("postgresql://wreath:secret@db.internal/wreath_test")

    with pytest.raises(postgres.OperationalError, match="only over loopback"):
        await postgres._authenticate(reader, writer, info, ("10.0.0.5", 5432))


@pytest.mark.asyncio
async def test_md5_authentication_answers_the_challenge_when_opted_in_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(postgres.LEGACY_MD5_ENV, postgres.LEGACY_MD5_VALUE)
    salt = b"\x01\x02\x03\x04"
    reader = asyncio.StreamReader()
    reader.feed_data(_md5_challenge(salt))
    reader.feed_data(b"Z" + struct.pack("!I", 5) + b"I")
    writer = _Writer()
    info = postgres._parse_dsn("postgresql://wreath:secret@127.0.0.1/wreath_test")

    await postgres._authenticate(reader, writer, info)

    expected = _md5_response("wreath", "secret", salt)
    assert any(expected in written for written in writer.writes), (
        "the md5 password message must carry md5(md5(password+user)+salt)"
    )


@pytest.mark.asyncio
async def test_md5_authentication_needs_a_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(postgres.LEGACY_MD5_ENV, postgres.LEGACY_MD5_VALUE)
    reader = asyncio.StreamReader()
    reader.feed_data(_md5_challenge(b"salt"))
    reader.feed_eof()
    writer = _Writer()
    info = postgres._parse_dsn("postgresql://wreath@127.0.0.1/wreath_test")

    with pytest.raises(postgres.OperationalError, match="password required"):
        await postgres._authenticate(reader, writer, info)


@pytest.mark.asyncio
async def test_md5_loopback_is_decided_by_the_peer_not_the_dsn_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(postgres.LEGACY_MD5_ENV, postgres.LEGACY_MD5_VALUE)
    salt = b"\x01\x02\x03\x04"
    reader = asyncio.StreamReader()
    reader.feed_data(_md5_challenge(salt))
    reader.feed_data(b"Z" + struct.pack("!I", 5) + b"I")
    writer = _Writer()
    info = postgres._parse_dsn("postgresql://wreath:secret@tfb-database/hello_world")

    await postgres._authenticate(reader, writer, info, ("::1", 5432, 0, 0))

    assert any(_md5_response("wreath", "secret", salt) in w for w in writer.writes)


@pytest.mark.asyncio
async def test_md5_is_refused_when_the_peer_is_remote_however_the_dsn_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(postgres.LEGACY_MD5_ENV, postgres.LEGACY_MD5_VALUE)
    reader = asyncio.StreamReader()
    reader.feed_data(_md5_challenge(b"salt"))
    reader.feed_eof()
    writer = _Writer()
    info = postgres._parse_dsn("postgresql://wreath:secret@localhost/wreath_test")

    with pytest.raises(postgres.OperationalError, match="only over loopback"):
        await postgres._authenticate(reader, writer, info, ("10.0.0.5", 5432))
