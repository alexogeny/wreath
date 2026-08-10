"""The one place that decides which compiled extension a facade gets.

Exposes every compiled extension, or ``None`` when this build has not got it.
Facade modules in ``wreath.*`` import from here.

**``_core`` is mandatory, and its absence is refused here, at import.** Not per
call, not per request, and not by degrading to something slower -- there is
nothing slower to degrade to. Routing, HTTP parsing, both codecs, headers,
validation and Cedar evaluation are C. A build without it is a broken build and
says so once, naming the fix. The other seven are optional, and each facade
raises a named error when asked for something the build has not got.

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
#: `_core` is required; the rest are optional and their facades each raise a
#: named error when asked for something the build has not got -- `wreath.reactor`
#: for `timers="wheel"`, `wreath.edge.headers` on import, `wreath.server` when
#: asked to serve. `_flight` is absent on Windows by construction (`setup.py`
#: gates it on POSIX), which is why telemetry degrades to off there rather than
#: failing.
_EXTENSIONS: frozenset[str] = frozenset(
    {"_core", "_client", "_postgres", "_server", "_reactor", "_edge", "_flight", "_http3"}
)

_MISSING_CORE = (
    "wreath._native._core is not built. Routing, HTTP parsing, the JSON and "
    "msgpack codecs, header handling, validation and Cedar evaluation are C, "
    "and there is nothing to run without it. Build it in place with\n\n"
    "    python setup.py build_ext --inplace\n\n"
    "or install a wheel (`pip install wreath`), which ships it prebuilt."
)


def extension(name: str) -> Any | None:
    """One compiled extension, or `None` when this build has not got it.

    Resolved per call rather than cached, which costs a `sys.modules` hit --
    `import_module` is a dict lookup after the first call -- and lets a test
    assign `wreath._native._client = None` and have that stick.

    An unknown `name` raises rather than returning `None`: a typo must not read
    as "this build has not got it".
    """
    if name not in _EXTENSIONS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        return importlib.import_module(f"wreath._native.{name}")
    except ImportError:
        return None


# Any-typed: the compiled module is invisible to static analysis.
#
# `_core` is the one eager load, and the one that refuses. Nearly every facade
# wants it, so the dlopen is paid once here rather than discovered as an
# `AttributeError` deep in a request. Everything else waits for a caller that
# means it -- `_client`'s module init alone imports `asyncio`, which brings
# `ssl`, `subprocess`, `logging`, `inspect` and `dataclasses` with it and
# measured at 74 ms of the 118 ms `import wreath._native._core` used to cost,
# charged to every subprocess and every xdist worker for a client most of them
# never open.
_core: Any = extension("_core")
if _core is None:  # pragma: no cover - a broken build cannot run the suite
    raise ImportError(_MISSING_CORE)


def __getattr__(name: str) -> Any:
    """Load one optional extension on first reference, then cache it.

    Caching by assignment into the module globals is what makes this a one-time
    cost: `__getattr__` runs only when normal attribute lookup fails, so the
    second reference never reaches here. It also means a test may assign
    `wreath._native._client = None` and have that stick, the way it did when
    these were plain module-level names.
    """
    module = extension(name)
    globals()[name] = module
    return module
