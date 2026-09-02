from __future__ import annotations

import json
import subprocess
import sys

import pytest


def _run(source: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("statement", "prefix", "required", "unexpected"),
    [
        (
            "from wreath.typegen.model import TypeRef",
            "wreath.typegen",
            "wreath.typegen.model",
            {
                "wreath.typegen.inspect",
                "wreath.typegen.targets.typescript",
                "wreath.typegen.typescript_renderer",
            },
        ),
        (
            "from wreath.infra.model import InfrastructurePlan",
            "wreath.infra",
            "wreath.infra.model",
            {
                "wreath.infra.deploy",
                "wreath.infra.inference",
                "wreath.infra.render",
                "wreath.infra.settings",
            },
        ),
        (
            "from wreath._docs.config import Site",
            "wreath._docs",
            "wreath._docs.config",
            {"wreath._docs.site", "wreath._docs.markdown", "wreath._native._docs"},
        ),
    ],
)
def test_leaf_import_does_not_load_unrelated_tool_runtime(
    statement: str, prefix: str, required: str, unexpected: set[str]
) -> None:
    modules = _run(
        f"{statement}\n"
        "import json, sys\n"
        f"print(json.dumps(sorted(name for name in sys.modules if name.startswith({prefix!r}))))"
    )
    assert required in modules
    assert set(modules).isdisjoint(unexpected)


@pytest.mark.parametrize("package", ["wreath.typegen", "wreath.infra", "wreath._docs"])
def test_lazy_tool_facades_keep_star_dir_and_missing_attribute_contracts(
    package: str,
) -> None:
    contract = _run(
        "import importlib, json\n"
        f"package = importlib.import_module({package!r})\n"
        "namespace = {}\n"
        f"exec('from {package} import *', namespace)\n"
        "try:\n"
        "    getattr(package, 'Nonexistent')\n"
        "except AttributeError as error:\n"
        "    missing = str(error)\n"
        "else:\n"
        "    missing = ''\n"
        "print(json.dumps({\n"
        "    'all': package.__all__,\n"
        "    'star': sorted(name for name in namespace if not name.startswith('__')),\n"
        "    'dir': sorted(name for name in package.__all__ if name in dir(package)),\n"
        "    'missing': missing,\n"
        "}))\n"
    )
    assert contract["star"] == sorted(contract["all"])
    assert contract["dir"] == sorted(contract["all"])
    assert contract["missing"] == f"module {package!r} has no attribute 'Nonexistent'"
