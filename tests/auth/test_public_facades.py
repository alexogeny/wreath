from __future__ import annotations

import json
import subprocess
import sys


def _loaded_modules(statement: str, prefix: str) -> set[str]:
    probe = (
        f"{statement}\n"
        "import json, sys\n"
        f"print(json.dumps(sorted(name for name in sys.modules if name.startswith({prefix!r}))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_public_auth_identity_does_not_load_unrelated_authentication_tools() -> None:
    loaded = _loaded_modules("from wreath.auth import Identity", "wreath._auth")
    assert "wreath._auth.models" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._auth.backends",
            "wreath._auth.decorators",
            "wreath._auth.jwt",
            "wreath._auth.oauth2",
            "wreath._auth.oidc",
            "wreath._auth.session_backend",
        }
    )


def test_public_authorization_requirement_does_not_load_policy_engines() -> None:
    loaded = _loaded_modules(
        "from wreath.authorization import AuthRequirement", "wreath._auth"
    )
    assert "wreath._auth.requirements" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._auth.cedar",
            "wreath._auth.cedar_engine",
            "wreath._auth.geofence",
            "wreath._auth.permissions",
        }
    )


def test_public_auth_facades_preserve_export_contracts() -> None:
    probe = """
import json
import wreath.auth as auth
import wreath.authorization as authorization

contracts = {}
for module in (auth, authorization):
    namespace = {}
    exec(f'from {module.__name__} import *', namespace)
    try:
        getattr(module, 'Nonexistent')
    except AttributeError as error:
        missing = str(error)
    else:
        missing = ''
    contracts[module.__name__] = {
        'all': module.__all__,
        'dir': sorted(name for name in module.__all__ if name in dir(module)),
        'exports': sorted(name for name in module.__all__ if name in namespace),
        'missing': missing,
    }
print(json.dumps(contracts))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    contracts = json.loads(result.stdout)
    for module_name, contract in contracts.items():
        assert contract["dir"] == sorted(contract["all"])
        assert contract["exports"] == sorted(contract["all"])
        assert contract["missing"] == (
            f"module {module_name!r} has no attribute 'Nonexistent'"
        )
