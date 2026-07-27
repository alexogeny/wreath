"""Deprecated alias for `wreath.objects`.

The module was renamed because "store" meant two unrelated things in this
codebase: blob storage, and the small keyed tables behind rate limiting,
idempotency, and sessions. `wreath.objects` stores objects; `wreath.store`
is free for the other meaning.

Importing this module still works and returns the same classes under their old
names. It will be removed in a future release -- move to `wreath.objects`,
where `Storage` is `ObjectStore`, `LocalStorage` is
`LocalObjectStore`, `S3Storage` is `S3ObjectStore`, `StoragePath` is
`ObjectPath`, and `StorageError` is `ObjectError`.
"""

from __future__ import annotations

import warnings

from .objects import (
    LocalObjectStore,
    MemoryObjectStore,
    ObjectError,
    ObjectPath,
    ObjectStat,
    ObjectStore,
    S3ObjectStore,
    file_chunks,
    normalize_key,
    unzip_stream,
    zip_stream,
)

warnings.warn(
    "wreath.storage is deprecated; import from wreath.objects instead "
    "(Storage -> ObjectStore, S3Storage -> S3ObjectStore, "
    "StoragePath -> ObjectPath, StorageError -> ObjectError)",
    DeprecationWarning,
    stacklevel=2,
)

#: The old names, so an existing import keeps working unchanged.
Storage = ObjectStore
InMemoryStorage = MemoryObjectStore
LocalStorage = LocalObjectStore
S3Storage = S3ObjectStore
StoragePath = ObjectPath
StorageError = ObjectError

__all__ = [
    "InMemoryStorage",
    "LocalStorage",
    "ObjectStat",
    "S3Storage",
    "Storage",
    "StorageError",
    "StoragePath",
    "file_chunks",
    "normalize_key",
    "unzip_stream",
    "zip_stream",
]
