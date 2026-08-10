"""Renderer facade: select the reference renderer or an optional native one,
honouring `--pure` and `WREATH_PURE` exactly like Wreath's other accelerators.

The native renderer is not built, and that is now a decision rather than a
pending one: rendering a client is a cold path reached from `wreath typegen`,
and `render_typescript` already assembles output linearly with a single join.
See its module docstring. The selection contract is kept because it costs
nothing and a future `_core.typegen_*` would drop in without touching callers --
but a build without one is not a gap, and `backend_name` reporting `"pure"` is
the only place that vocabulary still means anything here.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .._native import _core
from . import typescript_renderer as _reference

Renderer = Callable[[tuple[Any, ...], int], bytes]


def select_renderers(*, pure: bool = False) -> tuple[Renderer, Renderer, str]:
    """Return `(render_models, render_client, backend_name)`."""
    force_pure = pure or os.environ.get("WREATH_PURE") == "1"
    native_models = None if force_pure or _core is None else getattr(
        _core, "typegen_render_models", None
    )
    native_client = None if force_pure or _core is None else getattr(
        _core, "typegen_render_client", None
    )
    if native_models is not None and native_client is not None:
        return native_models, native_client, "native"
    return _reference.render_models, _reference.render_client, "pure"


__all__ = ["Renderer", "select_renderers"]
