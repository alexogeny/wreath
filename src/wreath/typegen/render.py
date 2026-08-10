"""The renderer typegen uses.

There is one, and this module is what is left of the seam that used to choose
between it and an optional C renderer. That renderer was never built, and its
absence is now a decision rather than a pending one: rendering a client is a cold
path reached from `wreath typegen`, run by a developer occasionally rather than
by a request, and `typescript_renderer` already assembles output linearly with a
single join. See its module docstring.

`select_renderers` survives its own selection because two callers read the
backend name it returns, and because a future `_core.typegen_*` would want
somewhere to land -- but a build without one is not a gap, and `"pure"` is no
longer the other half of anything.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import typescript_renderer as _reference

Renderer = Callable[[tuple[Any, ...], int], bytes]


def select_renderers() -> tuple[Renderer, Renderer, str]:
    """Return `(render_models, render_client, backend_name)`."""
    return _reference.render_models, _reference.render_client, "python"


__all__ = ["Renderer", "select_renderers"]
