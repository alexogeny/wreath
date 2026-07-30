"""Zip64 stream framing verified against the stdlib ``zipfile`` reference reader.

Standalone-runnable: ``objects.py`` is import-clean at module level (the ``_fsguard``
import is lazy inside LocalObjectStore), so it falls back to a by-path load under
``/usr/bin/python3`` without the built wreath extension. Exercises
`zip_stream`/`unzip_stream` + `MemoryObjectStore`.

The real ``wreath.objects`` is preferred whenever it imports, and the by-path load is
only the no-build fallback, because a by-path load defeats `wreath mutant`: it execs
pristine source into a *second* module object, so a mutation applied to
``wreath.objects`` in the forked child's memory never reaches the code under test.
Every `unzip_stream` limit below was reported `survived` by the runtime sweep while
these tests passed -- the same lie a subprocess test tells, for the same reason.
"""
import asyncio
import importlib
import importlib.util
import io
import pathlib
import struct
import sys
import zipfile

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "wreath"


def _load(name, filename):
    try:
        return importlib.import_module(f"wreath.{name}")
    except ImportError:
        spec = importlib.util.spec_from_file_location(name, _SRC / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod  # slotted dataclasses need the module registered
        spec.loader.exec_module(mod)
        return mod


# `objects`, not the deprecating `storage` alias: the alias imports relatively,
# which the by-path fallback has no parent package for.
module = _load("objects", "objects.py")


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


# --- `_zip_entry_count`: the pre-check that bounds work before `zipfile` runs ---
#
# Nothing called this function directly, and every one of its framing refusals
# returns `None` -- "let `zipfile` diagnose it" -- so the whole body could be
# replaced by `return None` and only the tests below would notice. What is lost
# when it is silently disabled is not a diagnosis but the bound: `unzip_stream`
# falls through to `zf.infolist()`, which materializes the object graph for every
# declared entry *before* `max_entries` is checked, which is the allocation this
# function exists to refuse.
#
# Five controls in it are provably redundant rather than untested, and no test
# below asserts inside the window where they would matter -- a test there would
# pin an accident instead of the contract. Each is defence in depth, so the tests
# that pin the *backstop* are named:
#
# - `eocd < 0` (:1304) and `zip64 < 0` (:1315) are masked by `directory_start < 0`
#   (:1326). A negative record offset becomes `directory_end`, and
#   `directory_end - directory_size` is then negative for every unsigned
#   `directory_size`, so the walk is refused one guard later either way. Backstop
#   pinned by `test_entry_count_walks_no_directory_that_starts_before_the_file`.
# - `directory_size == 0xFFFFFFFF` (:1322) is masked the same way: the sentinel
#   makes `directory_start` negative unless the archive is itself larger than 4
#   GiB, which is past `max_archive_bytes` and past what a test should allocate.
# - `cursor > directory_end` (:1337) is an early exit for the condition the
#   final `cursor == directory_end` (:1342) answers anyway; removing it costs one
#   more loop iteration, not a wrong answer. Pinned by
#   `test_entry_count_defers_a_record_that_overruns_the_directory`.
# - and therefore :1342's `else None` is unreachable while :1337 stands -- the
#   loop can only exit with `cursor >= directory_end`, and :1337 has already
#   refused every `>`. It stays because it is what makes the walk's exit
#   condition explicit at the point the count is returned.


def _stdlib_archive(count):
    """An archive with no zip64 records, as another writer would produce one."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(count):
            archive.writestr(f"f{i}.txt", b"x" * (i + 1))
    return buffer.getvalue()


def _tail(raw):
    """The `(eocd, locator, zip64)` offsets in an archive `zip_stream` wrote."""
    eocd = raw.rfind(b"PK\x05\x06")
    locator = eocd - 20
    assert raw[locator : locator + 4] == b"PK\x06\x07", "expected a zip64 locator"
    return eocd, locator, raw.rfind(b"PK\x06\x06", 0, locator)


def _patch(raw, offset, blob):
    out = bytearray(raw)
    out[offset : offset + len(blob)] = blob
    return bytes(out)


def test_entry_count_agrees_with_the_directory_it_counts():
    for count in (0, 1, 3, 9):
        archive = _build({f"f{i}.bin": b"x" * (i + 1) for i in range(count)})
        assert module._zip_entry_count(archive, 64) == count


def test_entry_count_counts_an_archive_without_zip64_records():
    # The `elif directory_size == 0xFFFFFFFF` arm, which a zip64 archive never
    # reaches: `zip_stream` always writes a locator, another writer does not.
    for count in (0, 1, 4):
        assert module._zip_entry_count(_stdlib_archive(count), 64) == count


def test_entry_count_stops_one_past_the_limit_instead_of_walking_the_directory():
    """The early return is the bound -- counting all of a hostile directory is the work refused."""
    archive = _build({f"f{i}.bin": b"x" for i in range(10)})
    assert module._zip_entry_count(archive, 3) == 4
    assert module._zip_entry_count(archive, 64) == 10


def test_unzip_refuses_from_the_precheck_before_zipfile_reads_the_directory():
    """Which of the two entry-count guards refused, pinned by the number in the message.

    The pre-check (`:1390`) stops at `max_entries + 1`; the `infolist()` check
    below it (`:1398`) reports the true total. Ten entries against a limit of
    three therefore says "4" from the first and "10" from the second, and
    asserting "4" is what distinguishes them. `test_unzip_refuses_too_many_entries`
    above cannot: two entries against a limit of one reads "2 entries" whichever
    guard fired, so both were free to be deleted with the suite still green.
    """
    archive = _build({f"f{i}.bin": b"x" for i in range(10)})

    async def go():
        store = module.MemoryObjectStore()
        await store.write("many.zip", archive)
        limits = module.ZipExtractionLimits(max_entries=3)
        try:
            await module.unzip_stream(store, "many.zip", limits=limits)
        except module.ObjectError as error:
            assert "has 4 entries" in str(error), str(error)
            assert "limit is 3" in str(error), str(error)
        else:
            raise AssertionError("entry-count pre-check was not enforced")

    asyncio.run(go())


def test_entry_count_defers_a_truncated_eocd():
    archive = _build({"one.bin": b"1"})
    assert module._zip_entry_count(archive[:-3], 64) is None


def test_entry_count_defers_a_comment_length_that_lies():
    """`eocd + 22 + comment_bytes != len(raw)`: trailing bytes the record does not account for."""
    archive = _build({"one.bin": b"1"})
    eocd, _, _ = _tail(archive)
    assert module._zip_entry_count(_patch(archive, eocd + 20, b"\x01\x00"), 64) is None


def test_entry_count_defers_a_zip64_record_that_overlaps_its_locator():
    """`zip64 + 56 > locator`: a 56-byte record cannot fit in the space it claims.

    Planted one byte past where the real record starts, so `rfind` prefers it, and
    with a `record_size` that satisfies the *next* guard -- otherwise that guard
    refuses this input too and the overlap check is never what is being tested.
    """
    archive = _build({"one.bin": b"1"})
    _, locator, _ = _tail(archive)
    fake = locator - 55
    raw = _patch(archive, fake, b"PK\x06\x06" + struct.pack("<Q", 43))
    raw = _patch(raw, fake + 40, struct.pack("<Q", 0))  # directory_size
    assert raw.rfind(b"PK\x06\x06", 0, locator) == fake  # the planted one wins
    assert fake + 12 + 43 == locator  # `:1318` would accept it
    assert module._zip_entry_count(raw, 64) is None


def test_entry_count_defers_a_zip64_record_size_that_misses_the_locator():
    archive = _build({"one.bin": b"1"})
    _, locator, zip64 = _tail(archive)
    record_size = struct.unpack_from("<Q", archive, zip64 + 4)[0]
    assert zip64 + 12 + record_size == locator  # the archive as written
    raw = _patch(archive, zip64 + 4, struct.pack("<Q", record_size + 1))
    assert module._zip_entry_count(raw, 64) is None


def test_entry_count_walks_no_directory_that_starts_before_the_file():
    """`directory_start < 0`, and why a negative offset is not self-refusing.

    A negative `cursor` does not raise in Python -- it indexes from the *end*, so
    `raw[cursor:cursor + 4]` reads real archive bytes and the record signature can
    match. This declares a directory reaching 74 bytes before the file starts, and
    a first record whose name length lands the cursor exactly on `directory_end`:
    without the guard the walk reports one entry from bytes it was never given a
    directory for. That is the backstop `eocd < 0`, `zip64 < 0` and the
    `0xFFFFFFFF` sentinel above all lean on, so it is pinned here rather than
    three times over.
    """
    archive = _stdlib_archive(1)
    eocd = archive.rfind(b"PK\x05\x06")
    directory_start = struct.unpack_from("<I", archive, eocd + 16)[0]
    behind = len(archive) - directory_start  # `-behind` lands on the real record
    raw = _patch(archive, eocd + 12, struct.pack("<I", eocd + behind))
    raw = _patch(raw, directory_start + 28, struct.pack("<HHH", eocd + behind - 46, 0, 0))
    assert raw[-behind : -behind + 4] == b"PK\x01\x02"  # a walkable signature
    assert module._zip_entry_count(raw, 64) is None


def test_entry_count_defers_a_record_that_cannot_fit_before_the_directory_end():
    """`cursor + 46 > directory_end`: a fixed 46-byte record needs 46 bytes of room.

    The signature is made to match so the bound is the only thing refusing: without
    it the header fields are unpacked from past the end of the buffer, which is a
    `struct.error` out of a function whose contract is to return `None`.
    """
    archive = _stdlib_archive(1)
    eocd = archive.rfind(b"PK\x05\x06")
    raw = _patch(archive, eocd - 10, b"PK\x01\x02")
    raw = _patch(raw, eocd + 12, struct.pack("<I", 10))  # a 10-byte directory
    assert module._zip_entry_count(raw, 64) is None


def test_entry_count_defers_a_record_that_overruns_the_directory():
    """A name length that carries the cursor past `directory_end`."""
    archive = _stdlib_archive(2)
    first = archive.find(b"PK\x01\x02")
    raw = _patch(archive, first + 28, struct.pack("<HHH", 60_000, 0, 0))
    assert module._zip_entry_count(raw, 64) is None


def test_entry_count_reads_no_zip64_locator_from_before_the_file():
    """`locator >= 0`, which a negative slice would otherwise satisfy.

    An archive can be as short as its 22-byte end record, which puts `locator` at
    -20 and `raw[locator:locator + 4]` four bytes from the *end* -- inside the end
    record itself. These 26 bytes spell the locator signature there, so without the
    bound the reader would go looking for a zip64 record that no offset in this file
    points at, and refuse an archive that declares no entries and has none.
    """
    blob = bytearray(b"\x00" * 4 + b"PK\x05\x06" + b"\x00" * 18)
    blob[10:14] = b"PK\x06\x07"  # cd-start-disk + entries-on-disk, read as a locator
    blob[16:20] = struct.pack("<I", 0)  # directory size
    blob[24:26] = struct.pack("<H", 0)  # comment length
    raw = bytes(blob)
    assert raw[-16:-12] == b"PK\x06\x07"
    assert module._zip_entry_count(raw, 64) == 0


def test_entry_count_defers_a_central_record_with_a_wrong_signature():
    """Only the signature byte is changed, so the walk would otherwise still add up.

    A corruption that also broke the name/extra/comment lengths would be refused
    by the cursor-overrun guard below instead, and the signature check could be
    deleted with this test still passing.
    """
    archive = _stdlib_archive(3)
    first = archive.find(b"PK\x01\x02")
    assert first > 0
    raw = _patch(archive, first + 3, b"\x03")
    assert module._zip_entry_count(raw, 64) is None


def test_unzip_extracts_an_archive_the_precheck_cannot_frame():
    """Trailing bytes: `zipfile` reads the archive, the pre-check declines to count it.

    `zipfile` locates the directory through the end record's offsets and never
    minds bytes after it; the pre-check requires the end record to be the end of
    the file, so it returns `None` -- "no answer", not "zero entries". Extraction
    must proceed on the reader's count, which is what the `is not None` clause
    buys: without it the comparison is `None > 1024` and a readable archive fails
    with a `TypeError` instead of extracting.
    """
    archive = _build({"one.txt": b"one", "two.txt": b"two"}) + b"trailing junk"
    assert module._zip_entry_count(archive, 64) is None

    async def go():
        store = module.MemoryObjectStore()
        await store.write("padded.zip", archive)
        written = await module.unzip_stream(store, "padded.zip")
        assert set(written) == {"one.txt", "two.txt"}, written
        assert await store.read("one.txt") == b"one"

    asyncio.run(go())


def test_unzip_falls_back_to_the_reader_count_when_the_precheck_declines():
    """The `infolist()` limit, unreachable while the pre-check answers first.

    Its refusal is the only one left when the pre-check returns `None`, and it
    reports the true total rather than stopping one past the limit -- so "2
    entries" here, against "4" in the pre-check test above, is what says which
    guard ran.
    """
    archive = _build({"one.txt": b"one", "two.txt": b"two"}) + b"trailing junk"

    async def go():
        store = module.MemoryObjectStore()
        await store.write("padded.zip", archive)
        limits = module.ZipExtractionLimits(max_entries=1)
        try:
            await module.unzip_stream(store, "padded.zip", limits=limits)
        except module.ObjectError as error:
            assert "has 2 entries" in str(error), str(error)
            assert "limit is 1" in str(error), str(error)
        else:
            raise AssertionError("the reader-count limit was not enforced")
        assert not await store.exists("one.txt")

    asyncio.run(go())


def test_unzip_skips_directory_entries():
    """An object store has no directories, so a zero-byte object named after one is noise."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("dir/", b"")
        archive.writestr("dir/file.txt", b"contents")

    async def go():
        store = module.MemoryObjectStore()
        await store.write("dirs.zip", buffer.getvalue())
        written = await module.unzip_stream(store, "dirs.zip")
        assert written == ["dir/file.txt"], written
        assert not await store.exists("dir/")
        assert not await store.exists("dir")

    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all zip tests PASS")
