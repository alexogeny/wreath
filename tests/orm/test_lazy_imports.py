from __future__ import annotations

import json
import subprocess
import sys
from importlib import import_module

import pytest


def _loaded_modules(statement: str) -> set[str]:
    probe = (
        f"{statement}\n"
        "import json, sys\n"
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name.startswith('wreath'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_importing_model_does_not_load_the_query_execution_stack() -> None:
    loaded = _loaded_modules("from wreath.orm import Model")
    unexpected = {
        "wreath.orm.compiler",
        "wreath.orm.dto",
        "wreath.orm.registry",
        "wreath.orm.session",
        "wreath.postgres",
        "wreath.sql",
    }
    assert loaded.isdisjoint(unexpected), sorted(loaded & unexpected)


def test_orm_exports_keep_their_public_package_contract() -> None:
    from wreath import orm

    assert set(orm.__all__) == set(orm._EXPORTS)
    assert len(orm.__all__) == len(orm._EXPORTS)
    for name in orm.__all__:
        defining = import_module(f"wreath.orm.{orm._EXPORTS[name]}")
        assert getattr(orm, name) is getattr(defining, name)
    assert set(orm.__all__) <= set(dir(orm))
    missing = "Nonexistent"
    with pytest.raises(AttributeError, match="no attribute 'Nonexistent'"):
        getattr(orm, missing)
