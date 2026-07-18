"""Explicit application-owned and request-owned state."""

from __future__ import annotations

from typing import Any

_MISSING = object()


class State:
    """A small attribute-accessible namespace with explicit ownership."""

    __slots__ = ("_values",)

    def __init__(self) -> None:
        object.__setattr__(self, "_values", {})

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self._values[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)

    def require(self, name: str) -> Any:
        value = self._values.get(name, _MISSING)
        if value is _MISSING:
            raise RuntimeError(f"required state value is not configured: {name}")
        return value
