"""Object storage: one protocol, three backends, and no cloud SDK.

An application that stores uploads wants the same handful of operations wherever
the bytes end up -- read, write, stat, exists, list, delete, presign -- and wants
them to behave the same in a test, on a developer's disk, and against S3. This
module is that protocol (`ObjectStore`) and the backends that satisfy it:
`MemoryObjectStore` for tests, `LocalObjectStore` for a directory
on disk, and `S3ObjectStore` for S3 and anything speaking its REST API
(MinIO, R2, Wasabi, Spaces). `ObjectPath` is the pathlib-shaped handle
over any of them.

**Every key goes through `normalize_key`**, which is the single containment
gate. A backend cannot be trusted to refuse a traversal on its own -- a local
filesystem resolves `../` and an S3 bucket stores a key spelled `../../etc`
without complaint -- so the check lives in one function that every entry point
calls, rather than in each backend where one omission is invisible.

**No boto3, and no third-party dependency at all.** S3 authentication is SigV4,
an HMAC chain over a canonicalised request; `wreath._sigv4` is that chain in
under two hundred lines, and the transport is wreath's own
`HTTPClient`, so there is no second HTTP stack in the
process.

**Memory is bounded on the paths where an object can be large, and the paths
where it is not say so.** Reading from S3 streams back in ranged windows; writing
switches to a real multipart upload once the data passes the part size;
`zip_stream` emits a Zip64 archive without ever holding an object or the
archive. What buffers, deliberately and by necessity, is `unzip_stream`
(the stdlib `zipfile` reader needs a seekable archive, bounded by
`ZipExtractionLimits`), `file_chunks` (its argument is already bytes in hand),
and each individual S3 request (the HTTP client is buffered per operation).

Wire a store into an application with `wreath.app.Wreath.objects`, which
gives an S3 store a pinned outbound client started and stopped with the app, and
closes a local store's root descriptor on shutdown:

```python
app.objects("assets", backend="s3", bucket="ev-assets", region="ap-southeast-2")
store = app.state.objects_assets
```

The guide is [Object storage](../guides/objects.md).
"""
from __future__ import annotations

import asyncio
import errno
import fnmatch
import hashlib
import hmac
import json as _json
import mimetypes
import os
import re
import stat as _stat
import struct
import time
import zlib
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from xml.etree import ElementTree as _ET

from ._native import _core

if TYPE_CHECKING:
    # In any real install the module is there, and assuming so keeps `_sigv4.sign`
    # and friends precisely typed at the S3 call sites below.
    from . import _sigv4
    from .request import Request
    from .response import ProblemResponse, Response
else:
    try:
        from .response import ProblemResponse, Response
    except ImportError:
        # Optional for the same reason `_sigv4` is, and load-bearing for the
        # same test: `tests/storage/test_zip.py` execs this file by path when
        # the extension is not built, and a by-path load has no parent package
        # to import a sibling from. `zip_stream` and `MemoryObjectStore` are
        # what that path uses; only the resumable-upload surface below needs
        # the HTTP layer, and it is unreachable without a router to mount it on.
        ProblemResponse = Response = None

    try:  # normal package import
        from . import _sigv4
    except ImportError:
        # This module is import-clean enough to be loaded by path, without the
        # rest of the package and without the built extension, which is how the
        # zip framing is tested against the stdlib reader. That route has no
        # sibling to import, and it never reaches S3, so the signer is optional
        # here and required at every call site that uses it.
        _sigv4 = None

#: `list` is part of the public ObjectStore API (`await storage.list(prefix=...)`),
#: so inside the class bodies below the name resolves to that method rather than
#: to the builtin. `from __future__ import annotations` means runtime is
#: unaffected -- annotations there are never evaluated -- but the shadowing is
#: real, and anything that does evaluate them (`typing.get_type_hints`) would
#: get the method. Class-scope annotations spell the builtin as `_List`.
_List = list

_CHUNK = 1 << 16
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

__all__ = [
    "ObjectError",
    "ObjectStat",
    "ObjectStore",
    "ObjectPath",
    "MemoryObjectStore",
    "LocalObjectStore",
    "S3ObjectStore",
    "ZipExtractionLimits",
    "normalize_key",
    "zip_stream",
    "unzip_stream",
    "file_chunks",
    # Resumable uploads.
    "PARTIAL_UPLOAD",
    "MemoryUploadStore",
    "ObjectUploadStore",
    "ResumableUploads",
    "UploadLimits",
    "UploadState",
    "UploadStore",
    "resumable",
    "sniff_content_type",
]


@dataclass(frozen=True, slots=True)
class ZipExtractionLimits:
    """Hard resource ceilings for one `unzip_stream` extraction.

    The archive and each entry are buffered because the stdlib ZIP reader needs
    seekable input and verifies an entry after decompression. These limits make
    both allocations explicit and also bound entry-count work and total output.
    """

    max_archive_bytes: int = 16 * 1024 * 1024
    max_entries: int = 1024
    max_entry_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_archive_bytes",
            "max_entries",
            "max_entry_bytes",
            "max_total_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


_DEFAULT_ZIP_EXTRACTION_LIMITS = ZipExtractionLimits()


class ObjectError(Exception):
    """The one exception every backend raises for every failed operation.

    Covers a key this module refuses (`normalize_key`), an object that is
    not there, a containment escape a local store caught, and a non-success
    status from S3. One type across the backends is what lets a handler written
    against `MemoryObjectStore` catch the same thing in production
    against `S3ObjectStore`.

    Transport failures below the storage layer are *not* converted: a connection
    that never reached S3 raises `wreath.http_client.ClientError` or `OSError`,
    because "the object is missing" and "the network is down" call for different
    responses and collapsing them loses that.
    """


#: The characters `normalize_key` refuses inside a segment: the C0 controls
#: (`\x00`-`\x1f`) and `DEL`. Compiled once, at import, and searched once per
#: segment -- the predicate it replaces read `any(ord(c) < 0x20 or ord(c) ==
#: 0x7F for c in part)`, one interpreter step per character of every key on the
#: containment gate every backend routes through. Ablated over the whole call
#: (15 rounds, 20k iterations, A/A floor 0.00-0.02us): 1.10 -> 0.40us on a
#: 15-character key, 2.74 -> 0.75us on 45, 4.78 -> 1.16us on 85. The class of
#: win is deleting work rather than widening it -- one C-level scan over the
#: segment instead of a Python loop over its characters -- so there is nothing
#: here for `_native/`. The set is literal code points, not a shorthand class,
#: so it is exactly the predicate and not a Unicode-aware approximation of it.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def normalize_key(key: str) -> str:
    """Return the canonical form of `key`, or refuse it.

    The single containment gate. Every method on every backend calls it before
    the key reaches a filesystem or a URL, so a traversal is refused once here
    rather than four times in four backends.

    Empty and `.` segments are collapsed, and a backslash is treated as a
    separator, so a key that arrived from a Windows client normalises to the
    same value it would have anywhere else. The result always uses `/`, on
    every platform, because a key is not a path -- it is what an S3 bucket will
    be asked for.

    Refused, each as an `ObjectError`: a key that is not a string, an empty key,
    an absolute key, a key containing a `..` segment, one containing a C0
    control character or `DEL`, and one that collapses to nothing (`"."`).
    A non-string says so and names the type it got: `None` arriving here is
    almost always a field that was never populated, and reporting it as an
    empty key sends the reader looking for the wrong bug.

    A `..` segment is refused rather than resolved. Resolving it would make
    `a/../b` and `b` the same object, so a caller that built a key by
    concatenating request data would silently address something it did not name.

    Args:
        key: the object key to canonicalise.

    Returns:
        The key with `//` and `.` segments collapsed and separators as `/`.

    Raises:
        ObjectError: for any of the refusals above.
    """
    if not isinstance(key, str):
        raise ObjectError(f"object key must be a string, not {type(key).__name__}")
    if not key:
        raise ObjectError("empty object key")
    if key.startswith("/"):
        raise ObjectError(f"absolute object key not allowed: {key!r}")
    parts = []
    for part in key.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ObjectError(f"object key escapes the store: {key!r}")
        if _CONTROL_CHARS.search(part):
            raise ObjectError(f"control character in object key: {key!r}")
        parts.append(part)
    if not parts:
        raise ObjectError(f"object key resolves to nothing: {key!r}")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ObjectStat:
    """What a backend knows about one object without reading its bytes.

    **An etag is unquoted in every backend, and comparable only against another
    stat from the same backend.** The quotes an HTTP `ETag` header carries are a
    wire detail, so they are stripped on the way in and a caller emitting the
    header adds them back; what a backend puts *inside* them still differs.
    `MemoryObjectStore` uses an MD5 of the content, `LocalObjectStore` an
    `mtime-size` pair -- cheap, and not a content hash -- and `S3ObjectStore`
    whatever S3 returned. Comparing two stats from the same backend is
    meaningful; comparing across backends is not, and used to fail for the
    additional and uninteresting reason that only one of them was quoted.

    `last_modified` is `None` wherever the backend does not supply one:
    `MemoryObjectStore` never does, and `S3ObjectStore.list` does not either,
    since only a `HEAD` carries the header. `size` is `-1` only from
    `S3ObjectStore.write_stream`, when a multipart upload completed but the
    confirming `HEAD` did not answer.

    Args:
        key: the object's normalised key.
        size: length in bytes.
        etag: the backend's version tag, in the backend's own format.
        last_modified: POSIX timestamp, when the backend supplies one.
        content_type: the media type, when the backend records or guesses one.
    """

    key: str
    size: int
    etag: str
    last_modified: float | None = None
    content_type: str | None = None


@runtime_checkable
class ObjectStore(Protocol):
    """The contract every backend satisfies, and the type to annotate against.

    Runtime-checkable, so `isinstance(store, ObjectStore)` holds for anything
    with these methods -- which is a structural check on names only, not on
    signatures. Annotate a handler with this rather than with a concrete backend
    and the same code runs against memory in a test and S3 in production.

    Every method normalises the key it is given, so a caller never has to. The
    keys a store *returns* are always already normalised.
    """

    async def read(self, key: str) -> bytes:
        """The whole object as bytes. Raises `ObjectError` when it is absent."""
        ...

    def read_stream(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
        """Stream the object in chunks, so a large one never has to fit in memory.

        Args:
            key: the object to read.
            range: an inclusive `(first, last)` byte range, as HTTP spells one.
        """
        ...

    async def write(
        self, key: str, data: bytes | bytearray | memoryview, *, content_type: str | None = None
    ) -> ObjectStat:
        """Store `data` under `key`, replacing whatever was there.

        **`data` may be any of `bytes`, `bytearray` or `memoryview`, and no
        backend keeps a reference to it past the `await`.** A caller may reuse
        or mutate the buffer as soon as the call returns; a backend that wanted
        to hold on to one would have to copy it itself.

        The parameter was `bytes` alone, which forced every caller assembling an
        object in a `bytearray` to freeze it first. That copy is pure `memcpy`
        and measured 391us on an 8 MiB append -- roughly half of what an append
        costs -- so it was work being done for nobody: no backend needed the
        immutability. Widening an *input* is backwards compatible, and an
        existing implementation annotated `bytes` still satisfies this at
        runtime; the contract it now has to honour is the no-retention sentence
        above, which all three backends here already did.
        """
        ...

    async def write_stream(
        self, key: str, chunks: AsyncIterable[bytes], *, content_type: str | None = None
    ) -> ObjectStat:
        """Store an async stream of chunks under `key`, without buffering it whole."""
        ...

    async def stat(self, key: str) -> ObjectStat:
        """Metadata for one object. Raises `ObjectError` when it is absent."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether `key` is present, without transferring the object."""
        ...

    def list(
        self, prefix: str = "", *, delimiter: str | None = None
    ) -> AsyncIterator[ObjectStat]:
        """Yield the objects whose key starts with `prefix`, in key order.

        `delimiter` is honoured only by `S3ObjectStore`, which passes it
        to the bucket; the local and memory backends ignore it. No backend
        surfaces the common prefixes a delimiter groups -- only the objects.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove `key`. Deleting an object that is not there is not an error."""
        ...

    def url(self, key: str, *, expires: int = 3600, method: str = "GET") -> str:
        """A signed URL granting `method` on `key`, minted without a round trip.

        Args:
            key: the object the URL grants access to.
            expires: lifetime in **seconds from now**, not an absolute time.
            method: the HTTP method authorised. A `GET` URL will not serve a `PUT`.
        """
        ...

    def path(self, key: str) -> ObjectPath:
        """An `ObjectPath` bound to this store and `key`."""
        ...


class ObjectPath:
    """An immutable handle to one key in one store, shaped like `pathlib.Path`.

    Joins with `/`, inspects with `name`/`suffix`/`parent`, and moves
    bytes with `read_bytes`/`write_bytes`/`open`. The store
    travels with the handle, so a function can take a path and need to know
    nothing about which backend is behind it:

    ```python
    assets = store.path("assets")
    await (assets / "2026" / "logo.svg").write_bytes(svg, content_type="image/svg+xml")
    ```

    `""` and `"/"` both mean the store's root, which is the one key
    `normalize_key` would refuse -- a root handle is the natural starting
    point for joining and for `iterdir`, so it is admitted here and only
    here. Every other key goes through the gate at construction.

    Two paths are equal when they name the same key **in the same store object**
    (compared by identity), because the same key in two different buckets is not
    the same object and treating it as one would let a cache serve the wrong
    bytes.

    Nothing here is cached: a path is a name, not a snapshot, so
    `exists` and `read_bytes` always ask the store.
    """

    __slots__ = ("_storage", "_key")

    def __init__(self, storage: ObjectStore, key: str) -> None:
        self._storage = storage
        self._key = normalize_key(key) if key not in ("", "/") else ""

    @property
    def key(self) -> str:
        """The normalised key, or `""` for the store's root."""
        return self._key

    @property
    def name(self) -> str:
        """The last segment of the key, like `pathlib.PurePath.name`."""
        return self._key.rsplit("/", 1)[-1]

    @property
    def suffix(self) -> str:
        """The final extension including the dot, or `""` when there is none.

        A leading dot does not begin an extension, so `.gitignore` has no
        suffix -- the same rule `pathlib.PurePath.suffix` follows.
        """
        name = self.name
        dot = name.rfind(".")
        return name[dot:] if dot > 0 else ""

    @property
    def parent(self) -> ObjectPath:
        """The path one segment up, in the same store.

        The root is its own parent, so walking upwards terminates rather than
        producing a key that means something else.
        """
        head = self._key.rsplit("/", 1)[0] if "/" in self._key else ""
        return ObjectPath(self._storage, head)

    def __truediv__(self, other: str) -> ObjectPath:
        joined = f"{self._key}/{other}" if self._key else other
        return ObjectPath(self._storage, joined)

    def __repr__(self) -> str:
        return f"ObjectPath({self._key!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ObjectPath)
            and other._key == self._key
            and other._storage is self._storage
        )

    def __hash__(self) -> int:
        return hash((id(self._storage), self._key))

    async def read_bytes(self) -> bytes:
        """The whole object as bytes.

        Raises:
            ObjectError: when the object is not there.
        """
        return await self._storage.read(self._key)

    async def write_bytes(
        self, data: bytes | bytearray | memoryview, *, content_type: str | None = None
    ) -> ObjectStat:
        """Store `data` at this key, replacing whatever was there.

        Args:
            data: the object's new contents, as `bytes`, `bytearray` or
                `memoryview`. The backend retains none of it; see
                `ObjectStore.write`.
            content_type: the media type. What a backend does with it varies; see each.

        Returns:
            The stat of the object just written.
        """
        return await self._storage.write(self._key, data, content_type=content_type)

    async def exists(self) -> bool:
        """Whether an object is stored at this key, asked afresh each call."""
        return await self._storage.exists(self._key)

    async def unlink(self) -> None:
        """Delete this key. Not an error when it is already gone."""
        await self._storage.delete(self._key)

    async def iterdir(self) -> AsyncIterator[ObjectPath]:
        """Every object beneath this path, **recursively**, in key order.

        The name is `pathlib`'s and the behaviour is deliberately not: object
        stores have no directories, only keys with slashes in them, so there is
        no such thing as a direct child to stop at. This yields every key under
        the prefix at any depth. For one level only, pass `delimiter="/"` to
        `ObjectStore.list` -- on S3, the one backend that honours it.
        """
        prefix = f"{self._key}/" if self._key else ""
        async for obj in self._storage.list(prefix):
            yield ObjectPath(self._storage, obj.key)

    async def glob(self, pattern: str) -> AsyncIterator[ObjectPath]:
        """Every object beneath this path whose key matches `pattern`.

        Matching is `fnmatch.fnmatch` over the whole key, so **`*` crosses
        `/`**: under `reports/`, the pattern `*.csv` matches
        `reports/2026/q3.csv` as well as `reports/summary.csv`. That is not
        `pathlib`'s rule, where `*` stops at a separator and `**` is needed
        to descend; here there is no separator to stop at, only a key.

        The filter is applied in this process, after the store has listed the
        prefix. A pattern is not a cheaper listing -- it is the same listing
        with rows discarded -- so narrow the path rather than the pattern when
        the prefix is large.
        """
        prefix = f"{self._key}/" if self._key else ""
        full = f"{prefix}{pattern}"
        async for obj in self._storage.list(prefix):
            if fnmatch.fnmatch(obj.key, full):
                yield ObjectPath(self._storage, obj.key)

    def presigned_url(self, *, expires: int = 3600, method: str = "GET") -> str:
        """A signed URL for this key. See `ObjectStore.url`."""
        return self._storage.url(self._key, expires=expires, method=method)

    def open(self, mode: str = "rb") -> _PathIO:
        """An async context manager over this object.

        `"rb"` iterates chunks and answers `.read()` with the whole object;
        `"wb"` collects `.write()` calls in memory and stores them as one
        object when the block ends normally. A `"wb"` block therefore holds the
        whole object -- for a large one, call `ObjectStore.write_stream`
        directly. A block that raises stores nothing.

        There is no append and no read-write mode: an object store replaces
        objects whole, so either would be a shape this cannot honour.

        Args:
            mode: `"rb"` or `"wb"`.

        Raises:
            ObjectError: for any other mode, including `"r"`, `"w"` and `"a"`.
        """
        return _PathIO(self, mode)


