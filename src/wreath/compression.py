"""Gzip compression using CPython's maintained native zlib module."""

from __future__ import annotations

from ._pure.compression import GzipCompressor, gzip_compress

__all__ = ["GzipCompressor", "gzip_compress"]
