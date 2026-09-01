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


def test_list_pushes_a_directory_prefix_into_the_native_walk(tmp_path, monkeypatch):
    s = LocalObjectStore(tmp_path)
    walked: list[str] = []
    real_walk = objects._core.local_walk

    def recording_walk(root, scandir, join):
        walked.append(os.fspath(root))
        return real_walk(root, scandir, join)

    monkeypatch.setattr(objects._core, "local_walk", recording_walk)

    async def go():
        await s.write("logs/2026/a.log", b"a")
        await s.write("archive/old.log", b"old")
        assert [item.key async for item in s.list("logs/2026/")] == ["logs/2026/a.log"]

    _run(go())
    assert len(walked) == 1
    assert walked[0] != os.fspath(tmp_path), "the unrelated archive must not be traversed"
    s.close()


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
            "kept.tmp",
            "nested/deep.txt",
            "real.txt",
        ]

    _run(go())
    s.close()


def test_a_write_block_that_raises_stores_nothing():
    async def go():
        s = MemoryObjectStore()
        with pytest.raises(ZeroDivisionError):
            async with s.path("half.txt").open("wb") as fh:
                fh.write(b"the first half")
                raise ZeroDivisionError("the handler blew up mid-body")
        assert not await s.exists("half.txt")

    _run(go())


def test_glob_star_crosses_a_slash_and_iterdir_recurses():
    async def go():
        s = MemoryObjectStore()
        for key in ("reports/summary.csv", "reports/2026/q3.csv", "reports/notes.txt"):
            await s.write(key, b"x")
        assert sorted([p.key async for p in s.path("reports").glob("*.csv")]) == [
            "reports/2026/q3.csv",
            "reports/summary.csv",
        ]
        assert sorted([p.key async for p in s.path("reports").glob("2026/*.csv")]) == [
            "reports/2026/q3.csv",
        ]
        assert sorted([p.key async for p in s.path("reports").iterdir()]) == [
            "reports/2026/q3.csv",
            "reports/notes.txt",
            "reports/summary.csv",
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
    with pytest.raises(ObjectError, match="must be a string, not NoneType"):
        normalize_key(None)  # type: ignore[arg-type]
    with pytest.raises(ObjectError, match="empty object key"):
        normalize_key("")


def test_closing_twice_cannot_close_a_recycled_descriptor(tmp_path):
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


# `test_local_presign_sign_and_verify` signs and verifies with one store, so it
# holds for a store that ignored `url_secret` and signed with a random key: the
# same instance verifies its own signature either way. What a secret is *for* is
# that another process -- the next worker, after a restart -- verifies a URL this
# one issued, so these use a second store as the verifier.

_SECRET = b"fixed-secret-32-bytes-length!!!!"
_OTHER = b"another-secret-32-bytes-long!!!!"


def _signed(store, key="reports/q3.csv"):
    q = _query(store.url(key, expires=900, method="GET"))
    return dict(method="GET", expires=int(q["expires"]), signature=q["signature"])


def test_a_second_store_with_the_same_url_secret_verifies_the_first_ones_url(tmp_path):
    for issuer, verifier, stranger in (
        (
            LocalObjectStore(tmp_path, url_secret=_SECRET),
            LocalObjectStore(tmp_path, url_secret=_SECRET),
            LocalObjectStore(tmp_path, url_secret=_OTHER),
        ),
        (
            MemoryObjectStore(url_secret=_SECRET),
            MemoryObjectStore(url_secret=_SECRET),
            MemoryObjectStore(url_secret=_OTHER),
        ),
    ):
        claim = _signed(issuer)
        assert verifier.verify_local_url("reports/q3.csv", **claim)
        assert not stranger.verify_local_url("reports/q3.csv", **claim)
        for store in (issuer, verifier, stranger):
            if isinstance(store, LocalObjectStore):
                store.close()


def test_a_store_given_no_url_secret_signs_with_one_only_it_knows(tmp_path):
    a = LocalObjectStore(tmp_path)
    b = LocalObjectStore(tmp_path)
    claim = _signed(a)
    assert a.verify_local_url("reports/q3.csv", **claim)
    assert not b.verify_local_url("reports/q3.csv", **claim)
    a.close()
    b.close()

    m, n = MemoryObjectStore(), MemoryObjectStore()
    claim = _signed(m)
    assert m.verify_local_url("reports/q3.csv", **claim)
    assert not n.verify_local_url("reports/q3.csv", **claim)


def test_a_url_secret_that_is_not_bytes_is_refused_by_type_and_by_name():
    for factory in (MemoryObjectStore, lambda **kw: LocalObjectStore("/tmp", **kw)):
        with pytest.raises(TypeError) as text:
            factory(url_secret="a-string-secret")
        assert "url_secret must be bytes, not str" in str(text.value)
        assert "encode it" in str(text.value)

        with pytest.raises(TypeError) as number:
            factory(url_secret=32)
        assert "url_secret must be bytes, not int" in str(number.value)
        assert "encode it" not in str(number.value)  # there is nothing to encode


def test_a_bytes_like_url_secret_is_accepted_and_copied():
    for secret in (bytearray(_SECRET), memoryview(_SECRET)):
        store = MemoryObjectStore(url_secret=secret)
        assert store.verify_local_url("k", **_signed(store, "k"))


def test_normalize_key_refuses_a_delete_character():
    with pytest.raises(ObjectError, match="control character"):
        normalize_key("reports/q3\x7f.csv")
    with pytest.raises(ObjectError, match="control character"):
        normalize_key("reports/\x1f.csv")


def test_a_directory_component_that_is_a_symlink_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "link")

    async def go():
        s = LocalObjectStore(tmp_path)
        with pytest.raises(ObjectError, match="refusing symlink component"):
            await s.write("link/f.txt", b"x")
        assert not (tmp_path / "real" / "f.txt").exists()
        s.close()

    _run(go())


