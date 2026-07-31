"""Client-declared roots, enforced rather than displayed.

`roots/list` is the easiest MCP capability to ship as decoration: ask the client
where its workspace is, put the answer in a list, and never consult it again.
That is worth nothing -- a root nobody enforces is a comment the client wrote.

So a root here is a **boundary on filesystem-backed reads**, and it is enforced
by the machinery that already enforces one. `wreath._fsguard` walks a path
component by component beneath a trusted directory descriptor, refusing to
follow a symlink at any step, and static files and the template loader both use
it; `ToolContext.read_file` is the third caller. The server's own
`MCP(file_root=...)` is the outer bound -- nothing above it is nameable however
the path is spelled -- and the client's declared roots narrow it further, so a
client that says "my workspace is `/srv/data/public`" cannot be handed
`/srv/data/private/keys.pem` by a tool that asks for it.

Two checks, and both are needed. The lexical one decides whether the *name* the
tool asked for lies beneath a declared root, which is the question the client's
declaration is about; the descriptor walk decides whether the *file* that name
opens really lives beneath the server's root, which is the question a symlink
would otherwise answer for us.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from .._fsguard import ContainmentError, open_beneath, open_root


def root_paths(payload: Any) -> tuple[str, ...]:
    """The absolute paths in one `roots/list` result.

    A root that is not a `file://` URI is dropped rather than refused: the
    specification leaves room for other schemes, and a client that declares an
    HTTP root has said nothing about the filesystem either way.
    """
    if not isinstance(payload, Mapping):
        return ()
    entries = payload.get("roots")
    if not isinstance(entries, (list, tuple)):
        return ()
    found: list[str] = []
    for entry in entries:
        uri = entry.get("uri") if isinstance(entry, Mapping) else None
        if not isinstance(uri, str):
            continue
        parts = urlsplit(uri)
        if parts.scheme != "file" or not parts.path:
            continue
        found.append(os.path.normpath(unquote(parts.path)))
    return tuple(found)


def beneath_any(roots: tuple[str, ...], candidate: str) -> bool:
    """Whether `candidate` lies at or beneath one of `roots`."""
    target = os.path.normpath(candidate)
    for root in roots:
        if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def read_beneath(root_fd: int, relative: str, *, max_bytes: int) -> bytes:
    """Read one regular file beneath `root_fd`, or refuse and say why.

    Runs on a worker thread, called through `asyncio.to_thread`: the walk and
    the read are blocking syscalls, and an MCP endpoint serving a slow disk must
    not stop serving every other session while it waits.

    Raises:
        ContainmentError: The path escapes the root, traverses a symlink, is not
            a regular file, or is larger than `max_bytes`.
        OSError: There is no such file.
    """
    handle, info = open_beneath(root_fd, relative)
    try:
        if not stat.S_ISREG(info.st_mode):
            raise ContainmentError(
                f"{relative!r} is not a regular file, and a directory or a "
                "device is not something a resource read can return"
            )
        if info.st_size > max_bytes:
            raise ContainmentError(
                f"{relative!r} is {info.st_size} bytes, over this server's "
                f"`MCPLimits(max_file_bytes={max_bytes})`. The whole file "
                "becomes one JSON-RPC result held in memory, so the ceiling is "
                "on what one answer may cost."
            )
        with os.fdopen(handle, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(handle)


__all__ = ["ContainmentError", "beneath_any", "open_root", "read_beneath", "root_paths"]
