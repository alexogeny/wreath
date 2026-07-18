"""Pure-Python facade over the stdlib native zlib implementation."""

from __future__ import annotations

import zlib


def gzip_compress(data: bytes, level: int = 5) -> bytes:
    if not 0 <= level <= 9:
        raise ValueError("gzip level must be between 0 and 9")
    return zlib.compress(data, level=level, wbits=31)


class GzipCompressor:
    __slots__ = ("_compressor", "_state")

    def __init__(self, level: int = 5) -> None:
        if not 0 <= level <= 9:
            raise ValueError("gzip level must be between 0 and 9")
        self._compressor = zlib.compressobj(level, zlib.DEFLATED, 31)
        self._state = "open"

    def compress(self, data: bytes) -> bytes:
        if self._state != "open":
            raise RuntimeError("gzip compressor is not open")
        compressor = self._compressor
        assert compressor is not None
        return compressor.compress(data)

    def finish(self) -> bytes:
        if self._state != "open":
            raise RuntimeError("gzip compressor is not open")
        self._state = "finished"
        compressor = self._compressor
        assert compressor is not None
        return compressor.flush(zlib.Z_FINISH)

    def close(self) -> None:
        if self._state == "open":
            self._state = "closed"
            self._compressor = None


__all__ = ["GzipCompressor", "gzip_compress"]
