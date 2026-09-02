from __future__ import annotations

import json
import subprocess
import sys


def _run(source: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_field_declaration_does_not_load_admin_views_or_orm() -> None:
    modules = _run(
        "from wreath.admin import FieldAccess\n"
        "import json, sys\n"
        "print(json.dumps(sorted(name for name in sys.modules if name.startswith('wreath'))))\n"
    )
    assert "wreath._admin.fields" in modules
    assert set(modules).isdisjoint(
        {
            "wreath._admin.pages",
            "wreath._admin.registry",
            "wreath.crud",
            "wreath.orm",
            "wreath.pagination",
        }
    )


def test_admin_facades_keep_star_dir_and_missing_attribute_contracts() -> None:
    for package_name in ("wreath._admin", "wreath.admin"):
        contract = _run(
            "import importlib, json\n"
            f"package = importlib.import_module({package_name!r})\n"
            "namespace = {}\n"
            f"exec('from {package_name} import *', namespace)\n"
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
        assert contract["missing"] == (
            f"module {package_name!r} has no attribute 'Nonexistent'"
        )