class _PathIO:
    """Convenience async context manager over a `ObjectPath`.

    `"rb"` iterates chunks (and `.read()` returns the whole object); `"wb"`
    buffers writes and flushes on exit. Note: `"wb"` buffers — for true streaming
    writes use `ObjectStore.write_stream` directly.

    **A `"wb"` block that raises stores nothing.** The flush happens only on a
    normal exit, so a body that failed half-way through does not leave a
    truncated object under the real key for the next reader to find -- there is
    no partial write here to distinguish from a complete one, and an object
    store replaces objects whole. Catch the exception inside the block if a
    partial object is genuinely what you want.
    """

    __slots__ = ("_path", "_mode", "_buf")

    def __init__(self, path: ObjectPath, mode: str) -> None:
        if mode not in ("rb", "wb"):
            raise ObjectError(f"unsupported mode {mode!r}")
        self._path = path
        self._mode = mode
        self._buf: list[bytes] = []

    async def __aenter__(self) -> _PathIO:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._mode == "wb" and exc_type is None:
            await self._path.write_bytes(b"".join(self._buf))
        self._buf.clear()

    async def read(self) -> bytes:
        return await self._path.read_bytes()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._path._storage.read_stream(self._path.key):
            yield chunk

    def write(self, data: bytes) -> None:
        self._buf.append(data)


def _now() -> float:
    """The wall clock the local and memory presigners measure expiry against.

    A function rather than a direct `time.time()` call so a test can move the
    clock without sleeping; nothing else should replace it.
    """
    return time.time()


def _checked_secret(url_secret: object) -> bytes | None:
    """Refuse a non-`bytes` HMAC key where it is accepted, not where it is used.

    `Wreath.objects()` takes `**options`, so an annotation alone does not stop a
    `str` reaching here. Left unchecked it survives construction and raises from
    inside `hmac.new` on the first `url()` call, naming neither the option nor
    the registration that supplied it.

    Raises:
        TypeError: `url_secret` is neither `None` nor a bytes-like object.
    """
    if url_secret is None or isinstance(url_secret, bytes | bytearray | memoryview):
        return None if url_secret is None else bytes(url_secret)
    kind = type(url_secret).__name__
    hint = " -- encode it, e.g. url_secret=secret.encode()" if kind == "str" else ""
    raise TypeError(f"url_secret must be bytes, not {kind}{hint}")


def _sign_local(secret: bytes, method: str, key: str, deadline: int) -> str:
    """HMAC over the method, the key, and the **absolute** expiry timestamp."""
    msg = f"{method.upper()}\n{key}\n{deadline}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _verify_local(
    secret: bytes, key: str, *, method: str, expires: int, signature: str
) -> bool:
    """Whether `signature` is authentic for `key` and the deadline has not passed."""
    # `isascii()` before the compare, and it is load-bearing rather than belt
    # and braces: `hmac.compare_digest` **raises** `TypeError` on a `str`
    # carrying a non-ASCII character. The signature is a query parameter, so
    # without this a `?signature=é` answers 500 instead of 403 -- unauthenticated
    # input turning a refusal into an error. `_secondfactor.py`'s TOTP guard
    # exists for the same hazard and names the same consequence.
    if not signature.isascii():
        return False
    expected = _sign_local(secret, method, normalize_key(key), expires)
    return hmac.compare_digest(expected, signature) and _now() <= expires


