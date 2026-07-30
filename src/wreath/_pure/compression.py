"""Pure-Python facade over the stdlib native zlib implementation."""

from __future__ import annotations

import zlib


def gzip_compress(data: bytes, level: int = 5) -> bytes:
    """One complete gzip member for `data`, header and trailer included.

    The whole-buffer form: hand it the bytes you have and get back something a
    client can decompress on its own. Use `GzipCompressor` instead when the body
    arrives in pieces and you do not want to hold all of it.

    `level` runs from 0 (store, no compression) to 9 (smallest, slowest), and
    defaults to 5. Compression itself is CPython's `zlib`, a C extension in
    every supported build.

    Raises:
        ValueError: `level` is outside 0-9.
    """
    if not 0 <= level <= 9:
        raise ValueError("gzip level must be between 0 and 9")
    return zlib.compress(data, level=level, wbits=31)


class GzipCompressor:
    """The streaming form of `gzip_compress`: feed it chunks, then finish.

    One instance encodes exactly one gzip member, and it is a small state
    machine with three states — open, finished, closed — of which only *open*
    accepts work:

    ```python
    encoder = GzipCompressor(level=5)
    for chunk in body:
        yield encoder.compress(chunk)
    yield encoder.finish()
    ```

    `compress` and `finish` both raise `RuntimeError` once the compressor has
    left the open state, so a stream cannot be continued past its own trailer
    and a second `finish()` cannot emit a second one — which would produce
    bytes no decompressor agrees on.

    Abandon a stream that will never be finished with `close()`. It emits
    nothing, drops the encoder, is idempotent, and never raises, so it is safe
    in a `finally`.

    Args:
        level: 0 (store) to 9 (smallest, slowest); 5 by default.

    Raises:
        ValueError: `level` is outside 0-9.
    """

    __slots__ = ("_compressor", "_state")

    def __init__(self, level: int = 5) -> None:
        if not 0 <= level <= 9:
            raise ValueError("gzip level must be between 0 and 9")
        self._compressor = zlib.compressobj(level, zlib.DEFLATED, 31)
        self._state = "open"

    def compress(self, data: bytes) -> bytes:
        """Encode one chunk, returning whatever is ready to send.

        **`b""` is a normal answer, not an error.** deflate buffers to find
        matches, so a chunk often produces no output at all; the bytes it
        contributed come out of a later `compress` or out of `finish`. A caller
        that treats an empty return as end-of-stream will truncate the body.

        Raises:
            RuntimeError: this compressor has already been finished or closed.
        """
        if self._state != "open":
            raise RuntimeError("gzip compressor is not open")
        compressor = self._compressor
        assert compressor is not None
        return compressor.compress(data)

    def finish(self) -> bytes:
        """Flush what is still buffered, plus the gzip trailer, and close the member.

        The return value is the last thing to send: the trailer carries the CRC
        and the uncompressed length, so a body that stops before it is
        incomplete no matter how many chunks preceded it. Afterwards the
        compressor is finished, and `compress` and `finish` both raise.

        Raises:
            RuntimeError: this compressor has already been finished or closed.
        """
        if self._state != "open":
            raise RuntimeError("gzip compressor is not open")
        self._state = "finished"
        compressor = self._compressor
        assert compressor is not None
        return compressor.flush(zlib.Z_FINISH)

    def close(self) -> None:
        """Abandon a still-open compressor without emitting a trailer.

        For the stream that will not be completed — a client that disconnected,
        a handler that raised part-way through a body. An open compressor
        releases its encoder and becomes closed, after which `compress` and
        `finish` raise; a compressor that is already finished or closed is left
        exactly as it was.

        Never raises, in any of the three states, which is what makes it safe in
        a `finally` that does not know which one it is in.
        """
        if self._state == "open":
            self._state = "closed"
            self._compressor = None


__all__ = ["GzipCompressor", "gzip_compress"]
