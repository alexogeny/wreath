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
        "if name.startswith('wreath.policy'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_default_application_loads_only_its_configured_policy_component() -> None:
    loaded = _modules("from wreath import Wreath; Wreath()")
    assert "wreath.policy.traffic" in loaded
    assert loaded.isdisjoint(
        {
            "wreath.policy.admission",
            "wreath.policy.cache",
            "wreath.policy.compression",
            "wreath.policy.cors",
            "wreath.policy.csrf",
            "wreath.policy.deadline",
            "wreath.policy.idempotency",
            "wreath.policy.ratelimit",
            "wreath.policy.sessions",
        }
    )


def test_one_policy_export_does_not_load_unrelated_policy_components() -> None:
    loaded = _modules("from wreath.policy import CorsPolicy; CorsPolicy(allow_origins=['*'])")
    assert "wreath.policy.cors" in loaded
    assert loaded.isdisjoint(
        {
            "wreath.policy.admission",
            "wreath.policy.cache",
            "wreath.policy.compression",
            "wreath.policy.deadline",
            "wreath.policy.idempotency",
            "wreath.policy.ratelimit",
            "wreath.policy.sessions",
            "wreath.policy.traffic",
        }
    )


def test_policy_exports_and_exact_component_detection_keep_their_contract() -> None:
    source = """
import json
from typing import get_type_hints
from wreath import policy
hints = get_type_hints(policy.HttpPolicy.__init__)
namespace = {}
exec('from wreath.policy import *', namespace)
cors_type = namespace['CorsPolicy']
cors = cors_type(allow_origins=['*'])
subclass = type('CorsSubclass', (cors_type,), {})
spoof = type('CorsPolicy', (), {'__module__': 'wreath.policy.cors'})
try:
    getattr(policy, 'Nonexistent')
except AttributeError as error:
    missing = str(error)
else:
    missing = ''
print(json.dumps({
    'all': policy.__all__,
    'star': sorted(name for name in namespace if not name.startswith('__')),
    'dir': sorted(name for name in policy.__all__ if name in dir(policy)),
    'exact': policy.is_policy_component(cors),
    'subclass': policy.is_policy_component(subclass(allow_origins=['*'])),
    'spoof': policy.is_policy_component(spoof()),
    'cors_hint': str(hints['cors']),
    'missing': missing,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)
    assert contract["star"] == sorted(contract["all"])
    assert contract["dir"] == sorted(contract["all"])
    assert contract["exact"] is True
    assert contract["subclass"] is False
    assert contract["spoof"] is False
    assert contract["cors_hint"] == "wreath.policy.cors.CorsPolicy | None"
    assert contract["missing"] == "module 'wreath.policy' has no attribute 'Nonexistent'"


def test_policy_type_refusals_and_missing_exports_are_directly_observable() -> None:
    from wreath import policy
    from wreath.policy.cors import CorsPolicy

    assert policy.CorsPolicy is CorsPolicy

    with pytest.raises(
        TypeError,
        match=(
            "cors must be an exact CorsPolicy; "
            "subclass behavior cannot be frozen into native policy"
        ),
    ):
        policy.HttpPolicy(cors=object())

    wrong_name = type("WrongName", (), {"__module__": "wreath.policy.cors"})
    assert policy.is_policy_component(wrong_name()) is False
    missing = "Nonexistent"
    with pytest.raises(AttributeError, match="no attribute 'Nonexistent'"):
        getattr(policy, missing)


def test_direct_lazy_resolution_accepts_a_declared_export() -> None:
    from wreath import policy
    from wreath.policy.cors import CorsPolicy

    assert policy.__getattr__("CorsPolicy") is CorsPolicy
