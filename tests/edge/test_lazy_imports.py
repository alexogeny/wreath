from __future__ import annotations

import json
import subprocess
import sys


def _probe(statement: str) -> dict[str, object]:
    source = f"""
{statement}
import json, sys
print(json.dumps({{
    'wreath': sorted(name for name in sys.modules if name.startswith('wreath')),
    'edge': sorted(name for name in sys.modules if name.startswith('wreath.edge')),
}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_upstream_declarations_do_not_load_either_proxy_runtime() -> None:
    modules = _probe(
        "from wreath.edge import Upstream, UpstreamPool; "
        "UpstreamPool([Upstream('http://127.0.0.1:8000')])"
    )
    assert "wreath.edge.upstream" in modules["edge"]
    assert set(modules["edge"]).isdisjoint(
        {"wreath.edge.proxy", "wreath.edge.serve"}
    )
    assert "wreath.http_client" not in modules["wreath"]
    assert "wreath._native._edge" not in modules["wreath"]


def test_edge_exports_keep_their_public_package_contract() -> None:
    source = """
import json
from wreath import edge
namespace = {}
exec('from wreath.edge import *', namespace)
try:
    getattr(edge, 'Nonexistent')
except AttributeError as error:
    missing = str(error)
else:
    missing = ''
print(json.dumps({
    'all': edge.__all__,
    'dir': sorted(name for name in edge.__all__ if name in dir(edge)),
    'exports': {
        name: [type(namespace[name]).__name__, getattr(namespace[name], '__module__', '')]
        for name in edge.__all__
    },
    'missing': missing,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)
    expected = {
        "DEFAULT_ATTEMPTS": ["int", ""],
        "DEFAULT_CONNECTIONS": ["int", ""],
        "DEFAULT_MAX_BODY": ["int", ""],
        "HOP_BY_HOP": ["frozenset", ""],
        "IDEMPOTENT": ["frozenset", ""],
        "EdgeHandle": ["type", "wreath.edge.serve"],
        "Ejection": ["type", "wreath.edge.upstream"],
        "ReverseProxy": ["type", "wreath.edge.proxy"],
        "Upstream": ["type", "wreath.edge.upstream"],
        "UpstreamPool": ["type", "wreath.edge.upstream"],
        "forwardable": ["function", "wreath.edge.headers"],
        "serve": ["function", "wreath.edge.serve"],
    }
    assert contract["all"] == list(expected)
    assert contract["dir"] == sorted(expected)
    assert contract["exports"] == expected
    assert contract["missing"] == "module 'wreath.edge' has no attribute 'Nonexistent'"
