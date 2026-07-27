"""LocalObjectStore backend — round-trip, containment, atomic write, listing, presign.

Imports the built wreath package (needs ``_fsguard``), so the review+build+fix fork
runs it under ``uv``. Uses ``asyncio.run`` so it needs no pytest-asyncio config.
"""
import asyncio
import os

import pytest

from wreath import objects
from wreath.objects import (
    LocalObjectStore,
    MemoryObjectStore,
    ObjectError,
    normalize_key,
)


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


def _query(url: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))


def test_local_presign_sign_and_verify(tmp_path, monkeypatch):
    s = LocalObjectStore(tmp_path, url_secret=b"fixed-secret-32-bytes-length!!!!")
    monkeypatch.setattr(objects, "_now", lambda: 1_000_000.0)
    url = s.url("reports/q3.csv", expires=900, method="GET")
    assert url.startswith("/reports/q3.csv?expires=1000900&signature=")
    sig = _query(url)["signature"]
    ok = dict(method="GET", expires=1_000_900, signature=sig)
    assert s.verify_local_url("reports/q3.csv", **ok)
    # neither the deadline nor the method can be edited without breaking the HMAC
    assert not s.verify_local_url("reports/q3.csv", **{**ok, "expires": 1_000_901})
    assert not s.verify_local_url("reports/q3.csv", **{**ok, "method": "PUT"})
    s.close()


def test_a_local_signed_url_stops_working_at_its_deadline(tmp_path, monkeypatch):
    """`expires` is enforced, not merely signed.

    The URL carries an absolute deadline rather than a lifetime, because a
    lifetime with no issue time next to it is a number no verifier can act on:
    before this, the only way to invalidate an outstanding URL was to rotate
    ``url_secret`` and break every other URL with it.
    """
    s = LocalObjectStore(tmp_path, url_secret=b"k" * 32)
    monkeypatch.setattr(objects, "_now", lambda: 1_000_000.0)
    q = _query(s.url("a.txt", expires=900, method="GET"))
    assert q["expires"] == "1000900"
    args = dict(method="GET", expires=int(q["expires"]), signature=q["signature"])

    assert s.verify_local_url("a.txt", **args)
    monkeypatch.setattr(objects, "_now", lambda: 1_000_899.0)
    assert s.verify_local_url("a.txt", **args), "still inside the window"
    monkeypatch.setattr(objects, "_now", lambda: 1_000_901.0)
    assert not s.verify_local_url("a.txt", **args), "an expired URL must be refused"
    s.close()


def test_a_memory_signed_url_expires_the_same_way(monkeypatch):
    """The memory twin verifies with the same arithmetic, so a route can be tested on it."""
    s = MemoryObjectStore(url_secret=b"k" * 32)
    monkeypatch.setattr(objects, "_now", lambda: 500.0)
    url = s.url("a.txt", expires=60, method="GET")
    assert url.startswith("memory:///a.txt?expires=560&signature=")
    q = _query(url)
    args = dict(method="GET", expires=int(q["expires"]), signature=q["signature"])
    assert s.verify_local_url("a.txt", **args)
    monkeypatch.setattr(objects, "_now", lambda: 561.0)
    assert not s.verify_local_url("a.txt", **args)


def test_etags_have_one_format_across_backends(tmp_path):
    """No backend quotes its etag, so a cross-backend comparison fails only for real reasons."""
    async def go():
        mem = MemoryObjectStore()
        local = LocalObjectStore(tmp_path)
        stats = [await mem.write("a.bin", b"hello"), await local.write("a.bin", b"hello")]
        for st in stats:
            assert '"' not in st.etag, st.etag
        assert (await mem.stat("a.bin")).etag == stats[0].etag
        assert (await local.stat("a.bin")).etag == stats[1].etag
        local.close()

    _run(go())


def test_list_does_not_report_write_temporaries(tmp_path):
    """A killed write leaves `.<name>.<hex>.tmp` behind; it is not an object."""
    s = LocalObjectStore(tmp_path)

    async def populate():
        await s.write("real.txt", b"x")
        await s.write("kept.tmp", b"x")  # a genuine object that happens to end in .tmp
        await s.write("nested/deep.txt", b"x")

    _run(populate())
    (tmp_path / ".real.txt.aabbccddeeff.tmp").write_bytes(b"half a body")
    (tmp_path / "nested" / ".deep.txt.0123456789ab.tmp").write_bytes(b"half a body")

    async def go():
        assert sorted([o.key async for o in s.list()]) == [
            "kept.tmp", "nested/deep.txt", "real.txt",
        ]

    _run(go())
    s.close()


def test_a_write_block_that_raises_stores_nothing():
    """`open("wb")` flushes on a clean exit only — a failed body leaves no partial object."""
    async def go():
        s = MemoryObjectStore()
        with pytest.raises(ZeroDivisionError):
            async with s.path("half.txt").open("wb") as fh:
                fh.write(b"the first half")
                raise ZeroDivisionError("the handler blew up mid-body")
        assert not await s.exists("half.txt")

    _run(go())


def test_glob_star_crosses_a_slash_and_iterdir_recurses():
    """The stated contract: keys are not paths, so `*` does not stop at `/`.

    `fnmatch` over the whole key, not `pathlib` semantics -- pinned here because
    the difference is invisible until a listing returns more than expected.
    """
    async def go():
        s = MemoryObjectStore()
        for key in ("reports/summary.csv", "reports/2026/q3.csv", "reports/notes.txt"):
            await s.write(key, b"x")
        assert sorted([p.key async for p in s.path("reports").glob("*.csv")]) == [
            "reports/2026/q3.csv", "reports/summary.csv",
        ]
        assert sorted([p.key async for p in s.path("reports").glob("2026/*.csv")]) == [
            "reports/2026/q3.csv",
        ]
        assert sorted([p.key async for p in s.path("reports").iterdir()]) == [
            "reports/2026/q3.csv", "reports/notes.txt", "reports/summary.csv",
        ]

    _run(go())


def test_an_empty_object_yields_no_chunks_on_either_backend(tmp_path):
    async def go():
        mem = MemoryObjectStore()
        local = LocalObjectStore(tmp_path)
        await mem.write("empty.bin", b"")
        await local.write("empty.bin", b"")
        assert [c async for c in mem.read_stream("empty.bin")] == []
        assert [c async for c in local.read_stream("empty.bin")] == []
        local.close()

    _run(go())


def test_normalize_key_says_when_it_was_handed_a_non_string():
    """`None` is a field that was never populated, not an empty key."""
    with pytest.raises(ObjectError, match="must be a string, not NoneType"):
        normalize_key(None)  # type: ignore[arg-type]
    with pytest.raises(ObjectError, match="empty object key"):
        normalize_key("")


def test_closing_twice_cannot_close_a_recycled_descriptor(tmp_path):
    """The second `close()` must be a no-op, not a close of whatever inherited the fd."""
    s = LocalObjectStore(tmp_path)
    root_fd = s._root_fd
    s.close()
    dup = os.dup(1)
    if dup != root_fd:  # the freed number was not handed straight back
        os.close(dup)
        pytest.skip("the descriptor number was not recycled")
    s.close()
    os.fstat(dup)  # EBADF here means the second close took the recycled descriptor
    os.close(dup)


def test_the_deprecated_storage_alias_still_resolves(recwarn):
    import importlib

    module = importlib.import_module("wreath.storage")
    importlib.reload(module)  # the warning fires at import time
    assert module.LocalStorage is LocalObjectStore
    assert module.StorageError is ObjectError
    assert any(issubclass(w.category, DeprecationWarning) for w in recwarn)


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
