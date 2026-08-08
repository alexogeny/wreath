"""Explicit application-owned and request-owned state."""

from __future__ import annotations

from typing import Any

_MISSING = object()

#: Where a middleware parks a whole-body integrity check for `Request.body()`
#: and `Request.stream()` to spend. Named here rather than in either module
#: because both ends need the same string and neither imports the other:
#: `wreath.signatures` writes `(algorithm, digest)` when an RFC 9421 signature
#: covered a `Content-Digest`, and the body readers are the only place that can
#: check it -- global middleware runs before the body has arrived.
BODY_CHECK_SLOT = "_signature_body_digest"


class State:
    """A small attribute-accessible namespace with explicit ownership.

    Wreath keeps runtime state on an owner rather than in a module global, so
    what a value belongs to is visible from where it is read: `app.state` lives
    as long as the application, `request.state` only for one request.

        app.state.pool = await open_pool()          # at startup
        request.state.tenant = tenant               # per request

    Attributes are stored in a private dict, not on the instance, so any name is
    settable and reading one that was never set raises `AttributeError` naming
    it — the same shape as a plain object, which is what makes a typo look like
    a typo. Deleting an unset name raises `AttributeError` too.

    This is state, not configuration: nothing here is validated, typed, or
    persisted, and nothing is shared between processes. Configuration belongs in
    `wreath.config`.

    A request's `State` is created on first access rather than per request, so
    a request whose handler never touches it costs nothing. When the pipeline
    has classified the request it records `route_outcome` there, which is the
    one name a handler should not reuse.
    """

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
        """Return the value stored under `name`, or `default` when it is absent.

        The non-raising read, for a value that is genuinely optional. A stored
        `None` and an absent name are indistinguishable through this; use
        `require()` when absence is a bug.

        Returns:
            The stored value, or default.
        """
        return self._values.get(name, default)

    def require(self, name: str) -> Any:
        """Return the value stored under `name`, refusing to continue without it.

        This is the read for something a request cannot proceed without — a
        connection pool, a tenant resolved by middleware. It raises rather than
        returning `None`, so a missing dependency fails at the line that needed
        it, naming it, instead of surfacing later as an `AttributeError` on
        `None`. A stored `None` is a value and is returned as one.

        Returns:
            The stored value.

        Raises:
            RuntimeError: Nothing is stored under name.
        """
        value = self._values.get(name, _MISSING)
        if value is _MISSING:
            raise RuntimeError(f"required state value is not configured: {name}")
        return value
