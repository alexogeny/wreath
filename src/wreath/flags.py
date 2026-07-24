"""Feature flags with a small, dependency-free rule language.

A flag's value (from ``WREATH_FLAG_<NAME>`` env vars or an explicit mapping) is
evaluated per request:

- ``on``/``true``/``1``/``yes`` -> enabled; ``off``/``false``/``0``/``""`` -> disabled
- ``"25%"`` -> a *deterministic* percentage rollout, bucketed by a stable subject
  from the context (``id``/``key``/``user``), so a given subject is consistently
  in or out (no ``random``, so it survives replay and multiple workers).

The provider is a ``Protocol`` -- env/mapping now, external providers (Unleash,
LaunchDarkly, ...) are future adapters implementing the same surface.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from .config import read_osenv

FLAG_PREFIX = "WREATH_FLAG_"

_TRUE = frozenset({"on", "true", "1", "yes", "enabled"})
_FALSE = frozenset({"off", "false", "0", "no", "disabled", ""})

__all__ = ["FLAG_PREFIX", "FeatureFlags", "FlagProvider", "FlagView"]


@runtime_checkable
class FlagProvider(Protocol):
    """Anything that can answer whether a flag is on for a context."""

    def enabled(self, name: str, context: Mapping[str, Any] | None = None) -> bool: ...


def _subject(context: Mapping[str, Any] | None) -> str:
    if not context:
        return ""
    for key in ("id", "key", "user", "subject"):
        value = context.get(key)
        if value:
            return str(value)
    return ""


def _bucket(name: str, subject: str) -> int:
    digest = hashlib.blake2s(f"{name}:{subject}".encode()).hexdigest()[:8]
    return int(digest, 16) % 100


def evaluate_rule(value: str, name: str, context: Mapping[str, Any] | None = None) -> bool:
    """Evaluate one flag value against the rule language (see module docstring)."""
    token = value.strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    if token.endswith("%"):
        try:
            percent = float(token[:-1])
        except ValueError:
            return False
        return _bucket(name, _subject(context)) < percent
    return False


class FeatureFlags:
    """A mapping-backed ``FlagProvider`` (env-sourced or explicit)."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = {key.lower(): str(val) for key, val in (values or {}).items()}

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FeatureFlags":
        """Collect ``WREATH_FLAG_<NAME>`` entries from the environment."""
        env = environ if environ is not None else read_osenv()
        values = {
            key[len(FLAG_PREFIX) :]: val
            for key, val in env.items()
            if key.startswith(FLAG_PREFIX)
        }
        return cls(values)

    def enabled(self, name: str, context: Mapping[str, Any] | None = None) -> bool:
        raw = self._values.get(name.lower())
        if raw is None:
            return False
        return evaluate_rule(raw, name.lower(), context)

    def all(self, context: Mapping[str, Any] | None = None) -> dict[str, bool]:
        return {name: evaluate_rule(raw, name, context) for name, raw in self._values.items()}

    def view(self, context: Mapping[str, Any] | None = None) -> "FlagView":
        return FlagView(self, context)


class FlagView:
    """A context-bound view of a provider -- what a request handler receives."""

    __slots__ = ("_context", "_provider")

    def __init__(self, provider: FlagProvider, context: Mapping[str, Any] | None = None) -> None:
        self._provider = provider
        self._context = context

    def enabled(self, name: str) -> bool:
        return self._provider.enabled(name, self._context)

    __contains__ = enabled


def flags_dependency(provider: FlagProvider):
    """Build a ``Depends``-able yielding a request-scoped :class:`FlagView`.

    The context is taken from ``request.identity`` when present (so percentage
    rollouts bucket per authenticated principal). TODO: ``app.flags()``
    convenience wiring (``app.py`` owned by a concurrent fork).
    """
    from .request import Request

    def _dependency(request: Request) -> FlagView:
        identity = getattr(request, "identity", None)
        context: dict[str, Any] = {}
        if identity is not None:
            subject = getattr(identity, "id", None)
            if subject is not None:
                context["id"] = subject
        return FlagView(provider, context)

    return _dependency
