"""Object / blob storage — a pluggable, zero-dependency storage abstraction.

Phase 1 (this module): the :class:`Storage` protocol, a pathlib-like
:class:`StoragePath`, an in-process :class:`InMemoryStorage` (the dev/test twin the
apps hand-roll), a symlink-safe :class:`LocalStorage` backend (built on the same
``openat`` containment as static files), and incremental Zip64 :func:`zip_stream`.

Phase 2 (also here): :class:`S3Storage` — the S3 REST API over ``http_client`` signed
with zero-dep SigV4 (:mod:`wreath._sigv4`), memory-bounded via windowed ranged reads +
real multipart upload; ``app.storage(...)`` lifespan wiring; and :func:`file_chunks`.
Deferred: a zero-copy ``http_client.stream()`` request/response path (S3 currently rides
the buffered client per op) and an optional native SigV4 signing-key helper (the pure
signer is the shipped impl + parity contract). No native hot path here: byte-moving is
already native in the client; SigV4 is a thin signer.
"""
from __future__ import annotations

import asyncio
import errno
import fnmatch
import hashlib
import hmac
import mimetypes
import os
import stat as _stat
import struct
import zlib
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from xml.etree import ElementTree as _ET

try:  # normal package import
    from . import _sigv4
except ImportError:  # storage.py loaded standalone (zip-only tests never touch S3)
    _sigv4 = None  # type: ignore[assignment]

_CHUNK = 1 << 16
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

__all__ = [
    "StorageError",
    "ObjectStat",
    "Storage",
    "StoragePath",
    "InMemoryStorage",
    "LocalStorage",
    "S3Storage",
    "normalize_key",
    "zip_stream",
    "unzip_stream",
    "file_chunks",
]


class StorageError(Exception):
    """A storage operation failed (bad key, containment escape, missing object)."""


def normalize_key(key: str) -> str:
    """Normalize + validate an object key. The single containment gate for every backend.

    Rejects absolute keys, ``..`` traversal, control characters, and empty keys; collapses
    ``//`` and ``.`` segments. Keys always use ``/`` regardless of platform.
    """
    if not isinstance(key, str) or not key:
        raise StorageError("empty object key")
    if key.startswith("/"):
        raise StorageError(f"absolute object key not allowed: {key!r}")
    parts = []
    for part in key.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise StorageError(f"object key escapes the store: {key!r}")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in part):
            raise StorageError(f"control character in object key: {key!r}")
        parts.append(part)
    if not parts:
        raise StorageError(f"object key resolves to nothing: {key!r}")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ObjectStat:
    key: str
    size: int
    etag: str
    last_modified: float | None = None
    content_type: str | None = None


@runtime_checkable
class Storage(Protocol):
    """The backend contract. All methods take/return already-normalized keys."""

    async def read(self, key: str) -> bytes: ...
    def read_stream(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]: ...
    async def write(
        self, key: str, data: bytes, *, content_type: str | None = None
    ) -> ObjectStat: ...
    async def write_stream(
        self, key: str, chunks: AsyncIterable[bytes], *, content_type: str | None = None
    ) -> ObjectStat: ...
    async def stat(self, key: str) -> ObjectStat: ...
    async def exists(self, key: str) -> bool: ...
    def list(
        self, prefix: str = "", *, delimiter: str | None = None
    ) -> AsyncIterator[ObjectStat]: ...
    async def delete(self, key: str) -> None: ...
    def url(self, key: str, *, expires: int = 3600, method: str = "GET") -> str: ...
    def path(self, key: str) -> StoragePath: ...


