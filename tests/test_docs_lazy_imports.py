from __future__ import annotations

import json
import subprocess
import sys


def test_public_site_export_does_not_load_the_renderer() -> None:
    probe = """
from wreath.docs import Site
import json, sys
print(json.dumps(sorted(name for name in sys.modules if name.startswith('wreath._docs'))))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))
    assert "wreath._docs.config" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._docs.markdown",
            "wreath._docs.repo",
            "wreath._docs.search",
            "wreath._docs.site",
        }
    )


def test_public_docs_facade_preserves_its_export_contract() -> None:
    probe = """
import json
import wreath.docs as docs
namespace = {}
exec('from wreath.docs import *', namespace)
try:
    getattr(docs, 'Nonexistent')
except AttributeError as error:
    missing = str(error)
else:
    missing = ''
print(json.dumps({
    'all': docs.__all__,
    'dir': sorted(name for name in docs.__all__ if name in dir(docs)),
    'exports': sorted(name for name in docs.__all__ if name in namespace),
    'missing': missing,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)
    assert contract["dir"] == sorted(contract["all"])
    assert contract["exports"] == sorted(contract["all"])
    assert contract["missing"] == "module 'wreath.docs' has no attribute 'Nonexistent'"
