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
