from __future__ import annotations

import json
import subprocess
import sys

import pytest


def _modules(statement: str) -> set[str]:
    source = (
        f"{statement}\n"
        "import json, sys\n"
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name.startswith('wreath._port'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_port_finding_import_does_not_load_analyzers_or_execution_tools() -> None:
    loaded = _modules("from wreath.port import Finding")
    assert "wreath._port.ir" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._port.analyzer",
            "wreath._port.emit",
            "wreath._port.inventory",
            "wreath._port.verify",
        }
    )


def test_port_analyzer_import_does_not_load_emit_inventory_or_verify() -> None:
    loaded = _modules("from wreath.port import analyze")
    assert "wreath._port.analyzer" in loaded
    assert loaded.isdisjoint(
        {"wreath._port.emit", "wreath._port.inventory", "wreath._port.verify"}
    )


@pytest.mark.parametrize("package_name", ["wreath._port", "wreath.port"])
def test_port_facades_keep_star_dir_and_missing_attribute_contracts(
    package_name: str,
) -> None:
    source = """
import importlib, json
package = importlib.import_module(PACKAGE)
namespace = {}
exec('from ' + package.__name__ + ' import *', namespace)
print(json.dumps({
    'all': package.__all__,
    'star': sorted(name for name in namespace if not name.startswith('__')),
    'dir': sorted(name for name in package.__all__ if name in dir(package)),
}))
""".replace("PACKAGE", repr(package_name))
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)
    assert contract["star"] == sorted(contract["all"])
    assert contract["dir"] == sorted(contract["all"])

    package = __import__(package_name, fromlist=("",))
    expected = getattr(package, package.__all__[0])
    assert getattr(package, package.__all__[0]) is expected
    missing = "Nonexistent"
    with pytest.raises(AttributeError, match="no attribute 'Nonexistent'"):
        getattr(package, missing)
