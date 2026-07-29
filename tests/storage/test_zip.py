"""Zip64 stream framing verified against the stdlib ``zipfile`` reference reader.

Standalone-runnable: ``objects.py`` is import-clean at module level (the ``_fsguard``
import is lazy inside LocalObjectStore), so it loads by path under ``/usr/bin/python3``
without the built wreath extension. Exercises `zip_stream`/`unzip_stream` + `MemoryObjectStore`.
"""
import asyncio
import importlib.util
import io
import pathlib
import sys
import zipfile

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "wreath"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SRC / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # slotted dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


# `objects.py`, not the deprecating `storage.py` alias: the alias imports
# relatively, which a by-path load has no parent package for.
module = _load("wreath_objects_standalone", "objects.py")


async def _collect(store, keys):
    out = bytearray()
    async for chunk in module.zip_stream(store, keys):
        out += chunk
    return bytes(out)


def _build(objects):
    async def go():
        store = module.MemoryObjectStore()
        for k, v in objects.items():
            await store.write(k, v)
        return await _collect(store, list(objects))

    return asyncio.run(go())


def _read_back(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert zf.testzip() is None  # every CRC validates
        return {info.filename: zf.read(info.filename) for info in zf.infolist()}


def test_empty_archive():
    archive = _build({})
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert zf.namelist() == []


def test_single_small():
    objects = {"a/b/hello.txt": b"hello world"}
    assert _read_back(_build(objects)) == objects


def test_many_entries():
    objects = {f"dir/file{i:03}.bin": bytes([i % 256]) * (i + 1) for i in range(50)}
    assert _read_back(_build(objects)) == objects


def test_large_multichunk():
    # >64 KiB so it crosses the read chunk boundary; CRC + size must accumulate correctly.
    objects = {"big.dat": b"wreath" * 200_000}  # ~1.2 MiB
    got = _read_back(_build(objects))
    assert got["big.dat"] == objects["big.dat"]


def test_unzip_roundtrip():
    objects = {"x/one.txt": b"one", "x/two.txt": b"two"}

    async def go():
        store = module.MemoryObjectStore()
        for k, v in objects.items():
            await store.write(k, v)
        archive = await _collect(store, list(objects))
        await store.write("bundle.zip", archive)
        written = await module.unzip_stream(store, "bundle.zip", prefix="out/")
        return written, {k: await store.read(k) for k in written}

    written, contents = asyncio.run(go())
    assert set(written) == {"out/x/one.txt", "out/x/two.txt"}
    assert contents["out/x/one.txt"] == b"one"


def test_unzip_refuses_an_entry_over_the_expansion_limit():
    """A tiny compressed object cannot make extraction allocate an arbitrary entry."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("safe.txt", b"safe")
        archive.writestr("payload.bin", b"A" * (1024 * 1024))

    async def go():
        store = module.MemoryObjectStore()
        await store.write("bomb.zip", buffer.getvalue())
        limits = module.ZipExtractionLimits(
            max_archive_bytes=64 * 1024,
            max_entries=8,
            max_entry_bytes=64 * 1024,
            max_total_bytes=128 * 1024,
        )
        try:
            await module.unzip_stream(store, "bomb.zip", limits=limits)
        except module.ObjectError as error:
            assert "payload.bin" in str(error)
            assert "1048576" in str(error)
        else:
            raise AssertionError("oversized expanded entry was accepted")
        assert not await store.exists("safe.txt")
        assert not await store.exists("payload.bin")

    asyncio.run(go())


def test_unzip_refuses_too_many_entries():
    archive = _build({"one": b"1", "two": b"2"})

    async def go():
        store = module.MemoryObjectStore()
        await store.write("many.zip", archive)
        limits = module.ZipExtractionLimits(max_entries=1)
        try:
            await module.unzip_stream(store, "many.zip", limits=limits)
        except module.ObjectError as error:
            assert "2 entries" in str(error)
        else:
            raise AssertionError("entry-count limit was not enforced")

    asyncio.run(go())


def test_unzip_refuses_cumulative_expanded_bytes():
    archive = _build({"one": b"1234", "two": b"5678"})

    async def go():
        store = module.MemoryObjectStore()
        await store.write("total.zip", archive)
        limits = module.ZipExtractionLimits(max_entry_bytes=8, max_total_bytes=7)
        try:
            await module.unzip_stream(store, "total.zip", limits=limits)
        except module.ObjectError as error:
            assert "output exceeds 7 bytes" in str(error)
        else:
            raise AssertionError("cumulative output limit was not enforced")
        assert not await store.exists("one")

    asyncio.run(go())


def test_unzip_refuses_archive_bytes_while_reading():
    archive = _build({"payload": b"x" * 1024})

    async def go():
        store = module.MemoryObjectStore()
        await store.write("large.zip", archive)
        limits = module.ZipExtractionLimits(max_archive_bytes=len(archive) - 1)
        try:
            await module.unzip_stream(store, "large.zip", limits=limits)
        except module.ObjectError as error:
            assert f"exceeds {len(archive) - 1} bytes" in str(error)
        else:
            raise AssertionError("archive byte limit was not enforced")

    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all zip tests PASS")
