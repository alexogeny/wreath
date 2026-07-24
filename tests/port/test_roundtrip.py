"""Emitted code must be valid Python, and porting must be idempotent.

Skipped today; auto-activates when the tool ships. Design 07 §4 (round-trip
``ast.parse`` guard) and §3 (idempotency / no-clobber of unchanged sources).
"""
import ast

import pytest

port = pytest.importorskip("wreath.port")


def test_emitted_modules_parse(tmp_path, corpus_app_roots):
    for root in corpus_app_roots:
        result = port.port_tree(root, output=tmp_path / root.name)
        for emitted_file in result.written_files:
            ast.parse(emitted_file.read_text())  # a broken emit is a tool bug


def test_port_tree_is_idempotent(tmp_path, corpus_app_roots):
    root = corpus_app_roots[0]
    dest = tmp_path / "out"
    first = port.port_tree(root, output=dest)
    second = port.port_tree(root, output=dest)
    # Re-running against unchanged source is a no-op.
    assert second.regenerated == []
    assert first.written_files == second.written_files
