"""The one place that loads compiled extensions for Python facades.

The nine extensions built into every base wheel are required. Capability
extensions return ``None`` when their companion wheel is not installed or the
platform cannot contain them.

**``_core`` is mandatory, and its absence is refused here, at import.** Not per
call, not per request, and not by degrading to something slower -- there is
nothing slower to degrade to. Routing, HTTP parsing, both codecs, headers,
validation and Cedar evaluation are C. A build without it is a broken build and
says so once, naming the fix.

``wreath._pgdriver`` is the PostgreSQL driver's Python half, not a fallback:
``_native._postgres.Connection`` *subclasses* it, the C pipeline reads fifteen
module-level names out of it at init, and ``resolve_offsets`` resolves
``__slots__`` byte offsets against it. Deleting it stops the extension importing.
"""

from __future__ import annotations

import importlib
from typing import Any

#: Every extension `setup.py` builds. A name absent from here is a typo, not a
#: build without it.
#:
#: `_core`, `_client`, `_docs`, `_dupscan`, `_edge`, `_lint`, `_server`,
#: `_postgres`, and `_testrunner` are present in every base wheel. `_reactor` is installed by
#: `wreath[linux]`, `_http3` by
#: `wreath[h3]`/`wreath[http3]`, and `_flight` is platform-gated.
_EXTENSIONS: frozenset[str] = frozenset(
    {
        "_core",
        "_client",
        "_docs",
        "_dupscan",
        "_postgres",
        "_server",
        "_testrunner",
        "_reactor",
        "_edge",
        "_flight",
        "_http3",
        "_lint",
    }
)
_REQUIRED_EXTENSIONS = frozenset(
    {
        "_core",
        "_client",
        "_docs",
        "_dupscan",
        "_lint",
        "_postgres",
        "_server",
        "_edge",
        "_testrunner",
    }
)


def extension(name: str) -> Any | None:
    """Load one compiled extension.

    An unknown `name` raises rather than returning `None`: a typo must not read
    as "this build has not got it".
    """
    if name not in _EXTENSIONS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name = f"wreath._native.{name}"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        if name in _REQUIRED_EXTENSIONS:
            raise ImportError(
                f"{module_name} is missing from this installation; "
                "install a compiled Wreath wheel"
            ) from error
        return None


# Any-typed: the compiled module is invisible to static analysis.
# `_core` is the one eager load. Nearly every facade
# wants it, so the dlopen is paid once here rather than discovered as an
# `AttributeError` deep in a request. Everything else waits for a caller that
# means it -- `_client`'s module init alone imports `asyncio`, which brings
# `ssl`, `subprocess`, `logging`, `inspect` and `dataclasses` with it and
# measured at 74 ms of the 118 ms `import wreath._native._core` used to cost,
# charged to every subprocess and every xdist worker for a client most of them
# never open.
_core: Any = extension("_core")


def __getattr__(name: str) -> Any:
    """Load one optional extension on first reference, then cache it.

    Caching by assignment into the module globals is what makes this a one-time
    cost: `__getattr__` runs only when normal attribute lookup fails, so the
    second reference never reaches here.
    """
    module = extension(name)
    globals()[name] = module
    return module
