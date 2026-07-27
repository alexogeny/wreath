"""Static file serving.

``app.static("/assets", "public/")`` registers a route that serves files from
the directory, with path-traversal and symlink containment, conditional requests
(``ETag`` / ``If-None-Match``), and streamed bodies via ``FileResponse``::

    app = Wreath()
    app.static("/assets", "public")

Directory listings are intentionally not provided. ``index.html`` is served
for directory paths when present.

Files are opened *beneath a trusted root directory descriptor* (see
``wreath._fsguard``): the file that is checked is the file that is served, so
there is no revalidate-then-reopen-by-name window, and no symlinked component
can redirect the request outside the root.
"""

from __future__ import annotations

import asyncio
import os
import stat as _stat
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from typing import TYPE_CHECKING

from ._fsguard import ContainmentError, open_beneath, open_root
from .cache_control import CacheControl
from .exceptions import NotFound
from .response import (
    UNSATISFIABLE,
    FileResponse,
    RedirectResponse,
    Response,
    parse_range,
)


def _etag_matches(header: str | None, etag: str) -> bool:
    """Whether ``If-None-Match`` covers ``etag`` (RFC 9110 §13.1.2).

    The header is a *list*, may be ``*``, and its entries may carry the weak
    ``W/`` prefix -- which the weak comparison this needs ignores. Comparing the
    whole header to one tag with ``==`` meant a client that sent two tags, or a
    proxy that added one, re-downloaded the body every time.
    """
    if not header:
        return False
    if header.strip() == "*":
        return True
    target = etag.removeprefix("W/")
    for candidate in header.split(","):
        if candidate.strip().removeprefix("W/") == target:
            return True
    return False

if TYPE_CHECKING:
    from .request import Request


class StaticFiles:
    """A handler serving files under one directory subtree."""

    __slots__ = ("_executor", "_root", "_root_fd", "cache_control", "html_index")

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        html_index: bool = True,
        cache_control: CacheControl | None = None,
        max_workers: int = 8,
    ) -> None:
        root = os.path.realpath(os.fspath(directory))
        if not os.path.isdir(root):
            raise ValueError(f"static directory does not exist: {directory!r}")
        self._root = root
        self._root_fd = open_root(root)
        self.html_index = html_index
        self.cache_control = cache_control
        # Its own pool, not the loop's default executor. Every `to_thread` in
        # the process shares that one -- an ORM driver falling back, a
        # synchronous background task, a template render -- and a burst of
        # static requests would take all of it, so a directory of images could
        # stall work that had nothing to do with files. Bounded because the
        # work is a single `openat` per request: more threads than this buys
        # queueing, not throughput.
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="wreath-static"
        )

    async def __call__(self, request: Request) -> Response | FileResponse:
        rest = request.path_params.get("path", "")
        resolved = await asyncio.get_running_loop().run_in_executor(
            self._executor, self._resolve, rest
        )
        if resolved is None:
            raise NotFound("Not Found")
        fd, stat, name = resolved
        if isinstance(fd, str):
            # A directory reached without its trailing slash. Serving the index
            # from here would leave every relative link in it resolving one
            # level up, so the client is sent to the canonical path instead.
            return RedirectResponse(request.path + "/", status=308)

        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        headers = [
            (b"etag", etag.encode("ascii")),
            (b"last-modified", formatdate(stat.st_mtime, usegmt=True).encode("ascii")),
            # Advertised on every response, because a client decides whether to
            # *try* a range from this, and a download manager that does not see
            # it will not attempt to resume.
            (b"accept-ranges", b"bytes"),
        ]
        if self.cache_control is not None:
            headers.append((b"cache-control", self.cache_control.to_header()))
        # Conditional first: a 304 is not a range response, and answering the
        # range would send bytes to a client that already has them.
        if _etag_matches(request.header("if-none-match"), etag):
            os.close(fd)
            return Response(b"", status=304, headers=headers)

        window = self._range_for(request, etag, stat.st_size)
        if window is UNSATISFIABLE:
            os.close(fd)
            refused = Response(b"", status=416, headers=list(headers))
            refused.headers.append(
                (b"content-range", f"bytes */{stat.st_size}".encode("ascii"))
            )
            return refused
        if window is not None:
            first, last = window
            headers.append((
                b"content-range",
                f"bytes {first}-{last}/{stat.st_size}".encode("ascii"),
            ))
            return FileResponse.from_descriptor(
                fd, stat, name, headers=headers, range=(first, last)
            )
        return FileResponse.from_descriptor(fd, stat, name, headers=headers)

    def _range_for(self, request: Request, etag: str, size: int):
        """The byte window this request asked for, if any.

        `If-Range` is checked first: it means "send me the range *only if* the
        thing I already hold is still current". A stale validator there has to
        fall back to the whole representation, because splicing fresh bytes into
        a copy of an older file produces a document that never existed
        (RFC 9110 §13.1.5).
        """
        conditional = request.header("if-range")
        if conditional is not None and not _etag_matches(conditional, etag):
            return None
        return parse_range(request.header("range"), size)

    def close(self) -> None:
        """Shut the lookup pool down. Idempotent."""
        self._executor.shutdown(wait=False)

    def _resolve(self, rest: str) -> tuple[int | str, os.stat_result, str] | None:
        """Open a servable file beneath the root, or ``None``.

        Runs in a worker thread. Closes any descriptor it will not return, so a
        directory, a symlink escape, or a missing index never leaks an fd.
        """
        try:
            fd, stat = open_beneath(self._root_fd, rest)
        except (ContainmentError, OSError):
            return None
        if not _stat.S_ISDIR(stat.st_mode):
            return fd, stat, rest
        os.close(fd)
        if not self.html_index:
            return None
        if rest and not rest.endswith("/"):
            # Signals "redirect to the canonical path" to the caller; see
            # `__call__`. Returned rather than raised because this is an
            # ordinary outcome, not an error.
            return "redirect", stat, rest
        index = (rest.rstrip("/") + "/index.html").lstrip("/")
        try:
            fd, stat = open_beneath(self._root_fd, index)
        except (ContainmentError, OSError):
            return None
        if _stat.S_ISDIR(stat.st_mode):
            os.close(fd)
            return None
        return fd, stat, index


__all__ = ["StaticFiles"]
