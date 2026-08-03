"""Implementation of the generated admin. Use `wreath.admin`."""

from __future__ import annotations

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
