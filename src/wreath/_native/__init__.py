"""Loader for the optional C accelerator.

Exposes ``_core`` (the compiled module) or ``None`` when it is unavailable
or WREATH_PURE=1 requests the pure-Python twins. Facade modules in ``wreath.*``
import ``_core`` from here and fall back to ``wreath._pure``.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

# Any-typed: the compiled module is invisible to static analysis, and callers
# guard on ``_core is None`` before touching its attributes. The explicit
# import_module avoids ``from . import _core`` resolving to this attribute
# instead of the compiled submodule.
_core: Any = None
if not os.environ.get("WREATH_PURE"):
    try:
        _core = importlib.import_module("wreath._native._core")
    except ImportError:
        _core = None

#: The extensions loaded on first use rather than on import, mapped to whether
#: WREATH_PURE=1 suppresses them.
#:
#: `_core` above stays eager because nearly every facade wants it and it costs a
#: dlopen. These three do not: `_client`'s module init imports `asyncio`, which
#: brings `ssl`, `subprocess`, `logging`, `inspect` and `dataclasses` with it and
#: measured at 74 ms of the 118 ms `import wreath._native._core` used to cost --
#: charged to every subprocess and every xdist worker, for a client most of them
#: never open. A module `__getattr__` keeps `from wreath._native import _client`
#: working unchanged and defers the cost to the first caller that means it.
#:
#: `_reactor` and `_edge` load regardless of WREATH_PURE, unlike `_core` and
#: `_client`: they back the metal tier and the reverse proxy, both native by
#: definition with no pure twin to fall back to (AGENTS.md), so gating them would
#: turn `timers="wheel"` and `import wreath.edge` into failures in a mode that has
#: nothing else to offer them. `wreath.reactor` and `wreath.edge.headers` each
#: raise a named error when their extension is absent.
_LAZY: dict[str, bool] = {"_client": True, "_reactor": False, "_edge": False}


def __getattr__(name: str) -> Any:
    """Load one optional extension on first reference, then cache it.

    Caching by assignment into the module globals is what makes this a one-time
    cost: `__getattr__` runs only when normal attribute lookup fails, so the
    second reference never reaches here. It also means a test may assign
    `wreath._native._client = None` and have that stick, the way it did when
    these were plain module-level names.
    """
    honours_pure = _LAZY.get(name)
    if honours_pure is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module: Any = None
    if not (honours_pure and os.environ.get("WREATH_PURE")):
        try:
            module = importlib.import_module(f"wreath._native.{name}")
        except ImportError:
            module = None
    globals()[name] = module
    return module
