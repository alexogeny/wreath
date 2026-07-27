"""LocalObjectStore backend — round-trip, containment, atomic write, listing, presign.

Imports the built wreath package (needs ``_fsguard``), so the review+build+fix fork
runs it under ``uv``. Uses ``asyncio.run`` so it needs no pytest-asyncio config.
"""
import asyncio

import pytest

from wreath.storage import LocalObjectStore, ObjectError, normalize_key


def _run(coro):
    return asyncio.run(coro)


def test_normalize_key_rejects_escapes():
    assert normalize_key("a//b/./c") == "a/b/c"
    for bad in ("/abs", "../x", "a/../../b", "", ".", "a/\x00/b"):
        with pytest.raises(ObjectError):
            normalize_key(bad)


def test_roundtrip_and_stat(tmp_path):
    async def go():
        s = LocalObjectStore(tmp_path)
        stat = await s.write("reports/2026/q3.csv", b"col1,col2\n1,2\n", content_type="text/csv")
        assert stat.key == "reports/2026/q3.csv"
        assert stat.size == 14
        assert await s.read("reports/2026/q3.csv") == b"col1,col2\n1,2\n"
        assert await s.exists("reports/2026/q3.csv")
        st2 = await s.stat("reports/2026/q3.csv")
        assert st2.size == 14 and st2.content_type == "text/csv"
        s.close()

    _run(go())


def test_atomic_overwrite_and_ranged_read(tmp_path):
    async def go():
        s = LocalObjectStore(tmp_path)
        await s.write("k.bin", b"0123456789")
        await s.write("k.bin", b"abcdefghij")  # overwrite
        assert await s.read("k.bin") == b"abcdefghij"
        chunks = b""
        async for c in s.read_stream("k.bin", range=(2, 5)):
            chunks += c
        assert chunks == b"cdef"
        s.close()

    _run(go())


def test_stream_write_and_list_glob(tmp_path):
    async def go():
        s = LocalObjectStore(tmp_path)

        async def gen():
            yield b"aaa"
            yield b"bbb"

        await s.write_stream("logs/a.log", gen())
        await s.write("logs/b.log", b"x")
        await s.write("data/c.txt", b"y")
        keys = sorted([o.key async for o in s.list("logs/")])
        assert keys == ["logs/a.log", "logs/b.log"]
        assert await s.read("logs/a.log") == b"aaabbb"
        globbed = sorted([p.key async for p in s.path("logs").glob("*.log")])
        assert globbed == ["logs/a.log", "logs/b.log"]
        s.close()

    _run(go())


def test_missing_and_delete(tmp_path):
    async def go():
        s = LocalObjectStore(tmp_path)
        assert not await s.exists("nope")
        with pytest.raises(ObjectError):
            await s.read("nope")
        await s.write("gone.txt", b"z")
        await s.delete("gone.txt")
        assert not await s.exists("gone.txt")
        await s.delete("gone.txt")  # idempotent
        s.close()

    _run(go())


def test_containment_escape_rejected(tmp_path):
    async def go():
        s = LocalObjectStore(tmp_path)
        with pytest.raises(ObjectError):
            await s.write("../escape.txt", b"x")
        with pytest.raises(ObjectError):
            await s.read("../../etc/passwd")
        s.close()

    _run(go())


def test_local_presign_sign_and_verify(tmp_path):
    s = LocalObjectStore(tmp_path, url_secret=b"fixed-secret-32-bytes-length!!!!")
    url = s.url("reports/q3.csv", expires=900, method="GET")
    assert url.startswith("/reports/q3.csv?expires=900&signature=")
    sig = url.split("signature=")[1]
    assert s.verify_local_url("reports/q3.csv", method="GET", expires=900, signature=sig)
    assert not s.verify_local_url("reports/q3.csv", method="GET", expires=901, signature=sig)
    s.close()


def test_storagepath_ergonomics(tmp_path):
    async def go():
        s = LocalObjectStore(tmp_path)
        p = s.path("org/acme") / "project" / "state.json"
        assert p.key == "org/acme/project/state.json"
        assert p.name == "state.json" and p.suffix == ".json"
        assert p.parent.key == "org/acme/project"
        await p.write_bytes(b'{"ok":true}')
        assert await p.exists()
        assert await p.read_bytes() == b'{"ok":true}'
        async with p.open("rb") as fh:
            assert await fh.read() == b'{"ok":true}'
        await p.unlink()
        assert not await p.exists()
        s.close()

    _run(go())