def test_deleting_beneath_a_directory_that_is_not_there_creates_nothing(tmp_path):
    async def go():
        s = LocalObjectStore(tmp_path)
        await s.delete("no/such/dir/f.txt")  # not an error
        assert not (tmp_path / "no").exists()
        s.close()

    _run(go())


def test_stat_reports_a_symlink_that_leaves_the_root_as_a_storage_error(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"not yours")
    os.symlink(outside, root / "esc")

    async def go():
        s = LocalObjectStore(root)
        with pytest.raises(ObjectError):
            await s.stat("esc/secret.txt")
        with pytest.raises(ObjectError):
            await s.read("esc/secret.txt")
        s.close()

    _run(go())


def test_a_read_stream_is_chunked_rather_than_read_whole(tmp_path):
    payload = b"wreath!!" * 40_000  # 320 KiB, not a multiple of the chunk

    async def go():
        local = LocalObjectStore(tmp_path)
        memory = MemoryObjectStore()
        for store in (local, memory):
            await store.write("big.bin", payload)
            sizes = [len(c) async for c in store.read_stream("big.bin")]
            assert sum(sizes) == len(payload)
            assert len(sizes) > 1, sizes
            assert set(sizes[:-1]) == {1 << 16}, sizes
            assert sizes[-1] <= 1 << 16
        local.close()

    _run(go())


def test_memory_list_matches_the_prefix_it_was_given(tmp_path):
    async def go():
        s = MemoryObjectStore()
        for key in ("a/1.txt", "a/2.txt", "b/3.txt"):
            await s.write(key, b"x")
        assert [o.key async for o in s.list("a/")] == ["a/1.txt", "a/2.txt"]
        assert [o.key async for o in s.list("b/")] == ["b/3.txt"]
        assert [o.key async for o in s.list()] == ["a/1.txt", "a/2.txt", "b/3.txt"]

    _run(go())


@pytest.mark.parametrize(
    "signature",
    [
        "é",  # one non-ASCII character
        "deadbeefé",  # a real signature with one appended
        "١" * 64,  # Arabic-Indic digits: `isdigit()` is True
    ],
)
def test_a_non_ascii_signature_is_refused_rather_than_raised(signature):
    store = MemoryObjectStore(url_secret=_SECRET)
    assert not store.verify_local_url(
        "reports/q3.csv", method="GET", expires=1_000_900, signature=signature
    )
