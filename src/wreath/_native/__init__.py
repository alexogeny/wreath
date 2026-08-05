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
_client: Any = None
if not os.environ.get("WREATH_PURE"):
    try:
        _core = importlib.import_module("wreath._native._core")
    except ImportError:
        _core = None
    try:
        _client = importlib.import_module("wreath._native._client")
    except ImportError:
        _client = None

# Loaded regardless of WREATH_PURE, unlike the two above: it backs the metal
# tier, which is native by definition and has no pure twin to fall back to, so
# gating it here would turn `timers="wheel"` into a failure under WREATH_PURE=1
# rather than leaving that choice to the caller. `wreath.reactor` raises a clear
# error when it is absent. Any-typed for the same reason as `_core`.
_reactor: Any = None
try:
    _reactor = importlib.import_module("wreath._native._reactor")
except ImportError:
    _reactor = None

# Same rule as `_reactor` directly above, for the same reason: `wreath.edge` is
# native-only by design (AGENTS.md), so there is no pure twin for WREATH_PURE=1
# to select and gating it here would only make the module unimportable in a mode
# that has nothing else to offer it. `wreath.edge.headers` raises a named error
# when it is absent.
_edge: Any = None
try:
    _edge = importlib.import_module("wreath._native._edge")
except ImportError:
    _edge = None
