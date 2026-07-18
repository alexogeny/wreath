"""Renderer facade: select the pure reference renderer or an optional native
one, honouring ``--pure`` and ``WREATH_PURE`` exactly like Wreath's other accelerators.

The native renderer is gated behind a benchmark decision and is not built today,
so this always resolves to the pure implementation. The selection contract is
kept explicit so a future ``_core.typegen_*`` drops in without touching callers.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .._native import _core
from .._pure import typegen as _pure_typegen

Renderer = Callable[[tuple[Any, ...], int], bytes]


def select_renderers(*, pure: bool = False) -> tuple[Renderer, Renderer, str]:
    """Return ``(render_models, render_client, backend_name)``."""
    force_pure = pure or os.environ.get("WREATH_PURE") == "1"
    native_models = None if force_pure or _core is None else getattr(
        _core, "typegen_render_models", None
    )
    native_client = None if force_pure or _core is None else getattr(
        _core, "typegen_render_client", None
    )
    if native_models is not None and native_client is not None:
        return native_models, native_client, "native"
    return _pure_typegen.render_models, _pure_typegen.render_client, "pure"


__all__ = ["Renderer", "select_renderers"]
