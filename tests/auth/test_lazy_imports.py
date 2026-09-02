from __future__ import annotations

import json
import subprocess
import sys


def _loaded_modules(statement: str) -> set[str]:
    probe = (
        f"{statement}\n"
        "import json, sys\n"
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name.startswith('wreath._auth'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_application_construction_does_not_load_authorization_engine() -> None:
    loaded = _loaded_modules("from wreath import Wreath; Wreath()")
    assert loaded.isdisjoint(
        {
            "wreath._auth.cedar",
            "wreath._auth.cedar_engine",
            "wreath._auth.cedar_schema",
            "wreath._auth.facts",
        }
    )


def test_one_auth_export_does_not_load_unrelated_auth_tools() -> None:
    loaded = _loaded_modules("from wreath._auth import Identity")
    assert "wreath._auth.models" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._auth.backends",
            "wreath._auth.cedar",
            "wreath._auth.cedar_engine",
            "wreath._auth.decorators",
            "wreath._auth.requirements",
        }
    )


def test_auth_exports_keep_their_public_package_contract() -> None:
    probe = """
import json
from wreath import _auth
namespace = {}
exec('from wreath._auth import *', namespace)
try:
    getattr(_auth, 'Nonexistent')
except AttributeError as error:
    missing = str(error)
else:
    missing = ''
print(json.dumps({
    'all': _auth.__all__,
    'dir': sorted(name for name in _auth.__all__ if name in dir(_auth)),
    'exports': {
        name: [namespace[name].__module__, namespace[name].__name__]
        for name in _auth.__all__
    },
    'missing': missing,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)
    expected = {
        "AuthRequirement": ["wreath._auth.requirements", "AuthRequirement"],
        "AuthenticationBackend": ["wreath._auth.backends", "AuthenticationBackend"],
        "AuthorizationDecision": ["wreath._auth.models", "AuthorizationDecision"],
        "AuthorizationProvider": ["wreath._auth.backends", "AuthorizationProvider"],
        "BearerTokenBackend": ["wreath._auth.backends", "BearerTokenBackend"],
        "CedarAuthorizer": ["wreath._auth.cedar", "CedarAuthorizer"],
        "CedarEngine": ["wreath._auth.cedar", "CedarEngine"],
        "Credentials": ["wreath._auth.models", "Credentials"],
        "Identity": ["wreath._auth.models", "Identity"],
        "authenticated": ["wreath._auth.decorators", "authenticated"],
        "authorize": ["wreath._auth.decorators", "authorize"],
        "permissions": ["wreath._auth.decorators", "permissions"],
        "roles": ["wreath._auth.decorators", "roles"],
    }
    assert contract["all"] == list(expected)
    assert contract["dir"] == sorted(expected)
    assert contract["exports"] == expected
    assert contract["missing"] == "module 'wreath._auth' has no attribute 'Nonexistent'"
