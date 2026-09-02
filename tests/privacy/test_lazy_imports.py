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
        "if name.startswith('wreath._privacy'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_reach_declaration_does_not_load_privacy_execution_stack() -> None:
    loaded = _modules("from wreath.privacy import Reach")
    assert "wreath._privacy.model" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._privacy.declare",
            "wreath._privacy.execute",
            "wreath._privacy.graph",
            "wreath._privacy.planner",
            "wreath._privacy.record",
            "wreath._privacy.render",
            "wreath._privacy.retention",
        }
    )


def test_empty_privacy_registry_does_not_load_planning_or_execution() -> None:
    loaded = _modules("from wreath.privacy import Privacy; Privacy()")
    assert "wreath._privacy.registry" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._privacy.execute",
            "wreath._privacy.graph",
            "wreath._privacy.planner",
            "wreath._privacy.record",
            "wreath._privacy.render",
            "wreath._privacy.retention",
        }
    )


@pytest.mark.parametrize("package_name", ["wreath._privacy", "wreath.privacy"])
def test_privacy_facades_keep_star_dir_and_missing_attribute_contracts(
    package_name: str,
) -> None:
    source = """
import importlib, json
from typing import get_type_hints
package = importlib.import_module(PACKAGE)
namespace = {}
exec('from ' + package.__name__ + ' import *', namespace)
hints = get_type_hints(namespace['Privacy'].plan) if 'Privacy' in namespace else {}
print(json.dumps({
    'all': package.__all__,
    'star': sorted(name for name in namespace if not name.startswith('__')),
    'dir': sorted(name for name in package.__all__ if name in dir(package)),
    'plan_return': str(hints.get('return', '')),
}))
""".replace("PACKAGE", repr(package_name))
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)
    assert contract["star"] == sorted(contract["all"])
    assert contract["dir"] == sorted(contract["all"])
    if package_name == "wreath.privacy":
        assert contract["plan_return"] == "<class 'wreath._privacy.model.ErasurePlan'>"

    package = __import__(package_name, fromlist=("",))
    expected = getattr(package, package.__all__[0])
    assert getattr(package, package.__all__[0]) is expected
    missing = "Nonexistent"
    with pytest.raises(AttributeError, match="no attribute 'Nonexistent'"):
        getattr(package, missing)
