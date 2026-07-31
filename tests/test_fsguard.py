"""The beneath-root open primitive shared by static files and templates (#4/#8)."""

from __future__ import annotations

import os

import pytest

from wreath._fsguard import ContainmentError, open_beneath, open_root


def _write(path, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


def test_opens_a_normal_nested_file(tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub" / "item.txt", b"hello")
    root = open_root(tmp_path)
    try:
        fd, stat = open_beneath(root, "sub/item.txt")
        try:
            assert os.read(fd, 5) == b"hello"
            assert stat.st_size == 5
        finally:
            os.close(fd)
    finally:
        os.close(root)


def test_refuses_a_symlink_final_component(tmp_path) -> None:
    secret = tmp_path.parent / "secret.txt"
    _write(secret, b"SECRET")
    os.symlink(secret, tmp_path / "link.txt")
    root = open_root(tmp_path)
    try:
        with pytest.raises(ContainmentError):
            open_beneath(root, "link.txt")
    finally:
        os.close(root)


def test_refuses_a_symlinked_directory_component(tmp_path) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    _write(outside / "secret.txt", b"SECRET")
    os.symlink(outside, tmp_path / "escape")
    root = open_root(tmp_path)
    try:
        with pytest.raises(ContainmentError):
            open_beneath(root, "escape/secret.txt")
    finally:
        os.close(root)


def test_refuses_parent_traversal(tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    root = open_root(tmp_path / "sub")
    try:
        with pytest.raises(ContainmentError):
            open_beneath(root, "../escape.txt")
    finally:
        os.close(root)


# --- what `wreath mutant` found nothing was watching --------------------------
#
# Five mutants survived this file and five more were UNREACHED. Two of the
# refusals below had never fired in any test, and one of them guards a defect
# that actually shipped -- which is the shape a regression test is for.
#
# The real-filesystem result is equivalent when the `lstat` check is removed:
# `O_NOFOLLOW` on the open below it refuses the same symlink. The two direct
# tests pin both defences independently. The module keeps the pre-open check
# because the errno for `O_NOFOLLOW` + `O_DIRECTORY` is not the same on every
# platform, while `lstat` gives callers one deterministic refusal everywhere.


def test_a_known_symlink_is_refused_before_open(tmp_path, monkeypatch) -> None:
    """The deterministic first defence does not hand a known symlink to open."""
    import wreath._fsguard as guard

    os.symlink(tmp_path / "target.txt", tmp_path / "link.txt")
    root = open_root(tmp_path)

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("a known symlink reached os.open")

    monkeypatch.setattr(guard.os, "open", unexpected_open)
    try:
        with pytest.raises(ContainmentError, match="refusing to follow symlink"):
            guard._open_at(root, "link.txt", 0)
    finally:
        os.close(root)


def test_refuses_an_embedded_nul_byte(tmp_path) -> None:
    """A percent-encoded `%00` in a request path decodes to exactly this.

    `os.open` and `os.lstat` reject an embedded NUL with `ValueError`, which is
    neither exception this module documents, so it escaped every caller's
    handler and became a 500. The refusal exists to put it back in the
    vocabulary callers already catch -- and nothing asserted that until now.
    """
    from wreath._fsguard import _components

    root = open_root(tmp_path)
    try:
        for path in ("bad\x00.txt", "sub/bad\x00.txt", "\x00", "a\x00/b"):
            with pytest.raises(ContainmentError, match="NUL byte"):
                open_beneath(root, path)
            with pytest.raises(ContainmentError, match="NUL byte"):
                _components(path)
    finally:
        os.close(root)


def test_empty_and_dot_components_are_normalised_away(tmp_path) -> None:
    """`a//b`, `./a` and a trailing slash name the same file `a/b` does.

    Dropped rather than refused: they are what an ordinary URL path produces,
    and an empty component reaches `openat` as `""`, which is a
    `FileNotFoundError` rather than the file the caller plainly meant.
    """
    from wreath._fsguard import _components

    assert _components("sub//item.txt") == ["sub", "item.txt"]
    assert _components("./sub/./item.txt") == ["sub", "item.txt"]
    assert _components("/sub/item.txt") == ["sub", "item.txt"]
    assert _components("") == []
    assert _components(".") == []

    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub" / "item.txt", b"hello")
    root = open_root(tmp_path)
    try:
        for spelling in ("sub//item.txt", "./sub/item.txt", "/sub/item.txt"):
            fd, _ = open_beneath(root, spelling)
            try:
                assert os.read(fd, 5) == b"hello"
            finally:
                os.close(fd)
    finally:
        os.close(root)


def test_a_platform_without_dir_fd_fails_closed(tmp_path, monkeypatch) -> None:
    """The module's central safety claim, on the one platform that cannot hold it.

    Windows has no `openat`, so the walk cannot be made race-safe; the module
    documents that it refuses rather than falling back to name-based access.
    Nothing had ever executed that refusal, because every machine the suite runs
    on has `dir_fd`.
    """
    monkeypatch.setattr("wreath._fsguard._HAVE_DIR_FD", False)
    with pytest.raises(ContainmentError, match="lacks openat"):
        open_root(tmp_path)
    # And the walk refuses independently, rather than trusting that its caller
    # obtained the root through `open_root`.
    with pytest.raises(ContainmentError, match="lacks openat"):
        open_beneath(0, "item.txt")


def test_o_nofollow_catches_a_symlink_the_lstat_check_missed(tmp_path, monkeypatch) -> None:
    """The second defence, exercised on its own.

    `_open_at` refuses a symlink twice: an `lstat` before the open, and
    `O_NOFOLLOW` on the open itself for the component that was swapped in
    between. The first catches everything in practice, so the second had never
    run -- `wreath mutant` reported the `ELOOP` branch UNREACHED while the
    `lstat` refusal *survived* removal, which is the same fact from both ends.

    Blinding `S_ISLNK` is how the race is reproduced deterministically: it puts
    the walk in exactly the state a component swapped after the `lstat` leaves
    it.
    """
    secret = tmp_path.parent / "secret.txt"
    _write(secret, b"SECRET")
    os.symlink(secret, tmp_path / "link.txt")
    # `tmp_path.parent` is shared by every test in the session, so this name has
    # to differ from the one `test_refuses_a_symlinked_directory_component` uses.
    outside = tmp_path.parent / "outside-nofollow"
    outside.mkdir(exist_ok=True)
    _write(outside / "secret.txt", b"SECRET")
    os.symlink(outside, tmp_path / "escape")

    monkeypatch.setattr("wreath._fsguard.stat.S_ISLNK", lambda mode: False)
    root = open_root(tmp_path)
    try:
        # The final component, and an intermediate directory component: the open
        # carries O_DIRECTORY for the second, which is the case whose errno
        # differs by platform.
        with pytest.raises(ContainmentError, match="refusing to follow symlink"):
            open_beneath(root, "link.txt")
        with pytest.raises(ContainmentError, match="refusing to follow symlink"):
            open_beneath(root, "escape/secret.txt")
    finally:
        os.close(root)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits")
def test_an_error_that_is_not_a_symlink_stays_an_oserror(tmp_path) -> None:
    """Only ELOOP/EMLINK/ENOTDIR mean "symlink"; everything else propagates.

    The distinction matters to callers: `staticfiles` and the template loader
    catch `ContainmentError` as "refused" and `OSError` as "could not read", and
    collapsing the two would report an unreadable file as an escape attempt.
    Nothing reached the `except OSError` branch with a non-symlink errno,
    because a *missing* file fails at the `lstat` above it and never gets there.
    """
    blocked = tmp_path / "blocked.txt"
    _write(blocked, b"hello")
    blocked.chmod(0o000)
    root = open_root(tmp_path)
    try:
        with pytest.raises(PermissionError):
            open_beneath(root, "blocked.txt")
    finally:
        os.close(root)
        blocked.chmod(0o600)
