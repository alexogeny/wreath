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
