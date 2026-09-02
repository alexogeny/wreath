"""Implementation of the generated admin. Use `wreath.admin`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .fields import FieldAccess, resolve_readable, resolve_writable
    from .registry import Admin, AdminError, ModelAdmin

__all__ = [
    "Admin",
    "AdminError",
    "FieldAccess",
    "ModelAdmin",
    "resolve_readable",
    "resolve_writable",
]

_EXPORTS = {
    "Admin": "registry",
    "AdminError": "registry",
    "FieldAccess": "fields",
    "ModelAdmin": "registry",
    "resolve_readable": "fields",
    "resolve_writable": "fields",
}

_MODULE_EXPORTS = {
    "fields": ("FieldAccess", "resolve_readable", "resolve_writable"),
    "registry": ("Admin", "AdminError", "ModelAdmin"),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    loaded = import_module(f".{module}", __name__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
