"""Deterministic, dependency-free consumer type generation from typed routes."""

from __future__ import annotations

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
