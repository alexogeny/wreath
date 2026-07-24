"""Golden emitted-output comparison.

Skipped today; auto-activates when the tool ships. Each ``golden/<app>/<module>.py.expected``
maps back to its ``corpus/<app>/<module>.py`` source. The golden files are
placeholders until Phase 1 emit lands (see golden/README.md).
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