class StoragePath:
    """Immutable pathlib-like handle bound to a :class:`Storage` and a key.

    Gives the ``s3path.S3Path`` ergonomics the apps reinvent: ``/`` join, ``name``/
    ``suffix``/``parent``, async ``read_bytes``/``write_bytes``/``open``, ``iterdir``,
    ``glob`` (prefix + fnmatch), ``exists``, ``unlink``, ``presigned_url``.
    """

    __slots__ = ("_storage", "_key")

    def __init__(self, storage: Storage, key: str) -> None:
        self._storage = storage
        self._key = normalize_key(key) if key not in ("", "/") else ""

    @property
    def key(self) -> str:
        return self._key

    @property
    def name(self) -> str:
        return self._key.rsplit("/", 1)[-1]

    @property
    def suffix(self) -> str:
        name = self.name
        dot = name.rfind(".")
        return name[dot:] if dot > 0 else ""

    @property
    def parent(self) -> StoragePath:
        head = self._key.rsplit("/", 1)[0] if "/" in self._key else ""
        return StoragePath(self._storage, head)

    def __truediv__(self, other: str) -> StoragePath:
        joined = f"{self._key}/{other}" if self._key else other
        return StoragePath(self._storage, joined)

    def __repr__(self) -> str:
        return f"StoragePath({self._key!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, StoragePath)
            and other._key == self._key
            and other._storage is self._storage
        )

    def __hash__(self) -> int:
        return hash((id(self._storage), self._key))

    async def read_bytes(self) -> bytes:
        return await self._storage.read(self._key)

    async def write_bytes(self, data: bytes, *, content_type: str | None = None) -> ObjectStat:
        return await self._storage.write(self._key, data, content_type=content_type)

    async def exists(self) -> bool:
        return await self._storage.exists(self._key)

    async def unlink(self) -> None:
        await self._storage.delete(self._key)

    async def iterdir(self) -> AsyncIterator[StoragePath]:
        prefix = f"{self._key}/" if self._key else ""
        async for obj in self._storage.list(prefix):
            yield StoragePath(self._storage, obj.key)

    async def glob(self, pattern: str) -> AsyncIterator[StoragePath]:
        prefix = f"{self._key}/" if self._key else ""
        full = f"{prefix}{pattern}"
        async for obj in self._storage.list(prefix):
            if fnmatch.fnmatch(obj.key, full):
                yield StoragePath(self._storage, obj.key)

    def presigned_url(self, *, expires: int = 3600, method: str = "GET") -> str:
        return self._storage.url(self._key, expires=expires, method=method)

    def open(self, mode: str = "rb") -> _PathIO:
        return _PathIO(self, mode)


class _PathIO:
    """Convenience async context manager over a :class:`StoragePath`.

    ``"rb"`` iterates chunks (and ``.read()`` returns the whole object); ``"wb"``
    buffers writes and flushes on exit. Note: ``"wb"`` buffers — for true streaming
    writes use :meth:`Storage.write_stream` directly.
    """

    __slots__ = ("_path", "_mode", "_buf")

    def __init__(self, path: StoragePath, mode: str) -> None:
        if mode not in ("rb", "wb"):
            raise StorageError(f"unsupported mode {mode!r}")
        self._path = path
        self._mode = mode
        self._buf: list[bytes] = []

    async def __aenter__(self) -> _PathIO:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._mode == "wb":
            await self._path.write_bytes(b"".join(self._buf))

    async def read(self) -> bytes:
        return await self._path.read_bytes()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._path._storage.read_stream(self._path.key):
            yield chunk

    def write(self, data: bytes) -> None:
        self._buf.append(data)


def _sign_local(secret: bytes, method: str, key: str, expires: int) -> str:
    msg = f"{method.upper()}\n{key}\n{expires}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