class MemoryObjectStore:
    """A store in a dict: the test and development twin of a real backend.

    Satisfies `ObjectStore` completely, so a handler written against the
    protocol runs unchanged against this in a test and against S3 in production
    -- including key normalisation, which is the check most hand-rolled fakes
    leave out and so the one a test with a fake never catches.

    Everything lives in one process's memory, for the lifetime of the object.
    Nothing is bounded and nothing expires: a test that writes a gigabyte holds
    a gigabyte. There is no locking either, which costs nothing here because
    every method's mutation is a single dict operation and there is no `await`
    inside one for another task to interleave at.
    """

    def __init__(self, *, url_secret: bytes | None = None) -> None:
        self._objects: dict[str, tuple[bytes, str | None]] = {}
        self._secret = _checked_secret(url_secret) or os.urandom(32)

    async def read(self, key: str) -> bytes:
        """The whole object as bytes.

        Raises:
            ObjectError: when `key` is absent or is not a valid key.
        """
        key = normalize_key(key)
        try:
            return self._objects[key][0]
        except KeyError:
            raise ObjectError(f"no such object: {key!r}") from None

    async def read_stream(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
        """Yield the object in 64 KiB chunks.

        The chunking is for shape, not for memory: the object is already whole
        in this process, so this slices what it has. It exists so the calling
        code is the same code that streams from S3. An empty object yields no
        chunks, which is what the other two backends do with one.

        Args:
            key: the object to read.
            range: an inclusive `(first, last)` byte range, clamped by plain slicing.
        """
        data = await self.read(key)
        if range is not None:
            start, end = range
            data = data[start : end + 1]
        for chunk in _iter_chunks(data):
            yield chunk

    async def write(
        self, key: str, data: bytes | bytearray | memoryview, *, content_type: str | None = None
    ) -> ObjectStat:
        """Store `data` under `key`, replacing whatever was there.

        The bytes are copied on the way in, so this backend retains nothing of
        the caller's buffer and a `bytearray` may be reused the moment the call
        returns. `content_type` is recorded and returned by `stat` -- this is
        the only backend that stores it verbatim.
        """
        key = normalize_key(key)
        stored = bytes(data)
        self._objects[key] = (stored, content_type)
        return ObjectStat(key, len(stored), _etag(stored), None, content_type)

    async def write_stream(
        self,
        key: str,
        chunks: AsyncIterable[bytes | bytearray | memoryview],
        *,
        content_type: str | None = None,
    ) -> ObjectStat:
        """Drain `chunks` and store the result. Buffers the whole object."""
        buf = bytearray()
        async for chunk in chunks:
            buf += chunk
        # `write` freezes what it stores, so handing it the `bytearray` costs
        # one copy rather than two.
        return await self.write(key, buf, content_type=content_type)

    async def stat(self, key: str) -> ObjectStat:
        """Metadata for one object, with `last_modified` always `None`.

        There is no clock here on purpose: a fake that invented modification
        times would let a test assert on ordering this backend cannot actually
        guarantee.

        Raises:
            ObjectError: when `key` is absent or is not a valid key.
        """
        key = normalize_key(key)
        try:
            data, ct = self._objects[key]
        except KeyError:
            raise ObjectError(f"no such object: {key!r}") from None
        return ObjectStat(key, len(data), _etag(data), None, ct)

    async def exists(self, key: str) -> bool:
        """Whether `key` is present.

        An unusable key is a caller's mistake rather than an absent object, so it
        is refused rather than reported as `False`.

        Raises:
            ObjectError: when `key` is not a valid key.
        """
        return normalize_key(key) in self._objects

    async def list(
        self, prefix: str = "", *, delimiter: str | None = None
    ) -> AsyncIterator[ObjectStat]:
        """Every object whose key starts with `prefix`, in key order.

        `prefix` is matched literally against normalised keys and is *not*
        itself normalised, so it can end mid-segment (`"report"` matches
        `"reports/q3.csv"`) exactly as an S3 prefix does. `delimiter` is
        accepted for protocol compatibility and ignored.
        """
        for key in sorted(self._objects):
            if key.startswith(prefix):
                data, ct = self._objects[key]
                yield ObjectStat(key, len(data), _etag(data), None, ct)

    async def delete(self, key: str) -> None:
        """Remove `key`. Not an error when it is already gone."""
        self._objects.pop(normalize_key(key), None)

    def url(self, key: str, *, expires: int = 3600, method: str = "GET") -> str:
        """A `memory://` URL signed with this store's secret.

        Nothing serves it -- the scheme is unroutable on purpose, so a test that
        accidentally hands one to a real HTTP client fails rather than reaching
        somewhere. It exists so the presign call site is exercised, and so a
        route's verification can be tested against `verify_local_url` here
        exactly as it will run against `LocalObjectStore`.

        The query's `expires` is the **absolute** UNIX time the URL stops being
        valid -- `expires=900` here means "valid for the next 900 seconds", and
        what lands in the URL is that deadline. See `LocalObjectStore.url`.

        Args:
            key: the object the URL grants access to.
            expires: lifetime in seconds from now; the URL carries the deadline.
            method: the method authorised. Case-insensitive, and part of the signature.
        """
        key = normalize_key(key)
        deadline = int(_now()) + expires
        sig = _sign_local(self._secret, method, key, deadline)
        return f"memory:///{key}?expires={deadline}&signature={sig}"

    def verify_local_url(self, key: str, *, method: str, expires: int, signature: str) -> bool:
        """Whether a URL from `url` is authentic and still inside its deadline.

        Identical in behaviour and arithmetic to
        `LocalObjectStore.verify_local_url`, so a route that checks presigned
        URLs can be tested against this store and deployed against that one.

        Args:
            key: the key from the URL's path.
            method: the request's method.
            expires: the URL's `expires` value -- an absolute UNIX time.
            signature: the URL's `signature` value.

        Returns:
            Whether the signature matches and the deadline is still ahead.

        Raises:
            ObjectError: when `key` is not a valid key.
        """
        return _verify_local(
            self._secret, key, method=method, expires=expires, signature=signature
        )

    def path(self, key: str) -> ObjectPath:
        """An `ObjectPath` bound to this store and `key`."""
        return ObjectPath(self, key)


def _iter_chunks(data: bytes) -> Iterable[bytes]:
    # An empty object yields nothing at all, which is what the other two
    # backends do; yielding one empty chunk instead made "the stream ended"
    # and "the object is empty" different shapes depending on the backend.
    for i in range(0, len(data), _CHUNK):
        yield data[i : i + _CHUNK]


def _etag(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


#: The exact shape `LocalObjectStore.write_stream` gives its temporary file, and
#: the shape `_walk` skips so a killed write never shows up as an object. Both
#: sides come from here on purpose: when the writer and the filter were allowed
#: to spell it separately, only one of them was right.
_TMP_NAME = re.compile(r"\..+\.[0-9a-f]{12}\.tmp")


def _tmp_name(name: str) -> str:
    return f".{name}.{os.urandom(6).hex()}.tmp"


class LocalObjectStore:
    """A directory on disk, confined beneath a root that cannot be escaped.

    Containment is enforced by descriptor, not by string comparison: the root is
    opened once at construction and every read resolves relative to that
    descriptor through `wreath._fsguard`'s `openat` walk -- the same
    machinery static-file serving uses. A key that resolves outside the root, or
    that traverses a symlink on the way, is refused. Comparing resolved paths
    instead would be a check with a window in it: the path can be replaced
    between the check and the open.

    Writes land on a temporary file beside the target and arrive by
    `os.replace`, so a reader sees the old object or the new one and never a
    partial one. **The rename's durability is not guaranteed:** the file's
    contents are `fsync`'d before the rename, but the containing directory is
    not, so a machine that loses power immediately afterwards can come back with
    the object missing. Its contents are never torn.

    The root is created if it does not exist. Call `close` to release the
    root descriptor -- `wreath.app.Wreath.objects` does it on shutdown.

    Omitting `url_secret` generates a fresh random one, so signed URLs do not
    survive a restart and are not valid across workers. Pass one explicitly for
    anything but a single process.

    Args:
        root: the directory everything lives beneath. Created if absent.
        url_secret: the HMAC key `url` signs with. Random when omitted.
    """

    def __init__(self, root: str | os.PathLike[str], *, url_secret: bytes | None = None) -> None:
        # Checked before the root is opened: a refusal after `open_root` would
        # leak the descriptor, since the half-built store is discarded and its
        # `close` never runs.
        secret = _checked_secret(url_secret)
        self._root = os.fspath(root)
        os.makedirs(self._root, exist_ok=True)
        # Local imports keep this module import-clean for standalone zip/sigv4 testing.
        from ._fsguard import open_root

        self._root_fd = open_root(self._root)
        self._secret = secret or os.urandom(32)

    def close(self) -> None:
        """Release the root descriptor. Safe to call more than once.

        The descriptor is forgotten before it is closed, so a second call does
        nothing at all. That matters more than it looks: a file descriptor
        number is reused by the very next `open` in the process, so closing a
        remembered number twice can close a completely unrelated file that
        happened to inherit it -- a bug that surfaces far away from here, as an
        `EBADF` on something that was never touched.

        A store is unusable afterwards; every operation resolves against that
        descriptor.
        """
        fd, self._root_fd = self._root_fd, -1
        if fd >= 0:
            os.close(fd)

    # -- read ---------------------------------------------------------------
    async def read(self, key: str) -> bytes:
        """The whole object as bytes, assembled from `read_stream`.

        Raises:
            ObjectError: when the object is absent, escapes the root, or crosses a symlink.
        """
        buf = bytearray()
        async for chunk in self.read_stream(key):
            buf += chunk
        return bytes(buf)

    async def read_stream(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
        """Yield the object in 64 KiB chunks, off a thread.

        Every file-system call runs through `asyncio.to_thread`, because a read
        from a slow disk or a network mount would otherwise block the event loop
        for every other request in the process. The descriptor is closed when
        the iterator is exhausted, and by the generator's `aclose` when a
        consumer stops early.

        Args:
            key: the object to read.
            range: an inclusive `(first, last)` byte range, clamped to the size.

        Raises:
            ObjectError: when the object is absent, escapes the root, or crosses a symlink.
        """
        key = normalize_key(key)
        from ._fsguard import ContainmentError, open_beneath

        try:
            fd, st = await asyncio.to_thread(open_beneath, self._root_fd, key)
        except ContainmentError as exc:
            raise ObjectError(str(exc)) from exc
        except FileNotFoundError:
            raise ObjectError(f"no such object: {key!r}") from None
        try:
            remaining = st.st_size
            if range is not None:
                start, end = range
                await asyncio.to_thread(os.lseek, fd, start, os.SEEK_SET)
                remaining = min(end + 1, st.st_size) - start
            while remaining > 0:
                chunk = await asyncio.to_thread(os.read, fd, min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(os.close, fd)

    # -- write --------------------------------------------------------------
    async def write(
        self, key: str, data: bytes | bytearray | memoryview, *, content_type: str | None = None
    ) -> ObjectStat:
        """Store `data` at `key` atomically, without copying it. See `write_stream`."""
        async def _one() -> AsyncIterator[bytes | bytearray | memoryview]:
            yield data

        return await self.write_stream(key, _one(), content_type=content_type)

    async def write_stream(
        self,
        key: str,
        chunks: AsyncIterable[bytes | bytearray | memoryview],
        *,
        content_type: str | None = None,
    ) -> ObjectStat:
        """Stream `chunks` to `key`, atomically and without buffering.

        Parent directories are created as needed, each one opened with
        `O_NOFOLLOW` so a symlink planted in the path is refused rather than
        followed out of the root. Bytes go to a hidden temporary file in the
        target's own directory, are `fsync`'d, and then `os.replace` puts them
        at the key -- so a concurrent reader gets the old object or the new one.

        A failure anywhere, including cancellation, unlinks the temporary file
        before the exception continues. Only a hard kill leaves one behind, and
        it is inert: nothing ever reads it back, and `list` skips its
        `.<name>.<hex>.tmp` shape rather than reporting a key nobody wrote. It
        does still occupy disk until something removes it.

        **`content_type` is not persisted.** A filesystem has nowhere to keep it,
        so a later `stat` guesses from the key's extension instead; a media type
        the extension does not imply survives only as far as this call's return
        value.

        Args:
            key: where to store the object.
            chunks: the object's contents, as `bytes`, `bytearray` or
                `memoryview`. Each is written straight to the file descriptor
                and none is retained. Empty chunks are skipped.
            content_type: echoed into the returned stat, and stored nowhere.

        Returns:
            The stat of the object as it landed, read back with `os.stat`.

        Raises:
            ObjectError: when the key is invalid or a path component is a symlink.
            OSError: from the filesystem -- a full disk, a permission failure.
        """
        key = normalize_key(key)
        parts = key.split("/")
        parent_fd, opened, name = await asyncio.to_thread(self._open_parent, parts, True)
        tmp = _tmp_name(name)
        size = 0
        try:
            fd = await asyncio.to_thread(self._open_new, parent_fd, tmp)
            try:
                async for chunk in chunks:
                    if chunk:
                        await asyncio.to_thread(_write_all, fd, chunk)
                        size += len(chunk)
                await asyncio.to_thread(os.fsync, fd)
            finally:
                await asyncio.to_thread(os.close, fd)
            await asyncio.to_thread(
                os.replace, tmp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
            st = await asyncio.to_thread(os.stat, name, dir_fd=parent_fd, follow_symlinks=False)
        except BaseException:
            try:
                await asyncio.to_thread(os.unlink, tmp, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            for fd_ in opened:
                os.close(fd_)
        ct = content_type or mimetypes.guess_type(key)[0]
        return ObjectStat(key, st.st_size, _fs_etag(st), st.st_mtime, ct)

    @staticmethod
    def _open_new(parent_fd: int, name: str) -> int:
        return os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
            0o644,
            dir_fd=parent_fd,
        )

    def _open_parent(self, parts: _List[str], create: bool) -> tuple[int, _List[int], str]:
        """Walk (optionally creating) parent dirs beneath the root, refusing symlinks.

        Returns `(parent_fd, opened_fds_to_close, final_name)`; `parent_fd` is the
        last of `opened_fds` (or the root fd when the key is top-level).
        """
        opened: list[int] = []
        current = self._root_fd
        try:
            for part in parts[:-1]:
                try:
                    if _stat.S_ISLNK(os.lstat(part, dir_fd=current).st_mode):
                        raise ObjectError(f"refusing symlink component {part!r}")
                except FileNotFoundError:
                    if not create:
                        raise ObjectError(f"no such directory component {part!r}") from None
                    os.mkdir(part, 0o755, dir_fd=current)
                fd = os.open(
                    part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=current
                )
                opened.append(fd)
                current = fd
        except BaseException:
            for fd in opened:
                os.close(fd)
            raise
        return current, opened, parts[-1]

    # -- metadata / listing -------------------------------------------------
    async def stat(self, key: str) -> ObjectStat:
        """Metadata for one object.

        The etag is `<mtime_ns hex>-<size hex>`, not a hash of the content:
        hashing would mean reading the whole object to answer a metadata
        question. It changes whenever the object is rewritten, which is what an
        etag is for, but it is not comparable with any other backend's.

        The content type is guessed from the key's extension by `mimetypes`,
        because nothing was persisted at write time.

        Raises:
            ObjectError: when the object is absent, escapes the root, or crosses a symlink.
        """
        key = normalize_key(key)
        from ._fsguard import ContainmentError, open_beneath

        try:
            fd, st = await asyncio.to_thread(open_beneath, self._root_fd, key)
        except ContainmentError as exc:
            raise ObjectError(str(exc)) from exc
        except FileNotFoundError:
            raise ObjectError(f"no such object: {key!r}") from None
        os.close(fd)
        return ObjectStat(key, st.st_size, _fs_etag(st), st.st_mtime, mimetypes.guess_type(key)[0])

    async def exists(self, key: str) -> bool:
        """Whether an object is stored at `key`.

        `False` covers every refusal, not only absence: a key that escapes the
        root or crosses a symlink is reported as not present, because from a
        caller's side there is nothing there to read either way.
        """
        try:
            await self.stat(key)
            return True
        except ObjectError:
            return False

    async def list(
        self, prefix: str = "", *, delimiter: str | None = None
    ) -> AsyncIterator[ObjectStat]:
        """Every object whose key starts with `prefix`, in key order.

        The whole tree beneath the root is walked once, on a thread, before the
        first result is yielded -- so this is not incremental, and its cost is
        the size of the tree rather than of the prefix. Symlinks are skipped and
        never followed, both for entries and for directories, so the listing
        cannot leave the root.

        A key that vanishes between the walk and its stat is dropped silently
        rather than raising: a listing that races a delete should report what is
        there, not fail.

        `delimiter` is accepted for protocol compatibility and ignored; this
        backend always lists recursively.

        Every regular file under the root is an object here **except a write's
        own temporary**, which is skipped: a file matching the exact shape
        `write_stream` gives one (`.<name>.<12 hex digits>.tmp`) is left behind
        only by a write that was killed outright, and reporting it would put a
        key in the listing that no caller ever wrote. The pattern is deliberately
        narrow rather than "hidden files" or "anything ending in `.tmp`", so an
        object a caller genuinely named `notes.tmp` -- or `.env` -- is still
        listed. A key that happens to collide with the pattern is invisible
        here, though `read` and `stat` still answer for it.
        """
        entries = await asyncio.to_thread(self._walk)
        for key in entries:
            if key.startswith(prefix):
                try:
                    yield await self.stat(key)
                except ObjectError:
                    continue

    def _walk(self) -> _List[str]:
        return _core.local_walk(self._root, os.scandir, os.path.join)

    async def delete(self, key: str) -> None:
        """Remove `key`. Not an error when it is already gone.

        A missing parent directory counts as already gone, as does a missing
        file, as does a path component that is a symlink -- the containment
        refusal reaches the same conclusion a caller wanted, which is that
        nothing is stored at that key afterwards. Empty directories left behind
        by the last object under them are not removed: pruning them would race a
        concurrent write creating the same path.

        Raises:
            ObjectError: when the key itself is not a valid key.
            OSError: for any filesystem failure other than "not there".
        """
        key = normalize_key(key)
        parts = key.split("/")
        try:
            parent_fd, opened, name = await asyncio.to_thread(self._open_parent, parts, False)
        except ObjectError:
            return
        try:
            await asyncio.to_thread(os.unlink, name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        finally:
            for fd in opened:
                os.close(fd)

    def url(self, key: str, *, expires: int = 3600, method: str = "GET") -> str:
        """A signed **relative** path: `/<key>?expires=…&signature=…`.

        There is nothing to redirect a browser to on a local disk, so this is
        half of a presign: wreath mounts no route for it, and the application
        serves the object from a route of its own after checking the signature
        with `verify_local_url`. That keeps a development handler and its
        S3 counterpart the same call -- `store.url(...)` -- with only the
        route behind it differing.

        **The URL carries a deadline, not a lifetime.** `expires` is seconds
        from now, as it is on every backend, but what lands in the query string
        is the absolute UNIX time the URL stops working -- and that timestamp is
        covered by the HMAC, so it cannot be edited to buy more time.
        `verify_local_url` refuses a URL past it. Carrying the deadline rather
        than the issue time is what makes the check possible at all with no
        state kept between the two calls, and it is how SigV2 presigning spelled
        the same idea.

        The clock that matters is this process's, not the client's, since the
        deadline is compared here on the way back in. S3 differs in mechanism --
        SigV4 signs the issue time and AWS applies the window -- but agrees in
        effect: neither depends on the client's clock.

        Rotating `url_secret` still invalidates every outstanding URL at once,
        which is the lever to reach for when a deadline is too far away.

        Args:
            key: the object the URL grants access to.
            expires: lifetime in seconds from now; the URL carries the deadline.
            method: the method authorised. Case-insensitive, and part of the signature.
        """
        key = normalize_key(key)
        deadline = int(_now()) + expires
        sig = _sign_local(self._secret, method, key, deadline)
        return f"/{key}?expires={deadline}&signature={sig}"

    def verify_local_url(self, key: str, *, method: str, expires: int, signature: str) -> bool:
        """Whether a URL from `url` is authentic and still inside its deadline.

        The route-side half of `url`: read `expires` and `signature`
        off the query string, pass them here with the key and the request's
        method, and serve the object only when this answers true. The signature
        comparison is `hmac.compare_digest`, so it does not leak the correct
        signature through its timing.

        Two things are checked. The signature must be this store's over the
        method, the key and the deadline -- so none of the three can be edited.
        The deadline must still be ahead of this process's clock, which is what
        makes `expires` mean something rather than merely being signed.

        Both a forged signature and an expired one answer `False`; a route that
        wants to tell a user which is which has to decide that on its own, and
        usually should not.

        Args:
            key: the key from the URL's path.
            method: the request's method.
            expires: the URL's `expires` value -- an absolute UNIX time.
            signature: the URL's `signature` value.

        Returns:
            Whether the signature matches and the deadline is still ahead.

        Raises:
            ObjectError: when `key` is not a valid key.
        """
        return _verify_local(
            self._secret, key, method=method, expires=expires, signature=signature
        )

    def path(self, key: str) -> ObjectPath:
        """An `ObjectPath` bound to this store and `key`."""
        return ObjectPath(self, key)


def _write_all(fd: int, data: bytes | bytearray | memoryview) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _fs_etag(st: os.stat_result) -> str:
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


# --------------------------------------------------------------------------- zip
_ZIP_FLAGS = 0x0808  # bit 3 (data descriptor) + bit 11 (UTF-8 filenames)
_DOS_DATE = 0x21  # 1980-01-01, fixed (deterministic, no wall clock)


async def zip_stream(storage: ObjectStore, keys: Iterable[str]) -> AsyncIterator[bytes]:
    """Stream a Zip64 archive of `keys` straight out of `storage`.

    Hand the result to a streaming response and a caller downloads a hundred
    objects as one file while the process holds none of them:

    ```python
    return StreamingResponse(
        zip_stream(store, keys),
        headers=[(b"content-type", b"application/zip")],
    )
    ```

    Each entry is **stored, not deflated** (method 0). Compression would mean
    knowing the compressed size before the local header is written, which is
    exactly the thing that forces the whole entry into memory; instead the sizes
    and the CRC32 are computed as the bytes go past and emitted afterwards in a
    data descriptor. So the archive is at least as large as its contents, and
    that is the price of never buffering one.

    The central directory always uses Zip64 fields, whatever the size, so there
    is no threshold at which the format changes and no untested branch that only
    a four-gigabyte archive would reach.

    Output is deterministic: every entry's modification time is a fixed
    1980-01-01, so the same keys over the same bytes produce byte-identical
    archives and a caller may cache or checksum one.

    Memory is bounded by the number of entries, not by their size -- one name,
    CRC, size and offset are retained per key to build the central directory at
    the end.

    **A missing key fails part-way through.** Entries are streamed as they are
    read, so the `ObjectError` for a key that is not there can arrive after bytes
    have already been yielded. A response that has begun cannot be turned into an
    error, so the client receives a truncated archive; check the keys exist
    before starting if that matters.

    Args:
        storage: the store the objects are read from.
        keys: the objects to include, in order. Each is normalised for its name.

    Yields:
        The archive's bytes, starting with the first entry's header.

    Raises:
        ObjectError: for a key that is not there, possibly mid-stream.
    """
    builder = _core.ZipBuilder(zlib.crc32)
    for key in keys:
        name = normalize_key(key).encode("utf-8")
        yield builder.begin(name)
        async for chunk in storage.read_stream(key):
            if not chunk:
                continue
            builder.feed(chunk)
            yield chunk
        yield builder.end()
    for trailer in builder.finish():
        yield trailer


def _zip_entry_count(raw: bytes, limit: int) -> int | None:
    """Count central-directory entries without constructing `ZipInfo` objects.

    A hostile archive can fit hundreds of thousands of empty-entry records into
    the archive-byte budget. Counting the fixed records first prevents the
    stdlib reader from materializing that object graph before `max_entries` is
    checked. Malformed framing is left for `zipfile` to diagnose.
    """
    eocd = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65_557))
    if eocd < 0 or eocd + 22 > len(raw):
        return None
    comment_bytes = struct.unpack_from("<H", raw, eocd + 20)[0]
    if eocd + 22 + comment_bytes != len(raw):
        return None

    directory_size = struct.unpack_from("<I", raw, eocd + 12)[0]
    directory_end = eocd
    locator = eocd - 20
    if locator >= 0 and raw[locator : locator + 4] == b"PK\x06\x07":
        zip64 = raw.rfind(b"PK\x06\x06", 0, locator)
        if zip64 < 0 or zip64 + 56 > locator:
            return None
        record_size = struct.unpack_from("<Q", raw, zip64 + 4)[0]
        if zip64 + 12 + record_size != locator:
            return None
        directory_size = struct.unpack_from("<Q", raw, zip64 + 40)[0]
        directory_end = zip64
    elif directory_size == 0xFFFFFFFF:
        return None

    directory_start = directory_end - directory_size
    if directory_start < 0:
        return None
    cursor = directory_start
    count = 0
    while cursor < directory_end:
        if cursor + 46 > directory_end or raw[cursor : cursor + 4] != b"PK\x01\x02":
            return None
        name_bytes, extra_bytes, comment_bytes = struct.unpack_from(
            "<HHH", raw, cursor + 28
        )
        cursor += 46 + name_bytes + extra_bytes + comment_bytes
        if cursor > directory_end:
            return None
        count += 1
        if count > limit:
            return count
    return count if cursor == directory_end else None


async def unzip_stream(
    storage: ObjectStore,
    key: str,
    *,
    prefix: str = "",
    limits: ZipExtractionLimits = _DEFAULT_ZIP_EXTRACTION_LIMITS,
) -> list[str]:
    """Expand a bounded zip object into one object per entry.

    The stdlib ZIP reader needs a seekable archive and verifies each entry after
    decompression, so this function buffers the archive and one entry at a time.
    `limits` bounds both allocations, the number of entries parsed, and the
    cumulative output before any destination is written.

    Directory entries are skipped -- an object store has no directories, so a
    zero-byte object named after one would be noise. Every destination key and
    every declared size is validated before extraction starts, so a refused
    archive leaves no partial output behind.

    Args:
        storage: where the archive is read from and the entries are written back.
        key: the archive object.
        prefix: prepended verbatim to each entry's name. Not a path join.
        limits: hard ceilings for archive bytes, entries, one entry, and total output.

    Returns:
        The normalised keys written, in archive order.

    Raises:
        ObjectError: when a resource limit or destination-key check is refused.
        zipfile.BadZipFile: when the object is not a readable zip.
    """
    import io
    import zipfile

    raw_buffer = bytearray()
    async for chunk in storage.read_stream(key):
        if len(chunk) > limits.max_archive_bytes - len(raw_buffer):
            raise ObjectError(
                f"zip archive exceeds {limits.max_archive_bytes} bytes"
            )
        raw_buffer += chunk
    raw = bytes(raw_buffer)

    declared_entries = _zip_entry_count(raw, limits.max_entries)
    if declared_entries is not None and declared_entries > limits.max_entries:
        raise ObjectError(
            f"zip archive has {declared_entries} entries; limit is {limits.max_entries}"
        )

    written: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise ObjectError(
                f"zip archive has {len(infos)} entries; limit is {limits.max_entries}"
            )

        planned: list[tuple[Any, str]] = []
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            dest = f"{prefix}{info.filename}" if prefix else info.filename
            normalized = normalize_key(dest)
            if info.file_size > limits.max_entry_bytes:
                raise ObjectError(
                    f"zip entry {info.filename!r} expands to {info.file_size} bytes; "
                    f"per-entry limit is {limits.max_entry_bytes}"
                )
            if info.file_size > limits.max_total_bytes - total:
                raise ObjectError(
                    f"zip output exceeds {limits.max_total_bytes} bytes at "
                    f"entry {info.filename!r}"
                )
            total += info.file_size
            planned.append((info, normalized))

        for info, dest in planned:
            await storage.write(dest, zf.read(info))
            written.append(dest)
    return written


async def file_chunks(data: bytes, *, chunk_size: int = 1 << 20) -> AsyncIterator[bytes]:
    """Adapt bytes already in hand to the stream `ObjectStore.write_stream` wants.

    ```python
    await store.write_stream(key, file_chunks(upload.content))
    ```

    **This saves no memory, and is not pretending to.** The argument is already
    a whole object in this process; slicing it merely gives `write_stream` the
    shape it takes, which is what lets an S3 store turn a large payload into a
    multipart upload instead of one enormous `PUT`.

    **It is the wrong bridge for a spooled upload.** A multipart part over
    `RequestLimits.spool_max_bytes` (1 MiB by default) is written to a temporary
    file and `wreath.request.UploadedFile.data` is left *empty*, so
    `file_chunks(upload.data)` stores a zero-byte object for exactly the uploads
    large enough to want streaming. Iterate `upload.chunks()` instead, which
    reads either kind, and wrap it in an async generator for `write_stream`.

    Args:
        data: the object's contents.
        chunk_size: bytes per chunk. The 1 MiB default is below every part size.

    Yields:
        Successive slices of `data`; nothing at all when `data` is empty.
    """
    view = memoryview(data)
    for start in range(0, len(view), chunk_size):
        yield bytes(view[start:start + chunk_size])


_MIN_PART = 5 * 1024 * 1024
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _amz_date() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _http_date(value: bytes | None) -> float | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value.decode("ascii")).timestamp()
    except (TypeError, ValueError):
        return None


class S3ObjectStore:
    """S3, and anything that speaks its REST API: MinIO, R2, Wasabi, Spaces.

    Requests go over wreath's own `HTTPClient` and are
    authenticated with SigV4 from `wreath._sigv4`. There is no boto3 and no
    third-party dependency: SigV4 is an HMAC chain over a canonicalised request,
    which is a small amount of exact code, and taking on an SDK to avoid writing
    it would put a second HTTP stack and a second retry policy in the process.

    **The client must already be pinned to the host this store signs for.**
    Requests are issued with a path, not an absolute URL, so the scheme and host
    come from the client's `base_url` while the signature covers `host=`.
    A mismatch between the two produces a signature the server rejects rather
    than anything more helpful. `wreath.app.Wreath.objects` wires the pair
    together and is the supported way to build one.

    **Reads and writes are both memory-bounded, by different mechanisms.**
    `read_stream` walks the object in ranged windows;
    `write_stream` switches to a real multipart upload once buffered data
    passes the part size. A single request is still buffered whole by the HTTP
    client, so `window` and `part_size` are the actual bound on how much of
    an object is resident.

    **Nothing here retries.** A failed request raises, and a caller that wants
    to try again decides that with the context to do it safely -- a `PUT` is
    idempotent and a completed multipart upload is not.

    The public `orphaned_uploads` counter reports multipart uploads this store
    began, failed to finish, and then failed to abort; each one is billable
    storage nothing will reclaim without a bucket lifecycle rule. Export it if
    the application exports anything.

    **This store signs its URLs with its AWS credentials**, so it has no
    `url_secret` of its own and refuses one rather than accepting a
    security-shaped argument it would ignore.

    `region` must match the bucket's even when a custom `host` is given, because it
    is part of the signing scope; an S3-compatible server that ignores regions
    will accept any consistent value. The default `host` is the AWS
    virtual-hosted name and is wrong for every such server -- pass the endpoint's
    host with `path_style=True` for those.

    `part_size` is raised to 5 MiB if a smaller value is given, because S3
    rejects any part but the last below that; `window` is raised to 64 KiB. A
    larger window means fewer round trips and more resident memory.

    Args:
        client: an `HTTPClient` whose `base_url` resolves to `host`.
        bucket: the bucket name.
        region: the AWS region. Part of the signing scope, not just routing.
        access_key: the access key id.
        secret_key: the secret access key. Never appears in `__repr__`.
        session_token: an STS token for assumed-role credentials.
        host: the `Host` header to sign and send.
        scheme: used only by `url`; the transport's scheme comes from the client.
        path_style: address objects as `/{bucket}/{key}`.
        service: the SigV4 service name. `"s3"` unless deliberately impersonating.
        part_size: bytes buffered before `write_stream` flushes a multipart part.
        window: bytes requested per ranged GET in `read_stream`.
        url_secret: **refused.** Named only so passing one raises here, not later.

    Raises:
        TypeError: when `url_secret` is given; SigV4 signs with the credentials.
    """

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        session_token: str | None = None,
        host: str | None = None,
        scheme: str = "https",
        path_style: bool = False,
        service: str = "s3",
        part_size: int = 8 * 1024 * 1024,
        window: int = 8 * 1024 * 1024,
        url_secret: bytes | None = None,
    ) -> None:
        if url_secret is not None:
            raise TypeError(
                "S3ObjectStore signs URLs with its AWS credentials and has no use for "
                "url_secret; it is a LocalObjectStore/MemoryObjectStore setting. Drop it, "
                "or the URLs this store mints will not be the ones you think you configured."
            )
        self._client = client
        self._bucket = bucket
        self._region = region
        self._ak = access_key
        self._sk = secret_key
        self._token = session_token
        self._service = service
        self._scheme = scheme
        self._path_style = path_style
        self._host = host or f"{bucket}.s3.{region}.amazonaws.com"
        self._part_size = max(part_size, _MIN_PART)
        self._window = max(window, 1 << 16)
        #: Multipart uploads this store began, failed to finish, and then failed
        #: to abort. Each one is parts already stored in S3 that no longer belong to
        #: any object and that nothing will collect -- they accrue storage charges
        #: until a lifecycle rule reaps them. The abort is best-effort by necessity
        #: (it runs while a more interesting exception is propagating) but a
        #: best-effort cleanup with no counter is one nobody can discover has been
        #: failing, so the count is the signal.
        self.orphaned_uploads = 0

    def __repr__(self) -> str:  # never leak credentials
        return f"S3ObjectStore(bucket={self._bucket!r}, region={self._region!r})"

    # -- request plumbing ----------------------------------------------------
    def _obj_path(self, key: str) -> str:
        return f"/{self._bucket}/{key}" if self._path_style else f"/{key}"

    def _base_path(self) -> str:
        return f"/{self._bucket}" if self._path_style else "/"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: _List[tuple[str, str]] | None = None,
        body: bytes | bytearray | memoryview = b"",
        payload_hash: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        params = params or []
        extra = {k.lower(): v for k, v in (extra_headers or {}).items()}
        if payload_hash is None:
            # `bytes()` on a `bytes` is the same object, so a caller that
            # already had one pays nothing; only a signed `bytearray` body is
            # frozen, and only for the hash. A part upload sends
            # `UNSIGNED_PAYLOAD` and never reaches here.
            #
            # `wreath mutant` reports the `if body` here and the `or None`
            # below as survivors, and both are **equivalent mutants rather
            # than untested lines**: `EMPTY_SHA256` *is* `sha256_hex(b"")`,
            # and `_sigv4.sign` opens with `(headers or {})`. Each branch is a
            # fast path that skips work whose result is already known, so no
            # test can distinguish them and none should be written to try.
            payload_hash = _sigv4.sha256_hex(bytes(body)) if body else _sigv4.EMPTY_SHA256
        amz_date = _amz_date()
        signed = _sigv4.sign(
            method=method, host=self._host, path=path, region=self._region,
            service=self._service, access_key=self._ak, secret_key=self._sk,
            amz_date=amz_date, params=params, payload_hash=payload_hash,
            headers=extra or None, session_token=self._token,
        )
        # `extra` goes on the wire as well as into the signature: a header S3
        # sees but the signature does not cover (or the reverse) is a 403 whose
        # message names only the mismatch.
        headers = {"host": self._host, **extra, **signed}
        wire_headers = tuple(
            (k.encode("ascii"), str(v).encode("latin-1")) for k, v in headers.items()
        )
        query = "&".join(
            f"{_sigv4.uri_encode(k)}={_sigv4.uri_encode(v)}" for k, v in sorted(params)
        )
        target = _sigv4.uri_encode(path, encode_slash=False)
        if query:
            target += "?" + query
        return await self._client.request(method, target, headers=wire_headers, body=body)

    @staticmethod
    def _ok(resp: Any, *allowed: int) -> Any:
        if resp.status not in allowed:
            raise ObjectError(
                f"S3 {resp.status}: {bytes(resp.body[:512]).decode('utf-8', 'replace')}"
            )
        return resp

    # -- ObjectStore protocol ----------------------------------------------------
    async def read(self, key: str) -> bytes:
        """The whole object, in one unranged `GET`.

        Holds the object in memory, so reach for `read_stream` for
        anything whose size is not known to be small.

        An `ObjectError` message carries the first 512 bytes of S3's XML error
        body, which is where the actual reason is.

        Raises:
            ObjectError: for any status but 200, including an absent object's 404.
        """
        key = normalize_key(key)
        resp = self._ok(await self._send("GET", self._obj_path(key)), 200)
        return resp.body

    async def read_stream(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
        """Yield the object in ranged windows of `window` bytes.

        Each window is a separate signed `GET` with a `Range` header, so
        resident memory is one window rather than one object. The cost is one
        round trip per window, which is the trade `window` tunes.

        With no `range`, the walk runs until a window comes back short or
        empty -- a short read is the end of the object. Reaching past the end
        returns 416, which is treated as completion rather than as an error,
        because an object whose size is an exact multiple of the window
        legitimately lands there.

        Args:
            key: the object to read.
            range: an inclusive `(first, last)` byte range. Windows are clipped to it.

        Raises:
            ObjectError: for any status but 200, 206 or 416.
        """
        key = normalize_key(key)
        path = self._obj_path(key)
        start = range[0] if range else 0
        end = range[1] if range else None
        while end is None or start <= end:
            hi = start + self._window - 1
            if end is not None:
                hi = min(hi, end)
            resp = await self._ranged("GET", path, start, hi)
            if resp.status == 416:  # requested past end-of-object → done
                break
            self._ok(resp, 200, 206)
            chunk = resp.body
            if chunk:
                yield chunk
            if not chunk or (end is None and len(chunk) <= hi - start):
                break
            start += len(chunk)

    async def _ranged(self, method: str, path: str, start: int, hi: int) -> Any:
        amz_date = _amz_date()
        rng = f"bytes={start}-{hi}"
        signed = _sigv4.sign(
            method=method, host=self._host, path=path, region=self._region,
            service=self._service, access_key=self._ak, secret_key=self._sk,
            amz_date=amz_date, params=[], payload_hash=_sigv4.EMPTY_SHA256,
            headers={"range": rng}, session_token=self._token,
        )
        headers = {"host": self._host, "range": rng, **signed}
        wire = tuple((k.encode("ascii"), str(v).encode("latin-1")) for k, v in headers.items())
        return await self._client.request(
            method, _sigv4.uri_encode(path, encode_slash=False), headers=wire, body=b"",
        )

    async def write(
        self, key: str, data: bytes | bytearray | memoryview, *, content_type: str | None = None
    ) -> ObjectStat:
        """Store `data` at `key` with one signed `PUT`.

        The body is signed rather than sent unsigned, so its SHA-256 is computed
        over the whole object before the request goes out. Use
        `write_stream` above a few megabytes.

        `content_type` is sent as the `content-type` header and covered by the
        signature, so the object is stored with that media type and served with
        it afterwards. Omitting it leaves S3's own default, `binary/octet-stream`
        -- nothing is guessed from the key here, because a guess that is wrong is
        served to every future reader and there is no way to tell it from a
        deliberate choice.

        Args:
            key: where to store the object.
            data: the object's contents, as `bytes`, `bytearray` or
                `memoryview`. Nothing here retains it: the HTTP client copies
                the body once on its way to the socket.
            content_type: the media type, sent as a header and stored by S3.

        Returns:
            A stat with S3's etag, unquoted, and no `last_modified`.

        Raises:
            ObjectError: for any status but 200.
        """
        key = normalize_key(key)
        resp = self._ok(
            await self._send(
                "PUT", self._obj_path(key), body=data,
                extra_headers={"content-type": content_type} if content_type else None,
            ), 200,
        )
        etag = (resp.header(b"etag") or b"").decode("ascii").strip('"')
        return ObjectStat(key=key, size=len(data), etag=etag, content_type=content_type)

    async def write_stream(
        self,
        key: str,
        chunks: AsyncIterable[bytes | bytearray | memoryview],
        *,
        content_type: str | None = None,
    ) -> ObjectStat:
        """Store a stream at `key`, as one `PUT` or as a multipart upload.

        Chunks accumulate in a buffer; nothing is sent until it reaches
        `part_size`, at which point a multipart upload is initiated and each
        full part is uploaded and dropped from the buffer. A stream that never
        reaches `part_size` is stored with a single `PUT` instead, so a small
        upload does not pay for three round trips. Resident memory is therefore
        bounded by `part_size` plus the largest single chunk.

        **Once the upload has been initiated, every failure aborts it** -- a part
        that S3 rejects, a chunk iterator that raises, a completion that fails,
        a cancellation -- and then the original exception propagates. An upload
        left open is parts already stored and billed that belong to no object,
        so the abort covers the whole window in which one can exist rather than
        only the last step of it.

        If the abort *itself* fails, `orphaned_uploads` is incremented and the
        parts stay in the bucket until a lifecycle rule reaps them: there is no
        third attempt worth making from inside a request that is already
        failing, but there is a number that says it happened. A bucket taking
        streamed writes wants that lifecycle rule regardless, since a process
        killed outright never reaches the abort at all.

        When the confirming `HEAD` after a multipart upload fails -- an eventually
        consistent endpoint, a bucket that permits writes but not reads -- the
        object is nonetheless stored, so a stat with `size=-1` and an empty etag
        is returned to say so rather than raising over an upload that succeeded.

        Args:
            key: where to store the object.
            chunks: the object's contents.
            content_type: sent on the `PUT`, or on the multipart initiate.

        Returns:
            The stat read back with `stat`, or the `size=-1` stat described above.

        Raises:
            ObjectError: from any failing request in the sequence.
        """
        key = normalize_key(key)
        path = self._obj_path(key)
        buf = bytearray()
        parts: list[tuple[int, str]] = []
        upload_id: str | None = None
        num = 0
        try:
            async for chunk in chunks:
                buf += chunk
                while len(buf) >= self._part_size:
                    if upload_id is None:
                        upload_id = await self._initiate(path, content_type)
                    num += 1
                    # The slice is already a fresh `bytearray`; freezing it a
                    # second time copied a whole part for nothing, and neither
                    # `_put_part` nor the HTTP client under it retains a body.
                    part = buf[: self._part_size]
                    parts.append((num, await self._put_part(path, upload_id, num, part)))
                    del buf[: self._part_size]
            if upload_id is None:  # small object → one PUT
                return await self.write(key, buf, content_type=content_type)
            num += 1  # final (possibly < 5 MiB) part
            parts.append((num, await self._put_part(path, upload_id, num, buf)))
            self._ok(await self._complete(path, upload_id, parts), 200)
        except BaseException:
            # Nothing exists to abort until the initiate returned an id, and
            # after this point the object is stored: the window is exactly here.
            if upload_id is not None:
                await self._abort(path, upload_id)
            raise
        try:
            return await self.stat(key)
        except ObjectError:
            return ObjectStat(key=key, size=-1, etag="", content_type=content_type)

    async def _initiate(self, path: str, content_type: str | None) -> str:
        resp = self._ok(
            await self._send(
                "POST", path, params=[("uploads", "")],
                extra_headers={"content-type": content_type} if content_type else None,
            ), 200,
        )
        root = _ET.fromstring(resp.body)
        for el in root.iter():
            if _local(el.tag) == "UploadId":
                return el.text or ""
        raise ObjectError("S3 multipart: no UploadId in initiate response")

    async def _put_part(
        self, path: str, upload_id: str, num: int, data: bytes | bytearray | memoryview
    ) -> str:
        resp = self._ok(
            await self._send(
                "PUT", path,
                params=[("partNumber", str(num)), ("uploadId", upload_id)],
                body=data, payload_hash=_sigv4.UNSIGNED_PAYLOAD,
            ), 200,
        )
        return (resp.header(b"etag") or b"").decode("ascii")

    async def _complete(self, path: str, upload_id: str, parts: _List[tuple[int, str]]) -> Any:
        body = "".join(
            f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in parts
        )
        xml = (
            f'<CompleteMultipartUpload xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"{body}</CompleteMultipartUpload>"
        ).encode()
        return await self._send("POST", path, params=[("uploadId", upload_id)], body=xml)

    async def _abort(self, path: str, upload_id: str) -> None:
        """Best-effort abort of a multipart upload that will not be completed.

        The caller is already unwinding, so raising here would replace the
        interesting exception with a less interesting one. Transport failures are
        therefore counted rather than raised. A programming error is *not* --
        `AttributeError` or `TypeError` out of here is a bug in this file, and
        swallowing it would hide it behind an S3 outage that never happened.

        A refusal S3 *answers* counts too. `DELETE` on a multipart upload
        returns 204, and a status that is neither that nor a 404 -- a 403 from a
        policy that permits writes but not aborts, say -- leaves the same
        orphaned parts a dropped connection would. Reading the status is the
        difference between a cleanup that worked and one that was merely
        attempted. A 404 is `NoSuchUpload`: there is nothing left to reclaim, so
        counting it would be an alarm about storage that does not exist.
        """
        from .http_client import ClientError

        try:
            resp = await self._send("DELETE", path, params=[("uploadId", upload_id)])
        except (ClientError, OSError):
            self.orphaned_uploads += 1
            return
        if resp.status not in (200, 204, 404):
            self.orphaned_uploads += 1

    async def stat(self, key: str) -> ObjectStat:
        """Metadata for one object, from a `HEAD`. No bytes are transferred.

        A bucket that does not grant `s3:ListBucket` answers 403 rather than 404
        for a missing object, and this reports that as an error; `exists` is the
        method that treats it as absence.

        Raises:
            ObjectError: for any status but 200, including a 404 or a 403.
        """
        key = normalize_key(key)
        resp = self._ok(await self._send("HEAD", self._obj_path(key)), 200)
        size = int((resp.header(b"content-length") or b"0").decode("ascii") or 0)
        etag = (resp.header(b"etag") or b"").decode("ascii").strip('"')
        ctype = resp.header(b"content-type")
        return ObjectStat(
            key=key, size=size, etag=etag,
            last_modified=_http_date(resp.header(b"last-modified")),
            # latin-1, not ascii: a header value is bytes on the wire and a
            # parameter S3 hands back verbatim (a filename, say) need not be
            # ASCII. Decoding strictly turned that into a UnicodeDecodeError out
            # of a metadata call, which is not a storage failure by any reading.
            content_type=ctype.decode("latin-1") if ctype else None,
        )

    async def exists(self, key: str) -> bool:
        """Whether `key` is present, from a `HEAD`.

        **403 is reported as absent, not as an error.** A bucket whose policy
        does not grant `s3:ListBucket` answers a `HEAD` for a missing object
        with 403 rather than 404, deliberately, so that a caller cannot probe
        which keys exist. Treating that as a failure would make this method
        unusable on a correctly locked-down bucket; treating it as absence gives
        the same answer a permitted caller would get. The cost is that a genuine
        permission problem reads as an empty bucket -- if this always answers
        False, check the policy before concluding the objects are missing.

        Raises:
            ObjectError: for any status but 200, 403 or 404.
        """
        resp = await self._send("HEAD", self._obj_path(normalize_key(key)))
        if resp.status == 200:
            return True
        if resp.status in (403, 404):
            return False
        return bool(self._ok(resp, 200))

    async def list(
        self, prefix: str = "", *, delimiter: str | None = None
    ) -> AsyncIterator[ObjectStat]:
        """Yield the bucket's objects under `prefix`, following pagination.

        Uses `ListObjectsV2` and follows the continuation token until the
        bucket says it is not truncated, so a caller sees every match without
        handling a page size. Results arrive in the bucket's order, which for S3
        is lexicographic by key.

        `delimiter` makes the bucket omit objects whose key continues past the next
        occurrence of it after the prefix. The `CommonPrefixes` returned in their
        place are **not** yielded, so `delimiter="/"` gives the direct children
        only, with no way to see the subdirectories from here.

        A listing carries neither modification time nor media type, so both are
        `None` on every stat; call `stat` for either. Keys are normalised on the
        way out, so a zero-byte "folder marker" that some consoles create as
        `photos/` is reported as `photos` -- a different key from the one the
        bucket holds.

        Args:
            prefix: matched literally against keys by the bucket. Not normalised.
            delimiter: groups keys in the bucket; see above.

        Yields:
            One `ObjectStat` per object, in the bucket's key order.

        Raises:
            ObjectError: for any status but 200, or for a key that is refused.
        """
        token: str | None = None
        while True:
            params = [("list-type", "2")]
            if prefix:
                params.append(("prefix", prefix))
            if delimiter:
                params.append(("delimiter", delimiter))
            if token:
                params.append(("continuation-token", token))
            resp = self._ok(await self._send("GET", self._base_path(), params=params), 200)
            root = _ET.fromstring(resp.body)
            truncated = False
            token = None
            for el in root:
                name = _local(el.tag)
                if name == "Contents":
                    fields = {_local(c.tag): (c.text or "") for c in el}
                    yield ObjectStat(
                        key=normalize_key(fields.get("Key", "")),
                        size=int(fields.get("Size", "0") or 0),
                        etag=fields.get("ETag", "").strip('"'),
                        last_modified=None,
                    )
                elif name == "IsTruncated":
                    truncated = (el.text or "").strip().lower() == "true"
                elif name == "NextContinuationToken":
                    token = el.text
            if not truncated or not token:
                break

    async def delete(self, key: str) -> None:
        """Remove `key`. Not an error when it is already gone.

        S3 answers 204 whether or not the object was there, so this cannot and
        does not report which happened.

        Raises:
            ObjectError: for any status but 200 or 204.
        """
        self._ok(await self._send("DELETE", self._obj_path(normalize_key(key))), 200, 204)

    def url(self, key: str, *, expires: int = 3600, method: str = "GET") -> str:
        """A fully signed URL a browser can use directly, minted without a round trip.

        The signature is query-string SigV4, computed in this process from the
        credentials the store already holds, so handing a client an upload or
        download URL costs no call to S3. The credentials themselves are never
        in the URL -- only the access key id, the scope, and the signature.

        Unlike `LocalObjectStore.url`, this really expires: the URL carries
        its own signing time in `X-Amz-Date` and AWS rejects it after
        `expires` seconds, so the client's clock is irrelevant.

        AWS caps a SigV4 presign at seven days and rejects a longer one at use
        time rather than here.

        Args:
            key: the object the URL grants access to.
            expires: lifetime in seconds from now.
            method: `"PUT"` for a direct browser upload, `"GET"` for a download.

        Returns:
            An absolute URL using this store's `scheme` and `host`.
        """
        key = normalize_key(key)
        return _sigv4.presign(
            method=method, host=self._host, path=self._obj_path(key), region=self._region,
            service=self._service, access_key=self._ak, secret_key=self._sk,
            amz_date=_amz_date(), expires=expires, session_token=self._token,
            scheme=self._scheme,
        )

    def path(self, key: str) -> ObjectPath:
        """An `ObjectPath` bound to this store and `key`."""
        return ObjectPath(self, key)


# ---------------------------------------------------------------------------
# Resumable uploads
# ---------------------------------------------------------------------------
#
# The shipped surface is **Resumable Uploads for HTTP**,
# `draft-ietf-httpbis-resumable-upload-12` (July 2026). tus 1.0.x is
# deliberately *not* served: the two vocabularies overlap enough
# (`Upload-Offset` means the same thing, `Upload-Length` does not quite, the
# append media types differ) that one implementation answering both would have
# to guess which dialect a request is speaking from the headers it did *not*
# send. One protocol, named in the guide, is the honest surface.


#: Media type a client must use on an append, per the draft. A `PATCH` with any
#: other type is refused with 415 rather than interpreted, because the whole
#: point of the type is that the body is a *fragment* and not a representation.
PARTIAL_UPLOAD = "application/partial-upload"

#: Interim responses (104 Upload Resumption Supported) are not emitted. The
#: draft makes them optional and says a client learns the upload resource from
#: `Location` on the final 2xx, which is the path taken here; wreath's server
#: has no interim-response surface yet, and inventing one for this would be a
#: transport change hiding inside a storage feature.
_UPLOAD_ID_BYTES = 16


def _sf_integer(raw: str | None) -> int | None:
    """A Structured Fields Integer Item (RFC 9651 §3.3.1), or None.

    Deliberately strict and deliberately small. The four header fields this
    module reads are each a bare item, so the full parser is a digit check --
    and returning None for anything else lets each caller decide whether a
    missing field is a refusal or a default, which differs per field.

    Leading `+`, a decimal point, whitespace padding and a signed value are all
    rejected: `Upload-Offset` is defined as a *non-negative* Integer, and
    accepting `-0` or ` 12 ` here would mean two spellings of one offset.
    """
    if raw is None:
        return None
    if not raw or not raw.isdigit() or len(raw) > 15:
        return None
    return int(raw)


def _sf_boolean(raw: str | None) -> bool | None:
    """A Structured Fields Boolean Item (RFC 9651 §3.3.6), or None.

    `?1` and `?0` only. `true`/`false`/`1`/`0` are *not* accepted, because a
    client sending those is speaking a different protocol and guessing its
    intent is how a partial upload gets marked complete.
    """
    if raw == "?1":
        return True
    if raw == "?0":
        return False
    return None


@dataclass(frozen=True, slots=True)
class UploadLimits:
    """What the server will accept, advertised on every upload response.

    Emitted as the `Upload-Limit` Structured Fields Dictionary so a client can
    size its chunks *before* it starts rather than discovering a refusal four
    gigabytes in. Every field is optional and an absent field means "no limit
    of this kind"; the header is omitted entirely when nothing is limited.

    `min_append_size` is the one that surprises people, and it is not
    arbitrary: an S3 multipart upload requires every part except the last to be
    at least 5 MiB, so a backend assembling parts natively cannot accept a
    smaller non-final append without staging the remainder somewhere and
    re-uploading it on the next call. Advertising the floor costs one header;
    hiding it costs a silent write amplification on exactly the slow network
    the protocol exists to survive. A final append -- one carrying
    `Upload-Complete: ?1` -- is exempt, as the last part is.

    `max_append_size` interacts with `RequestLimits.max_body_bytes`, which
    bounds every request body the application will read at all. Advertising a
    larger append than the application will accept is a refusal waiting to
    happen, so `ResumableUploads` clamps this to the smaller of the two when it
    knows both.

    Args:
        max_size: largest complete upload, in bytes.
        min_size: smallest complete upload, in bytes.
        max_append_size: largest single append body.
        min_append_size: smallest non-final append body.
        max_age: seconds an idle upload survives before the sweeper reclaims it.
    """

    max_size: int | None = None
    min_size: int | None = None
    max_append_size: int | None = None
    min_append_size: int | None = None
    max_age: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_size", "min_size", "max_append_size", "min_append_size", "max_age"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer or None")
        if (
            self.max_size is not None
            and self.min_size is not None
            and self.min_size > self.max_size
        ):
            raise ValueError("min_size must not exceed max_size")

    def to_header(self) -> bytes | None:
        """The `Upload-Limit` value, or None when nothing is limited."""
        pairs = [
            (b"max-size", self.max_size),
            (b"min-size", self.min_size),
            (b"max-append-size", self.max_append_size),
            (b"min-append-size", self.min_append_size),
            (b"max-age", self.max_age),
        ]
        rendered = [b"%s=%d" % (name, value) for name, value in pairs if value is not None]
        return b", ".join(rendered) if rendered else None


@dataclass(slots=True)
class UploadState:
    """One upload in progress: where it is, where it is going, and how big.

    `offset` is the number of bytes the server has **durably accepted**, and it
    is the only authority on that. A client's `Upload-Offset` is a claim
    checked against this and never a source of truth -- a client that could
    move the offset by asserting it could overwrite a region it had already
    sent, or read across into another upload's parts.

    `length` is the declared total, once declared. The draft forbids it
    changing, so a second declaration that disagrees is refused rather than
    accepted as a correction.

    Args:
        id: opaque upload identifier, and the last path segment of its resource.
        key: the object key the completed upload will be stored under.
        offset: bytes durably accepted so far.
        length: the declared total size, or None while it is unknown.
        complete: whether the final append has been accepted.
        content_type: the media type, sniffed or declared.
        created: creation time, as a POSIX timestamp.
        updated: last-accepted-append time, as a POSIX timestamp.
        backend: opaque per-backend assembly state (an S3 upload id, a part count).
    """

    id: str
    key: str
    offset: int = 0
    length: int | None = None
    complete: bool = False
    content_type: str | None = None
    created: float = 0.0
    updated: float = 0.0
    backend: dict[str, Any] = field(default_factory=dict)

    def copy(self) -> UploadState:
        """An independent copy, including the backend's own dict.

        `MemoryUploadStore` hands these out and takes them back, and a handler
        mutates the one it is holding *before* asking the store to accept it.
        Sharing the instance would make that mutation land in the store early,
        so the conditional advance would compare the new offset against itself
        and refuse every append -- a store that appears to work in isolation
        and rejects everything the moment it is used the way it is meant to be.
        """
        return UploadState(
            id=self.id,
            key=self.key,
            offset=self.offset,
            length=self.length,
            complete=self.complete,
            content_type=self.content_type,
            created=self.created,
            updated=self.updated,
            backend=_json.loads(_json.dumps(self.backend)),
        )

    def to_json(self) -> bytes:
        """Serialized JSON, which is what the name says and what this returns.

        The one `to_json` in the tree. Nine others returned a `dict` -- a verb
        naming a wire format handing back a Python object -- and are now
        `as_dict`, so the two names mean two things: `as_dict` for a plain
        mapping a caller may still shape, `to_json` for bytes ready to store.
        """
        return _json.dumps(
            {
                "id": self.id,
                "key": self.key,
                "offset": self.offset,
                "length": self.length,
                "complete": self.complete,
                "content_type": self.content_type,
                "created": self.created,
                "updated": self.updated,
                "backend": self.backend,
            }
        ).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> UploadState:
        try:
            data = _json.loads(raw)
        except ValueError as exc:
            raise ObjectError(f"corrupt upload record: {exc}") from exc
        if not isinstance(data, dict):
            raise ObjectError("corrupt upload record: not an object")
        return cls(
            id=str(data["id"]),
            key=str(data["key"]),
            offset=int(data["offset"]),
            length=None if data.get("length") is None else int(data["length"]),
            complete=bool(data.get("complete", False)),
            content_type=data.get("content_type"),
            created=float(data.get("created", 0.0)),
            updated=float(data.get("updated", 0.0)),
            backend=dict(data.get("backend") or {}),
        )


@runtime_checkable
class UploadStore(Protocol):
    """Where the *state* of an in-progress upload lives, as opposed to its bytes.

    Separate from `ObjectStore` because the two have opposite lifetimes: the
    bytes become an object that outlives the request by years, and the record
    saying how many of them have arrived is dead the moment the upload
    completes or expires.

    `advance` is the reason this is a protocol rather than a dict. It is a
    **conditional** advance -- it moves the offset only if the offset is still
    what the caller read -- and it reports whether it won. Two appends racing
    on one upload is not hypothetical (a client retrying a chunk it thinks
    timed out is exactly this), and an unconditional write would let the loser
    rewind the offset and hand the next append a hole in the middle of the
    object.
    """

    async def create(self, state: UploadState) -> None:
        """Record a new upload. Raises `ObjectError` if the id is taken."""
        ...

    async def read(self, upload_id: str) -> UploadState | None:
        """The current state, or None when there is no such upload."""
        ...

    async def advance(self, state: UploadState, *, expected: int) -> bool:
        """Store `state` if the stored offset is still `expected`.

        Returns: whether the write won.
        """
        ...

    async def delete(self, upload_id: str) -> None:
        """Forget an upload. Absent is success."""
        ...

    async def expired(self, before: float) -> list[UploadState]:
        """Every upload whose last activity predates `before`."""
        ...


class MemoryUploadStore:
    """Upload state in one worker's memory.

    The default, and **it confines an upload to the worker that created it**:
    a resume that lands on a sibling worker finds no such upload and is
    answered 404, so the client starts again. That is a wasted upload, not a
    corrupt one -- the failure is closed -- but it makes this the wrong choice
    behind any load balancer that is not sticky. `ObjectUploadStore` is the
    shared one, and it needs no second datastore.

    Not synchronised, for the same reason `MemoryObjectStore` is not: every
    method's mutation is a single dict operation with no `await` inside it, so
    there is nothing for another task to interleave at.
    """

    __slots__ = ("_states",)

    def __init__(self) -> None:
        self._states: dict[str, UploadState] = {}

    async def create(self, state: UploadState) -> None:
        if state.id in self._states:
            raise ObjectError(f"upload already exists: {state.id!r}")
        self._states[state.id] = state.copy()

    async def read(self, upload_id: str) -> UploadState | None:
        current = self._states.get(upload_id)
        return current.copy() if current is not None else None

    async def advance(self, state: UploadState, *, expected: int) -> bool:
        current = self._states.get(state.id)
        if current is None or current.offset != expected:
            return False
        self._states[state.id] = state.copy()
        return True

    async def delete(self, upload_id: str) -> None:
        self._states.pop(upload_id, None)

    async def expired(self, before: float) -> _List[UploadState]:
        return [s for s in list(self._states.values()) if s.updated < before]


class ObjectUploadStore:
    """Upload state as a small object beside the bytes, in the same store.

    The shared-state answer that adds no datastore: a worker that did not
    create the upload can still read its record, because the record is in the
    bucket every worker already talks to. One extra round trip per request buys
    that, which is a fair trade against a resume that fails on a sibling worker.

    **The conditional advance is enforced in-process, and across processes it
    degrades to the offset check.** A plain object store has no
    compare-and-swap, so `advance` re-reads the record and refuses when the
    stored offset moved -- which closes the window for two appends *this*
    worker is serving, and narrows but does not close it for two workers
    serving the same upload at once. The draft already requires a client not to
    append to one upload in parallel; this makes a violating client lose an
    append rather than corrupt an object, because the loser's bytes were
    written to its own part key and are simply never assembled.

    Args:
        store: where records live; ordinarily the same store as the bytes.
        prefix: key prefix for records, kept distinct from the objects' own.
    """

    __slots__ = ("_prefix", "_store")

    def __init__(self, store: ObjectStore, *, prefix: str = ".uploads/") -> None:
        self._store = store
        self._prefix = prefix

    def _record_key(self, upload_id: str) -> str:
        return f"{self._prefix}{upload_id}.json"

    async def create(self, state: UploadState) -> None:
        key = self._record_key(state.id)
        if await self._store.exists(key):
            raise ObjectError(f"upload already exists: {state.id!r}")
        await self._store.write(key, state.to_json(), content_type="application/json")

    async def read(self, upload_id: str) -> UploadState | None:
        try:
            raw = await self._store.read(self._record_key(upload_id))
        except ObjectError:
            return None
        return UploadState.from_json(raw)

    async def advance(self, state: UploadState, *, expected: int) -> bool:
        current = await self.read(state.id)
        if current is None or current.offset != expected:
            return False
        await self._store.write(
            self._record_key(state.id), state.to_json(), content_type="application/json"
        )
        return True

    async def delete(self, upload_id: str) -> None:
        try:
            await self._store.delete(self._record_key(upload_id))
        except ObjectError:
            return

    async def expired(self, before: float) -> _List[UploadState]:
        stale: _List[UploadState] = []
        async for stat in self._store.list(prefix=self._prefix):
            state = await self.read(
                stat.key.removeprefix(self._prefix).removesuffix(".json")
            )
            if state is not None and state.updated < before:
                stale.append(state)
        return stale


class _UploadBackend:
    """How one backend assembles appended chunks into a finished object."""

    #: Smallest non-final append this assembly strategy can accept, in bytes.
    min_append: int = 0

    async def append(
        self, state: UploadState, data: bytes | bytearray | memoryview
    ) -> None:
        raise NotImplementedError

    async def finish(self, state: UploadState) -> ObjectStat:
        raise NotImplementedError

    async def abort(self, state: UploadState) -> None:
        raise NotImplementedError


class _PartsUploadBackend(_UploadBackend):
    """One object per append, concatenated by streaming on completion.

    The strategy for every store that is not S3, including a third-party one
    that satisfies only the `ObjectStore` protocol. It uses nothing but that
    protocol, so it cannot be wrong about a backend's internals, and it accepts
    an append of any size because a part here is just an object.

    The cost is honest and worth stating: completion reads every part and
    writes the object once, so a local upload pays one extra copy of itself.
    `write_stream` is fed a generator over `read_stream`, so that copy is
    bounded by the read window and not by the object -- a four-gigabyte upload
    completes in constant memory, it just does not complete for free.
    `_S3UploadBackend` avoids the copy because S3 assembles parts server-side.
    """

    def __init__(self, store: ObjectStore, prefix: str) -> None:
        self._store = store
        self._prefix = prefix

    def _part_key(self, upload_id: str, number: int) -> str:
        return f"{self._prefix}{upload_id}/{number:08d}.part"

    async def append(
        self, state: UploadState, data: bytes | bytearray | memoryview
    ) -> None:
        number = int(state.backend.get("parts", 0)) + 1
        await self._store.write(self._part_key(state.id, number), data)
        state.backend["parts"] = number

    async def finish(self, state: UploadState) -> ObjectStat:
        count = int(state.backend.get("parts", 0))
        store = self._store
        part_keys = [self._part_key(state.id, n) for n in range(1, count + 1)]

        async def _chunks() -> AsyncIterator[bytes]:
            for part_key in part_keys:
                async for chunk in store.read_stream(part_key):
                    yield chunk

        stat = await store.write_stream(
            state.key, _chunks(), content_type=state.content_type
        )
        for part_key in part_keys:
            await store.delete(part_key)
        return stat

    async def abort(self, state: UploadState) -> None:
        """Delete every staged part. Failures propagate; they are not noise.

        Every backend's `delete` treats an absent key as success, so an
        `ObjectError` out of one is the store saying it *could not*, not that
        there was nothing to do. Swallowing it here would leave objects nobody
        will ever read while the sweeper reported a clean run -- the quietly
        degraded shape, with the signal removed. The callers each have a use
        for it: `sweep` counts into `aborted_uploads`, and a `DELETE` reports.
        """
        count = int(state.backend.get("parts", 0))
        for number in range(1, count + 1):
            await self._store.delete(self._part_key(state.id, number))


class _S3UploadBackend(_UploadBackend):
    """S3's own multipart upload, one part per append.

    Assembly happens in the bucket, so completion transfers nothing: the
    `CompleteMultipartUpload` call is the whole of it. That is what buys the
    5 MiB floor on non-final parts, which `min_append` advertises rather than
    hides.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self.min_append = store._part_size

    async def _ensure(self, state: UploadState) -> str:
        upload_id = state.backend.get("upload_id")
        if upload_id is None:
            path = self._store._obj_path(normalize_key(state.key))
            upload_id = await self._store._initiate(path, state.content_type)
            state.backend["upload_id"] = upload_id
            state.backend["parts"] = []
        return str(upload_id)

    async def append(
        self, state: UploadState, data: bytes | bytearray | memoryview
    ) -> None:
        upload_id = await self._ensure(state)
        path = self._store._obj_path(normalize_key(state.key))
        parts = state.backend["parts"]
        number = len(parts) + 1
        if number > _S3_MAX_PARTS:
            raise ObjectError(
                f"S3 multipart upload is limited to {_S3_MAX_PARTS} parts; "
                "raise the append size"
            )
        etag = await self._store._put_part(path, upload_id, number, data)
        parts.append([number, etag])

    async def finish(self, state: UploadState) -> ObjectStat:
        upload_id = state.backend.get("upload_id")
        path = self._store._obj_path(normalize_key(state.key))
        if upload_id is None:
            # Nothing was ever appended: an upload completed at zero bytes is a
            # legitimate empty object, and a multipart upload cannot express
            # one (S3 refuses a completion with no parts).
            return await self._store.write(
                state.key, b"", content_type=state.content_type
            )
        parts = [(int(n), str(e)) for n, e in state.backend.get("parts", [])]
        self._store._ok(await self._store._complete(path, str(upload_id), parts), 200)
        try:
            return await self._store.stat(state.key)
        except ObjectError:
            return ObjectStat(
                key=normalize_key(state.key), size=state.offset, etag="",
                content_type=state.content_type,
            )

    async def abort(self, state: UploadState) -> None:
        upload_id = state.backend.get("upload_id")
        if upload_id is None:
            return
        path = self._store._obj_path(normalize_key(state.key))
        # `_abort` counts its own failures into `orphaned_uploads` rather than
        # raising, which is what the sweeper needs: parts left in the bucket
        # are billed, and a number that says how often that happened is the
        # difference between a cleanup that worked and one that was attempted.
        await self._store._abort(path, str(upload_id))


#: S3 refuses a multipart upload with more parts than this.
_S3_MAX_PARTS = 10_000

#: Bytes of an append inspected to decide the stored content type. Every
#: signature below fits, and the check is a handful of prefix comparisons
#: against a fixed table -- one step, once per upload. There is no loop with
#: length here to move into C, and adding a vectorised sniffer would widen the
#: build for work the interpreter does in nanoseconds. See the guide.
_SNIFF_BYTES = 32

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"OggS", "application/ogg"),
    (b"fLaC", "audio/flac"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)


def sniff_content_type(prefix: bytes) -> str | None:
    """The media type `prefix` looks like, or None.

    Deliberately a short table of unambiguous magic numbers rather than a
    general detector. The purpose is to refuse a *lie* -- a client declaring
    `image/png` over an HTML document that a browser will then render from your
    origin -- not to classify every format in existence, and a detector that
    guesses at ambiguous input would turn "unknown" into a wrong answer that
    something downstream trusts.

    `RIFF`-family containers (WebP, WAV, AVI) are absent for that reason: they
    share one signature and separating them needs the sub-chunk at offset 8,
    which is a second decision this table does not model.
    """
    for signature, media_type in _SIGNATURES:
        if prefix.startswith(signature):
            return media_type
    if prefix[:5].lower() in (b"<html", b"<!doc") or prefix[:6].lower() == b"<?xml ":
        return "text/plain"
    return None


class _Refused(Exception):
    """An upload request the protocol says to answer with a specific status."""

    __slots__ = ("detail", "extra", "status")

    def __init__(
        self, status: int, detail: str, extra: _List[tuple[bytes, bytes]] | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra or []


class ResumableUploads:
    """The upload-creating resource and the upload resources beneath it.

    Serves `draft-ietf-httpbis-resumable-upload-12`: `POST` to create,
    `HEAD` to learn the current offset, `PATCH` to append, `DELETE` to cancel.
    Mount it with `router()` and the routes are ordinary wreath routes, so
    `permissions=` and every `AuthRequirement` the router already understands
    apply to *creation* -- there is no second authorization vocabulary here,
    and there is nothing to bypass, which is the whole difference from a
    presigned URL.

    **Why this exists beside `url()`.** A presigned URL sends the bytes
    straight to S3, which scales beautifully and routes around every control
    this framework owns: the policy engine never sees the write, the quota is
    never consulted, the declared media type is never checked, and the
    application learns the object exists only if the client bothers to say so.
    This path keeps the application in the way of the *decisions* -- authorize,
    check the quota, choose the key, sniff the type -- while the bytes stream
    through in restartable pieces. It costs the bandwidth the presigned path
    avoided. Both remain available and the guide says which to choose.

    **Admission happens at creation, before the first byte.** A declared
    `Upload-Length` over `limits.max_size`, or refused by `quota`, is a 413
    against a request that has sent nothing. Enforcing it only on append would
    mean accepting gigabytes in order to refuse them.

    **The client's `Upload-Offset` is a claim, never an authority.** It is
    compared against the stored offset and a mismatch is a 409 carrying the
    real one, per §4.4.2. Advancing is conditional on the offset not having
    moved underneath, so two appends racing lose one rather than punching a
    hole in the object.

    **Completion enqueues a durable job.** `on_complete` names a task
    registered on `jobs`; it is enqueued with the upload id as its idempotency
    key, so a retried completion does not run it twice. A callback awaited in
    the request would be lost on the next deploy, which is the failure every
    hand-rolled upload pipeline eventually finds. This is also the seam that
    keeps image processing out of this module: the pixels are the
    application's.

    Args:
        store: where finished objects and parts are written.
        uploads: where in-progress upload state lives. Defaults to
            `MemoryUploadStore`, which confines an upload to one worker; pass
            `ObjectUploadStore(store)` behind more than one.
        prefix: key prefix for finished objects.
        staging: key prefix for parts and records; swept, never served.
        limits: what to advertise and enforce. `min_append_size` is raised to
            the backend's own floor when the backend has one.
        expire: seconds an idle upload survives. Also advertised as `max-age`.
        max_append_bytes: clamp for `max-append-size`; pass the application's
            `RequestLimits.max_body_bytes` so the advertised ceiling is one the
            application will actually read.
        jobs: a `JobRunner`, required when `on_complete` is given.
        on_complete: name of a registered task, enqueued with `(upload_id, key)`.
        quota: awaited at creation with `(request, declared_length_or_None)`;
            returning False refuses with 413. The seam a metering subsystem
            fills -- storage is the most commonly metered resource and this is
            the only moment before the bytes arrive.
        key_for: chooses the final object key from the request and the new
            upload id. Defaults to `prefix` plus the id.
        sniff: refuse an append whose leading bytes contradict a declared
            content type.

    Raises:
        ValueError: `on_complete` without `jobs`, or a non-positive `expire`.
    """

    __slots__ = (
        "_backend_prefix", "_inflight", "_jobs", "_key_for", "_limits", "_on_complete",
        "_prefix", "_quota", "_sniff", "_store", "_uploads", "expire",
        "aborted_uploads", "refused_appends", "swept_uploads",
    )

    def __init__(
        self,
        store: ObjectStore,
        *,
        uploads: UploadStore | None = None,
        prefix: str = "uploads/",
        staging: str = ".uploads/",
        limits: UploadLimits | None = None,
        expire: float = 24 * 3600.0,
        max_append_bytes: int | None = None,
        jobs: Any = None,
        on_complete: str | None = None,
        quota: Any = None,
        key_for: Any = None,
        sniff: bool = True,
    ) -> None:
        if on_complete is not None and jobs is None:
            raise ValueError(
                "on_complete needs jobs=<JobRunner>: completion is a durable job, "
                "not a callback, so it survives the deploy that lands mid-upload"
            )
        if expire <= 0:
            raise ValueError("expire must be positive")
        self._store = store
        self._uploads = uploads if uploads is not None else MemoryUploadStore()
        self._prefix = prefix
        self._backend_prefix = staging
        self._jobs = jobs
        self._on_complete = on_complete
        self._quota = quota
        self._key_for = key_for
        self._sniff = sniff
        self.expire = float(expire)
        #: Uploads whose parts were reclaimed by `sweep`.
        self.swept_uploads = 0
        #: Aborts the sweeper could not complete. Non-zero means storage is
        #: being billed for parts belonging to no object; on S3 the store's own
        #: `orphaned_uploads` counts the same event one layer down.
        self.aborted_uploads = 0
        #: Appends refused for any protocol reason. A rising count against a
        #: single client is a client bug; against all of them it is usually an
        #: advertised limit nobody read.
        self.refused_appends = 0
        self._inflight: set[str] = set()

        backend = self._backend()
        declared = limits if limits is not None else UploadLimits()
        floor = max(backend.min_append, declared.min_append_size or 0)
        ceiling = declared.max_append_size
        if max_append_bytes is not None:
            ceiling = max_append_bytes if ceiling is None else min(ceiling, max_append_bytes)
        self._limits = UploadLimits(
            max_size=declared.max_size,
            min_size=declared.min_size,
            max_append_size=ceiling,
            min_append_size=floor or None,
            max_age=int(self.expire),
        )

    @property
    def limits(self) -> UploadLimits:
        """What this resource advertises, after backend and body-size clamping."""
        return self._limits

    def _backend(self) -> _UploadBackend:
        factory = getattr(self._store, "_resumable_backend", None)
        if factory is not None:
            return factory(self._backend_prefix)
        if isinstance(self._store, S3ObjectStore):
            return _S3UploadBackend(self._store)
        return _PartsUploadBackend(self._store, self._backend_prefix)

    # -- protocol handlers --------------------------------------------------

    def _headers(self, state: UploadState, *, store: bool = False) -> _List[tuple[bytes, bytes]]:
        headers = [
            (b"upload-offset", str(state.offset).encode("ascii")),
            (b"upload-complete", b"?1" if state.complete else b"?0"),
        ]
        if state.length is not None:
            headers.append((b"upload-length", str(state.length).encode("ascii")))
        # Unconditional: `max_age` is always set from `expire`, which the
        # constructor refuses to leave non-positive, so `to_header()` cannot
        # answer None *here*. It can for a bare `UploadLimits`, which is where
        # that contract is tested. A guard that never fires is one a mutant
        # removes without any test objecting, and it did.
        headers.append((b"upload-limit", self._limits.to_header() or b""))
        if store:
            # §4.2: an offset is stale the instant it is read, so a cache that
            # answered a resume from a stored copy would send the client to an
            # offset the server has moved past.
            headers.append((b"cache-control", b"no-store"))
        return headers

    async def create(self, request: Request) -> Response:
        """`POST`: open an upload, optionally with its first bytes.

        Answers 201 with `Location` naming the new upload resource, per §4.3.
        A creation carrying `Upload-Complete: ?1` and the whole representation
        finishes in one round trip and still goes through every check an append
        does.
        """
        complete = _sf_boolean(request.header("upload-complete"))
        if complete is None:
            raise _Refused(400, "Upload-Complete must be ?0 or ?1")
        declared = _sf_integer(request.header("upload-length"))
        await self._admit(request, declared)

        upload_id = _upload_id()
        key = (
            self._key_for(request, upload_id)
            if self._key_for is not None
            else f"{self._prefix}{upload_id}"
        )
        now = _now()
        state = UploadState(
            id=upload_id,
            key=normalize_key(key),
            length=declared,
            content_type=request.header("content-type"),
            created=now,
            updated=now,
        )
        if state.content_type == PARTIAL_UPLOAD:
            # The creation body is not a partial-upload fragment; the type
            # describes the representation being built.
            state.content_type = None
        await self._uploads.create(state)
        try:
            await self._consume(request, state, complete=complete, first=True)
        except _Refused:
            await self._discard(state)
            raise
        headers = self._headers(state)
        headers.append((b"location", f"{request.path.rstrip('/')}/{upload_id}".encode()))
        return Response(b"", status=201, headers=headers)

    async def offset(self, request: Request) -> Response:
        """`HEAD`/`GET`: how much of this upload the server holds.

        204 with `Upload-Offset`, `Upload-Complete`, the declared length when
        there is one, the advertised limits, and `Cache-Control: no-store`.
        """
        state = await self._require(request)
        return Response(b"", status=204, headers=self._headers(state, store=True))

    async def append(self, request: Request) -> Response:
        """`PATCH`: add bytes at the offset the client says it is at.

        The type must be `application/partial-upload` (415 otherwise), the
        offset must match (409 with the real one otherwise), and the result
        must fit inside the declared length and the advertised maximum (413
        otherwise).
        """
        if (request.header("content-type") or "").split(";")[0].strip() != PARTIAL_UPLOAD:
            raise _Refused(415, f"append requires Content-Type: {PARTIAL_UPLOAD}")
        complete = _sf_boolean(request.header("upload-complete"))
        if complete is None:
            raise _Refused(400, "Upload-Complete must be ?0 or ?1")
        claimed = _sf_integer(request.header("upload-offset"))
        if claimed is None:
            raise _Refused(400, "Upload-Offset must be a non-negative integer")
        state = await self._require(request)
        if claimed != state.offset:
            raise _Refused(
                409,
                f"Upload-Offset {claimed} does not match {state.offset}",
                [(b"upload-offset", str(state.offset).encode("ascii"))],
            )
        declared = _sf_integer(request.header("upload-length"))
        if declared is not None and state.length is not None and declared != state.length:
            raise _Refused(400, "Upload-Length must not change")
        if declared is not None:
            # No `and state.length is None`. The refusal above has already
            # established that a declared length either matches the stored one
            # or there is no stored one, so the extra clause only ever guarded
            # an assignment of a value to itself. Two mutants survived on it,
            # one per clause, which is the signature of a condition written
            # twice rather than a control nothing tests.
            state.length = declared
        await self._consume(request, state, complete=complete, first=state.offset == 0)
        return Response(b"", status=204, headers=self._headers(state))

    async def cancel(self, request: Request) -> Response:
        """`DELETE`: give up on an upload and reclaim what it staged."""
        state = await self._require(request)
        await self._discard(state)
        return Response(b"", status=204)

    # -- machinery ----------------------------------------------------------

    async def _require(self, request: Request) -> UploadState:
        # No `if upload_id else None` guard: every store answers None for an
        # empty id anyway, so the shortcut was a second spelling of the same
        # outcome. A mutant survived on it, which is what a redundant clause
        # looks like from the outside.
        state = await self._uploads.read(request.path_params.get("upload_id", ""))
        if state is None:
            raise _Refused(404, "no such upload")
        return state

    async def _admit(self, request: Request, declared: int | None) -> None:
        """Refuse at creation, before a byte has been read."""
        maximum = self._limits.max_size
        if declared is not None and maximum is not None and declared > maximum:
            raise _Refused(413, f"declared length {declared} exceeds max-size {maximum}")
        if self._quota is not None and not await self._quota(request, declared):
            raise _Refused(413, "storage quota exceeded")

    async def _consume(
        self, request: Request, state: UploadState, *, complete: bool, first: bool
    ) -> None:
        """Read the body, hand it to the backend, and advance the offset.

        Nothing is read until the upload is claimed in `_inflight`: two appends
        this worker is serving for one upload would otherwise both read the
        offset, both write a part, and one of them would be assembled into a
        hole. The claim is released in every path, including a refusal.
        """
        if state.complete:
            raise _Refused(
                409,
                "upload is already complete",
                [(b"upload-offset", str(state.offset).encode("ascii"))],
            )
        if state.id in self._inflight:
            raise _Refused(
                409,
                "another append is in flight for this upload",
                [(b"upload-offset", str(state.offset).encode("ascii"))],
            )
        self._inflight.add(state.id)
        try:
            await self._consume_locked(request, state, complete=complete, first=first)
        finally:
            self._inflight.discard(state.id)

    async def _consume_locked(
        self, request: Request, state: UploadState, *, complete: bool, first: bool
    ) -> None:
        expected = state.offset
        buffered = bytearray()
        maximum = self._limits.max_size
        ceiling = self._limits.max_append_size
        async for chunk in request.stream():
            # No `if not chunk: continue` fast path. Appending an empty chunk is
            # a no-op and re-running three comparisons on unchanged values
            # decides nothing differently, so the guard was two spellings of one
            # outcome -- and a mutant survived on it, which is how that reads
            # from outside.
            buffered += chunk
            written = expected + len(buffered)
            # Checked as bytes arrive, not after joining them: `Content-Length`
            # is a claim and a chunked body has none, so a body that overruns
            # its declared length one chunk at a time is refused at the chunk
            # that crosses the line rather than after it has all been read.
            if ceiling is not None and len(buffered) > ceiling:
                raise _Refused(413, f"append exceeds max-append-size {ceiling}")
            if maximum is not None and written > maximum:
                raise _Refused(413, f"upload exceeds max-size {maximum}")
            if state.length is not None and written > state.length:
                raise _Refused(413, f"upload exceeds declared length {state.length}")

        # **The `bytes(buffered)` freeze that used to stand here is gone**, and
        # the decision it was waiting for has been made: `ObjectStore.write`
        # now accepts `bytes | bytearray | memoryview` and promises no backend
        # retains the buffer. Ablated on an 8 MiB append, the freeze was 1155us
        # against 584us without it (35us floor) when it was written, and
        # 592.46us against 201.77us (5.22us floor) when the survey re-took it --
        # both agreeing it was roughly half of an append, and both agreeing it
        # was pure `memcpy` at memory bandwidth rather than slow code. Nothing
        # downstream ever needed the immutability: `MemoryObjectStore.write`
        # copies what it stores regardless, `LocalObjectStore` hands each chunk
        # to `_write_all`, which takes a `memoryview`, and the S3 part goes to
        # the HTTP client, which copies the body once on its way to the socket.
        floor = self._limits.min_append_size
        if not complete and floor is not None and 0 < len(buffered) < floor:
            raise _Refused(400, f"a non-final append must be at least {floor} bytes")
        if complete and state.length is not None and expected + len(buffered) != state.length:
            raise _Refused(400, "a complete upload must match the declared length")

        # No `buffered and` clause: `sniff_content_type(b"")` matches nothing
        # and `_check_sniff` then returns without touching the state, so testing
        # it here was the same decision made twice. `first` is *not* redundant --
        # sniffing a later append would judge the representation by bytes from
        # the middle of it. The 32-byte prefix is frozen because it is 32 bytes:
        # the sniff table is `bytes.startswith` against literals, and copying a
        # sniff window is not the copy that mattered.
        if first and self._sniff:
            self._check_sniff(state, bytes(buffered[:_SNIFF_BYTES]))

        backend = self._backend()
        if buffered:
            await backend.append(state, buffered)
        state.offset = expected + len(buffered)
        state.complete = complete
        state.updated = _now()
        if complete:
            # No `and state.length is None`. A declared length has already been
            # checked to equal `expected + len(buffered)` above, so this assigns the
            # value it already holds; the clause was a second spelling of that
            # check and two mutants survived on it.
            state.length = state.offset
        if not await self._uploads.advance(state, expected=expected):
            # Somebody else moved the offset between the read and here, so
            # these bytes belong to no assembly. They are already written to
            # this attempt's own part key, and the sweeper reclaims them.
            state.offset = expected
            raise _Refused(
                409,
                "the upload advanced underneath this request",
                [(b"upload-offset", str(expected).encode("ascii"))],
            )
        if complete:
            await self._finish(state, backend)

    def _check_sniff(self, state: UploadState, prefix: bytes) -> None:
        looks_like = sniff_content_type(prefix)
        declared = (state.content_type or "").split(";")[0].strip().lower()
        if looks_like is None:
            return
        if declared and declared != looks_like:
            raise _Refused(
                415,
                f"content is {looks_like}, not the declared {declared}",
            )
        state.content_type = looks_like

    async def _finish(self, state: UploadState, backend: _UploadBackend) -> None:
        await backend.finish(state)
        await self._uploads.delete(state.id)
        if self._on_complete is not None:
            # The upload id is the idempotency key: a completion retried after
            # a lost response enqueues nothing the second time.
            await self._jobs.enqueue(
                self._on_complete, state.id, state.key, key=f"upload:{state.id}"
            )

    async def _discard(self, state: UploadState) -> None:
        await self._backend().abort(state)
        await self._uploads.delete(state.id)

    async def sweep(self, *, now: float | None = None) -> int:
        """Reclaim uploads idle for longer than `expire`. Returns how many.

        **Part of the feature, not a follow-up.** An abandoned S3 multipart
        upload is billed for its parts until something aborts it or a lifecycle
        rule reaps it, and an abandoned parts-backend upload is objects nobody
        will ever read. Wire this to a schedule:

        ```python
        @runner.schedule("*/15 * * * *")
        async def reap_uploads() -> None:
            await uploads.sweep()
        ```

        A failure to abort one upload does not stop the sweep -- the next one
        may well succeed -- but it is counted in `aborted_uploads`, because a
        sweeper that silently gives up leaves a bill nobody sees.
        """
        moment = _now() if now is None else now
        stale = await self._uploads.expired(moment - self.expire)
        backend = self._backend()
        reclaimed = 0
        for state in stale:
            try:
                await backend.abort(state)
            except (ObjectError, OSError):
                # Named rather than blanket: these are the storage layer saying
                # no. A TypeError out of here is a bug in this file and must
                # not be filed under "the bucket was unavailable".
                self.aborted_uploads += 1
                continue
            await self._uploads.delete(state.id)
            reclaimed += 1
        self.swept_uploads += reclaimed
        return reclaimed

    # -- mounting -----------------------------------------------------------

    def router(self, path: str = "/uploads", *, permissions: Iterable[str] = ()) -> Any:
        """Ordinary wreath routes for the four protocol operations.

        `permissions` is the router's own keyword and applies to every route,
        so authorization here is the same declaration as on any other route --
        which is the property a presigned URL gives up.
        """
        from .router import Router

        router = Router(permissions=permissions)
        base = path.rstrip("/")
        resource = f"{base}/{{upload_id}}"

        @router.post(base or "/", response_only=True, status_code=201)
        async def create_upload(request: Request) -> Response:
            """Create an upload resource and accept its first bytes."""
            return await self._answer(self.create, request)

        @router.route(resource, methods=("HEAD", "GET"), response_only=True, status_code=204)
        async def upload_offset(request: Request) -> Response:
            """Report how much of this upload the server holds."""
            return await self._answer(self.offset, request)

        @router.patch(resource, response_only=True, status_code=204)
        async def append_upload(request: Request) -> Response:
            """Append bytes at the client's stated offset."""
            return await self._answer(self.append, request)

        @router.delete(resource, response_only=True, status_code=204)
        async def cancel_upload(request: Request) -> Response:
            """Cancel an upload and reclaim what it staged."""
            return await self._answer(self.cancel, request)

        return router

    async def _answer(self, handler: Any, request: Request) -> Response:
        try:
            return await handler(request)
        except _Refused as refused:
            if refused.status != 404:
                self.refused_appends += 1
            response = ProblemResponse(
                status=refused.status, title="Upload refused", detail=refused.detail
            )
            response.headers.extend(refused.extra)
            return response


def _upload_id() -> str:
    """A URL-safe opaque upload id.

    Opaque on purpose: the id is the last segment of a URL the client keeps and
    retries against, so anything derived from the key would leak the object
    layout to whoever sees the URL, and anything sequential would let one
    client guess another's in-progress upload. Hex rather than base64url
    because the value ends up in a path segment and a key prefix, and one
    spelling that needs no escaping anywhere is worth four characters.
    """
    return os.urandom(_UPLOAD_ID_BYTES).hex()


def resumable(store: ObjectStore, **options: Any) -> ResumableUploads:
    """A `ResumableUploads` over `store`. Keywords are `ResumableUploads`'.

    ```python
    uploads = objects.resumable(
        store,
        uploads=objects.ObjectUploadStore(store),
        limits=objects.UploadLimits(max_size=5 * 1024**3),
        jobs=runner,
        on_complete="process_photo",
    )
    app.include_router(uploads.router("/uploads", permissions=["uploads::write"]))
    ```
    """
    return ResumableUploads(store, **options)
