"""API versioning by URL prefix (default) or `Accept-Version` negotiation.

Group per-version routes and mount them under `/v1`, `/v2`, ... composing
with wreath's `Router`:

```python
from wreath.versioning import VersionedRouter

api = VersionedRouter()
v1 = api.version("1"); v1.get("/llamas")(list_llamas_v1)
v2 = api.version("2"); v2.get("/llamas")(list_llamas_v2)
app.include_router(api.router())          # -> /v1/llamas and /v2/llamas
```

Prefix versioning is the routed form and the default: the version is a path
segment, so an unknown one matches no route and the app answers its ordinary 404,
which -- like every framework error -- is RFC 9457 `application/problem+json`,
not `{"detail": ...}`.

Header negotiation (`Accept-Version: 2`) is opt-in via `negotiate_version`,
and it resolves a version *string* for a handler to branch on. It does not
dispatch, and it never refuses: an absent, blank, or unsupported header resolves
to the caller's `default`. Full header-routed dispatch would need a middleware
and is a deliberate follow-up.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from .router import Router

VERSION_ATTR = "__wreath_api_version__"

_T = TypeVar("_T")

__all__ = ["VERSION_ATTR", "VersionedRouter", "negotiate_version", "version"]


def version(tag: str) -> Callable[[_T], _T]:
    """Tag a handler or `Router` with an API version (readable metadata).

    The tag is stored under `VERSION_ATTR` and stringified. It is metadata only:
    nothing in routing or dispatch reads it, so tagging a handler does not mount it
    anywhere. Use it to inspect or group what you already mounted.
    """

    def decorate(target: _T) -> _T:
        setattr(target, VERSION_ATTR, str(tag))
        return target

    return decorate


class VersionedRouter:
    """Collects per-version `Router`s and mounts each under a version prefix.

    There is no `app.versioned(...)` convenience method, and that is the current
    limitation: build the per-version routers here, then mount them yourself with
    `app.include_router(api.router())`.

    Mounting copies routes into the parent under a prefix, so it is a one-time
    startup act -- adding a route to a version router *after* mounting does not
    reach the app.

    Args:
        prefix_template: format string for each mount point, given `version`
    """

    __slots__ = ("_prefix_template", "_versions")

    def __init__(self, *, prefix_template: str = "/v{version}") -> None:
        self._prefix_template = prefix_template
        self._versions: dict[str, Router] = {}

    def version(self, tag: str) -> Router:
        """Return (creating if needed) the `Router` for version `tag`.

        Repeated calls with the same tag return the same router, so several modules
        can add routes to one version without coordinating who created it.
        """
        key = str(tag)
        router = self._versions.get(key)
        if router is None:
            router = Router()
            self._versions[key] = router
        return router

    def add(self, tag: str, router: Router) -> None:
        """Register an already-built `Router` under version `tag`.

        Replaces, without warning, any router already registered for that tag.
        """
        self._versions[str(tag)] = router

    @property
    def versions(self) -> tuple[str, ...]:
        """The registered tags, sorted as strings -- so `"10"` precedes `"2"`."""
        return tuple(sorted(self._versions))

    def mount(self, parent: Router) -> Router:
        """Include every version router into `parent` under its prefix.

        Mounts in sorted tag order and returns `parent`, so the call chains onto
        a router that already carries a prefix of its own.
        """
        for tag, router in sorted(self._versions.items()):
            parent.include_router(router, prefix=self._prefix_template.format(version=tag))
        return parent

    def router(self) -> Router:
        """A fresh `Router` with all versions mounted, ready to include.

        Call this after every version's routes are registered; a route added to a
        version afterwards does not reach the router this returned.
        """
        return self.mount(Router())


def negotiate_version(
    request: Any, *, default: str, supported: Iterable[str]
) -> str:
    """Resolve the requested version from `Accept-Version`, else `default`.

    This never refuses. An absent header, a blank one, a version outside
    `supported`, and an object with no `header` accessor all resolve to
    `default`; there is no 406 and no problem response. A handler that must
    reject an unknown version compares the raw header itself and raises. Nor is
    `default` validated -- it is returned stringified whether or not it appears
    in `supported`.

    Reads through `Request.header()`, which is the accessor. `Request.headers`
    is the raw `list[tuple[bytes, bytes]]` and has no `.get` -- this used to
    call `request.headers.get(...)` inside a blanket `except Exception`, so
    every real request raised `AttributeError`, was swallowed, and got
    `default` back. Negotiation never happened; only a dict-shaped test double
    ever exercised the path. Guarding for the accessor rather than catching is
    what makes that visible instead of silent.

    Args:
        supported: the version tags this endpoint serves, compared as strings

    Returns:
        the negotiated tag, or `default` when the header names none of them
    """
    allowed = {str(item) for item in supported}
    read = getattr(request, "header", None)
    raw = read("accept-version", "") if callable(read) else ""
    header = raw.strip() if isinstance(raw, str) else ""
    return header if header in allowed else str(default)
