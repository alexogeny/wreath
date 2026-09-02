from __future__ import annotations

import json
import subprocess
import sys

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


def test_constructing_an_application_does_not_load_audit_tools_or_the_orm() -> None:
    loaded = _loaded_modules("from wreath import Wreath; Wreath()")
    unexpected = {
        "wreath._audit.fix",
        "wreath._audit.middleware",
        "wreath._audit.runtime",
        "wreath._audit.sources",
        "wreath.crud",
        "wreath.orm",
        "wreath.pagination",
    }
    assert loaded.isdisjoint(unexpected), sorted(loaded & unexpected)


def test_one_audit_export_does_not_load_unrelated_audit_tools() -> None:
    loaded = _loaded_modules("from wreath._audit import Report")
    assert "wreath._audit.model" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._audit.fix",
            "wreath._audit.middleware",
            "wreath._audit.runtime",
            "wreath._audit.sources",
        }
    )


def test_audit_exports_keep_their_public_package_contract() -> None:
    from wreath import _audit
    from wreath._audit.fix import apply_fixes
    from wreath._audit.middleware import AuditMiddleware
    from wreath._audit.model import Finding, Report, Severity
    from wreath._audit.runtime import audit_response, run_runtime_audit
    from wreath._audit.sources import discover_static_dirs, run_audit

    expected = {
        "Finding": Finding,
        "Report": Report,
        "Severity": Severity,
        "run_audit": run_audit,
        "discover_static_dirs": discover_static_dirs,
        "apply_fixes": apply_fixes,
        "AuditMiddleware": AuditMiddleware,
        "run_runtime_audit": run_runtime_audit,
        "audit_response": audit_response,
    }
    assert _audit.__all__ == list(expected)
    assert {name: getattr(_audit, name) for name in _audit.__all__} == expected
    assert set(_audit.__all__) <= set(dir(_audit))
    missing = "Nonexistent"
    with pytest.raises(AttributeError, match="no attribute 'Nonexistent'"):
        getattr(_audit, missing)
