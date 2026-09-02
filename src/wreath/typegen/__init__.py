"""Deterministic, dependency-free consumer type generation from typed routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .inspect import build_api_model, derive_operation_id
    from .model import ApiModel, Diagnostic, TypegenError
    from .targets.typescript import render_typescript

__all__ = [
    "ApiModel",
    "Diagnostic",
    "TypegenError",
    "build_api_model",
    "derive_operation_id",
    "render_typescript",
]

_EXPORTS = {
    "ApiModel": "model",
    "Diagnostic": "model",
    "TypegenError": "model",
    "build_api_model": "inspect",
    "derive_operation_id": "inspect",
    "render_typescript": "targets.typescript",
}

_MODULE_EXPORTS = {
    "model": ("ApiModel", "Diagnostic", "TypegenError"),
    "inspect": ("build_api_model", "derive_operation_id"),
    "targets.typescript": ("render_typescript",),
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
