"""API versioning by URL prefix (default) or ``Accept-Version`` negotiation.

Group per-version routes and mount them under ``/v1``, ``/v2``, ... composing
with wreath's ``Router``::

    from wreath.versioning import VersionedRouter

    api = VersionedRouter()
    v1 = api.version("1"); v1.get("/llamas")(list_llamas_v1)
    v2 = api.version("2"); v2.get("/llamas")(list_llamas_v2)
    app.include_router(api.router())          # -> /v1/llamas and /v2/llamas

Header negotiation (``Accept-Version: 2``) is opt-in via ``negotiate_version``;
full header-routed dispatch would need a middleware and is a deliberate follow-up.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from .router import Router

VERSION_ATTR = "__wreath_api_version__"

_T = TypeVar("_T")

__all__ = ["VERSION_ATTR", "VersionedRouter", "negotiate_version", "version"]


def version(tag: str) -> Callable[[_T], _T]:
    """Tag a handler or ``Router`` with an API version (readable metadata)."""

    def decorate(target: _T) -> _T:
        setattr(target, VERSION_ATTR, str(tag))
        return target

    return decorate


class VersionedRouter:
    """Collects per-version ``Router``s and mounts each under a version prefix.

    TODO: ``app.versioned(...)`` convenience wiring (``app.py`` owned by a
    concurrent fork); until then call ``.router()`` and ``include_router`` it.
    """

    __slots__ = ("_prefix_template", "_versions")

    def __init__(self, *, prefix_template: str = "/v{version}") -> None:
        self._prefix_template = prefix_template
        self._versions: dict[str, Router] = {}

    def version(self, tag: str) -> Router:
        """Return (creating if needed) the ``Router`` for version ``tag``."""
        key = str(tag)
        router = self._versions.get(key)
        if router is None:
            router = Router()
            self._versions[key] = router
        return router

    def add(self, tag: str, router: Router) -> None:
        """Register an already-built ``Router`` under version ``tag``."""
        self._versions[str(tag)] = router

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._versions))

    def mount(self, parent: Router) -> Router:
        """Include every version router into ``parent`` under its prefix."""
        for tag, router in sorted(self._versions.items()):
            parent.include_router(router, prefix=self._prefix_template.format(version=tag))
        return parent

    def router(self) -> Router:
        """A fresh ``Router`` with all versions mounted, ready to include."""
        return self.mount(Router())


def negotiate_version(
    request: Any, *, default: str, supported: Iterable[str]
) -> str:
    """Resolve the requested version from ``Accept-Version``, else ``default``.

    Returns ``default`` when the header is absent or names an unsupported version.
    """
    allowed = {str(item) for item in supported}
    header = ""
    try:
        header = (request.headers.get("accept-version", "") or "").strip()
    except Exception:  # a malformed/missing header object just means "no preference"
        header = ""
    return header if header in allowed else str(default)
