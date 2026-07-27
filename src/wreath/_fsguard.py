"""Filesystem access confined beneath a trusted root directory descriptor.

Opening a path *by descriptor* — walking it component by component relative to a
root directory fd, each step refusing to follow a symlink — closes two holes at
once. There is no revalidate-then-reopen-by-name window (the file inspected is
the file whose descriptor is returned), and no symlinked component can redirect
the walk outside the root. Static files and the template loader both use this.

On platforms without `openat`/`dir_fd` support (notably Windows) the walk
cannot be made race-safe here, so it fails closed rather than silently falling
back to name-based access.
"""

from __future__ import annotations

import errno
import os
import stat

#: openat-style access (dir_fd) is required to walk beneath a root descriptor.
_HAVE_DIR_FD = os.open in os.supports_dir_fd
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class ContainmentError(Exception):
    """A path could not be opened beneath the root without escaping it."""


def open_root(directory: str | os.PathLike[str]) -> int:
    """Open `directory` as a trusted root descriptor (caller closes it)."""
    if not _HAVE_DIR_FD:
        raise ContainmentError("platform lacks openat/dir_fd support")
    return os.open(os.fspath(directory), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)


def _components(relative: str) -> list[str]:
    parts = [p for p in relative.replace(os.sep, "/").split("/") if p not in ("", ".")]
    if any(part == ".." for part in parts):
        # `..` is not a symlink, so O_NOFOLLOW cannot catch it; refuse it here.
        raise ContainmentError("path escapes the root")
    return parts


def open_beneath(root_fd: int, relative: str) -> tuple[int, os.stat_result]:
    """Open `relative` for reading beneath `root_fd`.

    Returns `(fd, stat)` where `stat` is the `fstat` of the opened
    descriptor. Raises `ContainmentError` if any component is a symlink or
    the path would escape the root, and `OSError` (e.g. `FileNotFoundError`)
    if the target does not exist. The caller owns and must close `fd`.
    """
    if not _HAVE_DIR_FD:
        raise ContainmentError("platform lacks openat/dir_fd support")
    parts = _components(relative)
    intermediates: list[int] = []
    current = root_fd
    try:
        for part in parts[:-1]:
            fd = _open_at(current, part, _O_DIRECTORY)
            intermediates.append(fd)
            current = fd
        if parts:
            target = _open_at(current, parts[-1], 0)
        else:  # the root itself
            target = os.open(".", os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC, dir_fd=current)
        return target, os.fstat(target)
    finally:
        for fd in intermediates:
            os.close(fd)


def _open_at(dir_fd: int, name: str, extra_flags: int) -> int:
    # lstat (no-follow) rejects a symlink component deterministically, regardless
    # of the platform errno for the O_NOFOLLOW+O_DIRECTORY combination.
    if stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode):
        raise ContainmentError(f"refusing to follow symlink component {name!r}")
    try:
        return os.open(
            name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | extra_flags, dir_fd=dir_fd
        )
    except OSError as exc:
        # A component swapped to a symlink after the lstat still fails the open
        # under O_NOFOLLOW (ELOOP, or ENOTDIR with O_DIRECTORY); fail closed.
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise ContainmentError(f"refusing to follow symlink component {name!r}") from exc
        raise


__all__ = ["ContainmentError", "open_beneath", "open_root"]
