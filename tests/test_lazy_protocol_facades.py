from __future__ import annotations

import json
import subprocess
import sys


def _loaded_modules(statement: str, prefixes: tuple[str, ...]) -> set[str]:
    probe = (
        f"{statement}\n"
        "import json, sys\n"
        f"prefixes = {prefixes!r}\n"
        "print(json.dumps(sorted(name for name in sys.modules if name.startswith(prefixes))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_dkim_export_does_not_load_email_delivery_types() -> None:
    loaded = _loaded_modules(
        "from wreath.email import DkimError", ("wreath._dkim", "wreath._userkit")
    )
    assert "wreath._dkim" in loaded
    assert "wreath._userkit" not in loaded


def test_user_store_does_not_load_smtp_and_mime_machinery() -> None:
    loaded = _loaded_modules(
        "from wreath._userkit import InMemoryUserStore",
        ("smtplib", "email.message", "email._header_value_parser"),
    )

    assert loaded == set()


def test_mcp_tool_export_does_not_load_server_and_transport() -> None:
    loaded = _loaded_modules("from wreath.mcp import Tool", ("wreath._mcp",))
    assert "wreath._mcp.registry" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._mcp.auth",
            "wreath._mcp.outbound",
            "wreath._mcp.routes",
            "wreath._mcp.server",
            "wreath._mcp.session",
        }
    )


def test_mutation_export_does_not_load_runner_and_reporter() -> None:
    loaded = _loaded_modules("from wreath.mutant import Mutation", ("wreath._mutant",))
    assert "wreath._mutant.model" in loaded
    assert loaded.isdisjoint(
        {
            "wreath._mutant.operators",
            "wreath._mutant.report",
            "wreath._mutant.runner",
        }
    )


def test_protocol_facades_preserve_export_contracts() -> None:
    probe = """
import json
import wreath.email as email
import wreath.mcp as mcp
import wreath.mutant as mutant

contracts = {}
for module in (email, mcp, mutant):
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
