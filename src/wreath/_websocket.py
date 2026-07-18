"""WebSocket frame primitives (RFC 6455).

``parse_frame(buffer)`` returns ``(fin, opcode, payload, consumed)`` with the
payload already unmasked, or ``None`` when the buffer holds an incomplete
frame. ``mask(data, key)`` applies the 4-byte XOR mask (masking and unmasking
are the same operation). ``build_frame(opcode, payload, fin=True,
mask_key=None)`` serializes one frame; servers send unmasked, clients pass a
4-byte key.
"""

from __future__ import annotations

from ._native import _core

if _core is not None:
    mask = _core.ws_mask
    parse_frame = _core.ws_parse_frame
    build_frame = _core.ws_build_frame
else:
    from ._pure.ws import ws_build_frame as build_frame
    from ._pure.ws import ws_mask as mask
    from ._pure.ws import ws_parse_frame as parse_frame

__all__ = ["build_frame", "mask", "parse_frame"]
