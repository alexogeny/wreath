"""Golden emitted-output comparison (Phase 1 declarative emit).

Auto-activates when the tool ships (``importorskip``). Each ``golden/<app>/<module>.py.expected``
is the exact ``wreath port`` output for its ``corpus/<app>/<module>.py`` source, and
pins the byte-for-byte result of the declarative emitter. Regenerate with the helper
in golden/README.md when the emitter's output intentionally changes.
"""
from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")

_GOLDEN = Path(__file__).parent / "golden"


def _iter_golden():
    return sorted(_GOLDEN.rglob("*.py.expected"))


@pytest.mark.parametrize("expected_path", _iter_golden(), ids=lambda p: p.name)
def test_emitted_matches_golden(expected_path, corpus_root):
    rel = expected_path.relative_to(_GOLDEN).with_suffix("")  # drop ".expected"
    source = corpus_root / rel
    emitted = port.emit_module(source)
    assert emitted == expected_path.read_text()


def test_port_tree_roundtrip_and_idempotent(corpus_root, tmp_path):
    """Every emitted file is valid Python, and a second run is a no-op."""
    import ast

    app = corpus_root / "tumbleweed_api"
    out = tmp_path / "ported"
    result = port.port_tree(app, out)
    assert result.written_files
    for path in out.rglob("*.py"):
        ast.parse(path.read_text())  # the emitter's round-trip guard, re-checked
    again = port.port_tree(app, out)
    assert not again.written_files and not again.regenerated
    assert again.skipped  # unchanged sources are skipped on re-run
