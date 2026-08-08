"""Wreath's public framework API.

The top level is intentionally small. Less common types live in their obvious
modules — for example `wreath.response.ProblemResponse`,
`wreath.binding.Query`, `wreath.policy.CorsPolicy`,
`wreath.webhooks.WebhookHub`, `wreath.http_client.HTTPClient`,
`wreath.authorization.CedarPolicies`, `wreath.testing.TestClient`.

**The six names resolve on first access rather than on import** (PEP 562), and
that is a cost decision rather than a style one. Importing anything under
`wreath` imports this module first, so eagerly reaching for `wreath.app` here
made `import wreath.pagination` pay for the router, the binder, the auth
backends and the flight recorder — 110ms of framework, measured.

`wreath._pytest_plugin` is what makes that worth fixing. pytest loads it through
the `pytest11` entry point in *every* repository that installs Wreath, whether
or not a single fixture is used; the plugin already keeps every Wreath import
inside a function for exactly this reason, and the parent-package import was
quietly undoing it. `pytest --help` measured 336ms with the plugin against
279ms without.

`from wreath import Wreath` costs what it always did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Re-exported for type checkers, which do not run `__getattr__`.
    from .app import Wreath
    from .binding import Depends
    from .request import Request
    from .response import JSONResponse, Response
    from .router import Router

__all__ = ["Depends", "JSONResponse", "Request", "Response", "Router", "Wreath"]

#: Public name -> the submodule that defines it. `test_pytest_plugin.py` holds
#: this to `__all__`, so a name added to one without the other is a red test
#: rather than an `AttributeError` in somebody's application.
_EXPORTS: dict[str, str] = {
    "Depends": "binding",
    "JSONResponse": "response",
    "Request": "request",
    "Response": "response",
    "Router": "router",
    "Wreath": "app",
}


def __getattr__(name: str) -> Any:
    """Import the defining module on first access, then cache in `globals()`.

    Caching is what keeps this off the hot path: `__getattr__` runs only until
    the name exists as a module global, so the second `wreath.Wreath` is an
    ordinary dict lookup rather than an `import_module` call.

    Nothing here forces an import order, and that is only safe because the one
    edge that made order matter is gone -- see the `TYPE_CHECKING` note in
    `wreath/request.py`. The eager top level used to import `.app` first, which
    quietly resolved a cycle between `wreath.request` and `._auth` for every
    entry path in the package. `test_pytest_plugin.py` now enters through each
    public name in turn, from a cold interpreter, so a new cycle is a red test
    rather than an `ImportError` in somebody's quickstart.
    """
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """`dir(wreath)` lists the lazy names too, which `globals()` alone would not."""
    return sorted({*globals(), *__all__})