class InMemoryStorage:
    """A dict-backed store — the dev/test twin the apps hand-roll for S3."""

    def __init__(self, *, url_secret: bytes | None = None) -> None:
        self._objects: dict[str, tuple[bytes, str | None]] = {}
        self._secret = url_secret or os.urandom(32)

    async def read(self, key: str) -> bytes:
        key = normalize_key(key)
        try:
            return self._objects[key][0]
        except KeyError:
            raise StorageError(f"no such object: {key!r}") from None

    async def read_stream(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
        data = await self.read(key)
        if range is not None:
            start, end = range
            data = data[start : end + 1]
        for chunk in _iter_chunks(data):
            yield chunk

    async def write(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectStat:
        key = normalize_key(key)
        self._objects[key] = (bytes(data), content_type)
        return ObjectStat(key, len(data), _etag(data), None, content_type)

    async def write_stream(
        self, key: str, chunks: AsyncIterable[bytes], *, content_type: str | None = None
    ) -> ObjectStat:
        buf = bytearray()
        async for chunk in chunks:
            buf += chunk
        return await self.write(key, bytes(buf), content_type=content_type)

    async def stat(self, key: str) -> ObjectStat:
        key = normalize_key(key)
        try:
            data, ct = self._objects[key]
        except KeyError:
            raise StorageError(f"no such object: {key!r}") from None
        return ObjectStat(key, len(data), _etag(data), None, ct)

    async def exists(self, key: str) -> bool:
        return normalize_key(key) in self._objects

    async def list(
        self, prefix: str = "", *, delimiter: str | None = None
    ) -> AsyncIterator[ObjectStat]:
        for key in sorted(self._objects):
            if key.startswith(prefix):
                data, ct = self._objects[key]
                yield ObjectStat(key, len(data), _etag(data), None, ct)

    async def delete(self, key: str) -> None:
        self._objects.pop(normalize_key(key), None)

    def url(self, key: str, *, expires: int = 3600, method: str = "GET") -> str:
        key = normalize_key(key)
        sig = _sign_local(self._secret, method, key, expires)
        return f"memory:///{key}?expires={expires}&signature={sig}"

    def path(self, key: str) -> StoragePath:
        return StoragePath(self, key)


def _iter_chunks(data: bytes) -> Iterable[bytes]:
    for i in range(0, len(data) or 1, _CHUNK):
        yield data[i : i + _CHUNK]


def _etag(data: bytes) -> str:
    return f'"{hashlib.md5(data).hexdigest()}"'


class LocalStorage:
    """Filesystem backend confined beneath a trusted root, symlink/``..``-safe.

    Reuses the same ``openat`` containment as static files (:mod:`wreath._fsguard`) for
    reads; writes go to a temp file beneath the root then ``os.replace`` (atomic).
    """

    def __init__(self, root: str | os.PathLike[str], *, url_secret: bytes | None = None) -> None:
        self._root = os.fspath(root)
        os.makedirs(self._root, exist_ok=True)
        # Local imports keep this module import-clean for standalone zip/sigv4 testing.
        from ._fsguard import open_root

        self._root_fd = open_root(self._root)
        self._secret = url_secret or os.urandom(32)

    def close(self) -> None:
        try:
            os.close(self._root_fd)
        except OSError:
            pass

    # -- read ---------------------------------------------------------------
    async def read(self, key: str) -> bytes:
        buf = bytearray()
        async for chunk in self.read_stream(key):
            buf += chunk
        return bytes(buf)

    async def read_stream(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
        key = normalize_key(key)
        from ._fsguard import ContainmentError, open_beneath

        try:
            fd, st = await asyncio.to_thread(open_beneath, self._root_fd, key)
        except ContainmentError as exc:
            raise StorageError(str(exc)) from exc
        except FileNotFoundError:
            raise StorageError(f"no such object: {key!r}") from None
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
    async def write(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectStat:
        async def _one() -> AsyncIterator[bytes]:
            yield data

        return await self.write_stream(key, _one(), content_type=content_type)

    async def write_stream(
        self, key: str, chunks: AsyncIterable[bytes], *, content_type: str | None = None
    ) -> ObjectStat:
        key = normalize_key(key)
        parts = key.split("/")
        parent_fd, opened, name = await asyncio.to_thread(self._open_parent, parts, True)
        tmp = f".{name}.{os.urandom(6).hex()}.tmp"
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

    def _open_parent(self, parts: list[str], create: bool) -> tuple[int, list[int], str]:
        """Walk (optionally creating) parent dirs beneath the root, refusing symlinks.

        Returns ``(parent_fd, opened_fds_to_close, final_name)``; ``parent_fd`` is the
        last of ``opened_fds`` (or the root fd when the key is top-level).
        """
        opened: list[int] = []
        current = self._root_fd
        try:
            for part in parts[:-1]:
                try:
                    if _stat.S_ISLNK(os.lstat(part, dir_fd=current).st_mode):
                        raise StorageError(f"refusing symlink component {part!r}")
                except FileNotFoundError:
                    if not create:
                        raise StorageError(f"no such directory component {part!r}") from None
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
        key = normalize_key(key)
        from ._fsguard import ContainmentError, open_beneath

        try:
            fd, st = await asyncio.to_thread(open_beneath, self._root_fd, key)
        except ContainmentError as exc:
            raise StorageError(str(exc)) from exc
        except FileNotFoundError:
            raise StorageError(f"no such object: {key!r}") from None
        os.close(fd)
        return ObjectStat(key, st.st_size, _fs_etag(st), st.st_mtime, mimetypes.guess_type(key)[0])

    async def exists(self, key: str) -> bool:
        try:
            await self.stat(key)
            return True
        except StorageError:
            return False

    async def list(
        self, prefix: str = "", *, delimiter: str | None = None
    ) -> AsyncIterator[ObjectStat]:
        entries = await asyncio.to_thread(self._walk)
        for key in entries:
            if key.startswith(prefix):
                try:
                    yield await self.stat(key)
                except StorageError:
                    continue

    def _walk(self) -> list[str]:
        keys: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(self._root, followlinks=False):
            rel = os.path.relpath(dirpath, self._root)
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if os.path.islink(full):
                    continue
                key = fn if rel == "." else f"{rel.replace(os.sep, '/')}/{fn}"
                keys.append(key)
        return sorted(keys)

    async def delete(self, key: str) -> None:
        key = normalize_key(key)
        parts = key.split("/")
        try:
            parent_fd, opened, name = await asyncio.to_thread(self._open_parent, parts, False)
        except StorageError:
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
        # Phase 1: sign a relative URL; the app-mounted local route + shared secret wiring
        # is Phase 2 (design 09 §3). `verify_local_url` is the route-side check.
        key = normalize_key(key)
        sig = _sign_local(self._secret, method, key, expires)
        return f"/{key}?expires={expires}&signature={sig}"

    def verify_local_url(self, key: str, *, method: str, expires: int, signature: str) -> bool:
        expected = _sign_local(self._secret, method, normalize_key(key), expires)
        return hmac.compare_digest(expected, signature)

    def path(self, key: str) -> StoragePath:
        return StoragePath(self, key)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _fs_etag(st: os.stat_result) -> str:
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


# --------------------------------------------------------------------------- zip
_ZIP_FLAGS = 0x0808  # bit 3 (data descriptor) + bit 11 (UTF-8 filenames)
_DOS_DATE = 0x21  # 1980-01-01, fixed (deterministic, no wall clock)


async def zip_stream(storage: Storage, keys: Iterable[str]) -> AsyncIterator[bytes]:
    """Stream a valid Zip64 archive of ``keys`` from ``storage`` — bounded memory.

    Each object is *stored* (method 0, no compression) and streamed through with a
    running CRC32 + a data descriptor, so neither a whole object nor the whole archive
    is ever buffered; the central directory always uses Zip64 fields.
    """
    offset = 0
    central: list[tuple[bytes, int, int, int]] = []
    for key in keys:
        name = normalize_key(key).encode("utf-8")
        local_off = offset
        lfh = struct.pack(
            "<IHHHHHIIIHH", 0x04034B50, 45, _ZIP_FLAGS, 0, 0, _DOS_DATE, 0, 0, 0, len(name), 0
        ) + name
        yield lfh
        offset += len(lfh)
        crc = 0
        size = 0
        async for chunk in storage.read_stream(key):
            if not chunk:
                continue
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
            yield chunk
            offset += len(chunk)
        crc &= 0xFFFFFFFF
        dd = struct.pack("<IIQQ", 0x08074B50, crc, size, size)
        yield dd
        offset += len(dd)
        central.append((name, crc, size, local_off))

    cd_start = offset
    cd = bytearray()
    for name, crc, size, local_off in central:
        extra = struct.pack("<HHQQQ", 0x0001, 24, size, size, local_off)
        cd += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50, 45, 45, _ZIP_FLAGS, 0, 0, _DOS_DATE,
            crc, 0xFFFFFFFF, 0xFFFFFFFF, len(name), len(extra), 0, 0, 0, 0, 0xFFFFFFFF,
        ) + name + extra
    yield bytes(cd)
    cd_size = len(cd)
    count = len(central)
    z64_off = cd_start + cd_size
    yield struct.pack(
        "<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, count, count, cd_size, cd_start
    )
    yield struct.pack("<IIQI", 0x07064B50, 0, z64_off, 1)
    yield struct.pack(
        "<IHHHHIIH",
        0x06054B50, 0, 0, min(count, 0xFFFF), min(count, 0xFFFF),
        min(cd_size, 0xFFFFFFFF), min(cd_start, 0xFFFFFFFF), 0,
    )


async def unzip_stream(storage: Storage, key: str, *, prefix: str = "") -> list[str]:
    """Extract a zip object from ``storage`` back into individual objects.

    Phase 1 buffers the archive (via stdlib ``zipfile``) then writes each entry; a fully
    streaming reader is a Phase-2 follow-up (design 09 §4). Returns the written keys.
    """
    import io
    import zipfile

    raw = await storage.read(key)
    written: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            dest = f"{prefix}{info.filename}" if prefix else info.filename
            await storage.write(dest, zf.read(info.filename))
            written.append(normalize_key(dest))
    return written


async def file_chunks(data: bytes, *, chunk_size: int = 1 << 20) -> AsyncIterator[bytes]:
    """Bridge an already-read upload body into :meth:`Storage.write_stream`.

    Multipart parsing fully materializes each part, so this slices the buffered bytes
    rather than being a true wire-stream; it exists so ``store.write_stream(key,
    file_chunks(file_bytes))`` reads naturally. Unbuffered upload straight off the
    socket is a follow-up tied to the ``http_client`` streaming request path.
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


class S3Storage:
    """S3 (and S3-compatible: MinIO/R2/Wasabi/Spaces) backend — S3 REST over wreath's
    own :class:`~wreath.http_client.HTTPClient`, signed with zero-dep SigV4
    (:mod:`wreath._sigv4`). No boto3. The ``client`` must be pinned to the same
    scheme/host this signer targets (``app.storage`` wires that up)."""

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

    def __repr__(self) -> str:  # never leak credentials
        return f"S3Storage(bucket={self._bucket!r}, region={self._region!r})"

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
        params: list[tuple[str, str]] | None = None,
        body: bytes = b"",
        payload_hash: str | None = None,
    ) -> Any:
        params = params or []
        if payload_hash is None:
            payload_hash = _sigv4.sha256_hex(body) if body else _sigv4.EMPTY_SHA256
        amz_date = _amz_date()
        signed = _sigv4.sign(
            method=method, host=self._host, path=path, region=self._region,
            service=self._service, access_key=self._ak, secret_key=self._sk,
            amz_date=amz_date, params=params, payload_hash=payload_hash,
            session_token=self._token,
        )
        headers = {"host": self._host, **signed}
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
            raise StorageError(
                f"S3 {resp.status}: {bytes(resp.body[:512]).decode('utf-8', 'replace')}"
            )
        return resp

    # -- Storage protocol ----------------------------------------------------
    async def read(self, key: str) -> bytes:
        key = normalize_key(key)
        resp = self._ok(await self._send("GET", self._obj_path(key)), 200)
        return resp.body

    async def read_stream(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
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
        self, key: str, data: bytes, *, content_type: str | None = None
    ) -> ObjectStat:
        key = normalize_key(key)
        resp = self._ok(
            await self._send("PUT", self._obj_path(key), body=bytes(data)), 200,
        )
        etag = (resp.header(b"etag") or b"").decode("ascii").strip('"')
        return ObjectStat(key=key, size=len(data), etag=etag, content_type=content_type)

    async def write_stream(
        self, key: str, chunks: AsyncIterable[bytes], *, content_type: str | None = None
    ) -> ObjectStat:
        key = normalize_key(key)
        path = self._obj_path(key)
        buf = bytearray()
        parts: list[tuple[int, str]] = []
        upload_id: str | None = None
        num = 0
        async for chunk in chunks:
            buf += chunk
            while len(buf) >= self._part_size:
                if upload_id is None:
                    upload_id = await self._initiate(path, content_type)
                num += 1
                part = bytes(buf[: self._part_size])
                parts.append((num, await self._put_part(path, upload_id, num, part)))
                del buf[: self._part_size]
        if upload_id is None:  # small object → one PUT
            return await self.write(key, bytes(buf), content_type=content_type)
        num += 1  # final (possibly < 5 MiB) part
        parts.append((num, await self._put_part(path, upload_id, num, bytes(buf))))
        try:
            self._ok(await self._complete(path, upload_id, parts), 200)
        except BaseException:
            await self._abort(path, upload_id)
            raise
        try:
            return await self.stat(key)
        except StorageError:
            return ObjectStat(key=key, size=-1, etag="", content_type=content_type)

    async def _initiate(self, path: str, content_type: str | None) -> str:
        resp = self._ok(await self._send("POST", path, params=[("uploads", "")]), 200)
        root = _ET.fromstring(resp.body)
        for el in root.iter():
            if _local(el.tag) == "UploadId":
                return el.text or ""
        raise StorageError("S3 multipart: no UploadId in initiate response")

    async def _put_part(self, path: str, upload_id: str, num: int, data: bytes) -> str:
        resp = self._ok(
            await self._send(
                "PUT", path,
                params=[("partNumber", str(num)), ("uploadId", upload_id)],
                body=data, payload_hash=_sigv4.UNSIGNED_PAYLOAD,
            ), 200,
        )
        return (resp.header(b"etag") or b"").decode("ascii")

    async def _complete(self, path: str, upload_id: str, parts: list[tuple[int, str]]) -> Any:
        body = "".join(
            f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in parts
        )
        xml = (
            f'<CompleteMultipartUpload xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"{body}</CompleteMultipartUpload>"
        ).encode()
        return await self._send("POST", path, params=[("uploadId", upload_id)], body=xml)

    async def _abort(self, path: str, upload_id: str) -> None:
        try:
            await self._send("DELETE", path, params=[("uploadId", upload_id)])
        except Exception:
            pass

    async def stat(self, key: str) -> ObjectStat:
        key = normalize_key(key)
        resp = self._ok(await self._send("HEAD", self._obj_path(key)), 200)
        size = int((resp.header(b"content-length") or b"0").decode("ascii") or 0)
        etag = (resp.header(b"etag") or b"").decode("ascii").strip('"')
        ctype = resp.header(b"content-type")
        return ObjectStat(
            key=key, size=size, etag=etag,
            last_modified=_http_date(resp.header(b"last-modified")),
            content_type=ctype.decode("ascii") if ctype else None,
        )

    async def exists(self, key: str) -> bool:
        resp = await self._send("HEAD", self._obj_path(normalize_key(key)))
        if resp.status == 200:
            return True
        if resp.status in (403, 404):
            return False
        return bool(self._ok(resp, 200))

    async def list(
        self, prefix: str = "", *, delimiter: str | None = None
    ) -> AsyncIterator[ObjectStat]:
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
        self._ok(await self._send("DELETE", self._obj_path(normalize_key(key))), 200, 204)

    def url(self, key: str, *, expires: int = 3600, method: str = "GET") -> str:
        key = normalize_key(key)
        return _sigv4.presign(
            method=method, host=self._host, path=self._obj_path(key), region=self._region,
            service=self._service, access_key=self._ak, secret_key=self._sk,
            amz_date=_amz_date(), expires=expires, session_token=self._token,
            scheme=self._scheme,
        )

    def path(self, key: str) -> StoragePath:
        return StoragePath(self, key)
