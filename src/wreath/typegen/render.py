"""Select the deterministic Python renderer used by type generation.

Client rendering is a cold developer-tool path. `typescript_renderer` assembles
output linearly with a single join, and `select_renderers` returns it with the
backend name consumed by the CLI and generated-file metadata.
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
