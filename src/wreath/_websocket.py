"""WebSocket frame primitives (RFC 6455).

`parse_frame(buffer, offset=0)` returns `(fin, opcode, payload, consumed)` with
the payload already unmasked, or `None` when the buffer holds an incomplete
frame at that offset. `mask(data, key)` applies the 4-byte XOR mask (masking and unmasking
are the same operation). `build_frame(opcode, payload, fin=True,
mask_key=None)` serializes one frame; servers send unmasked, clients pass a
4-byte key.

`parse_frame` returning `None` for a short buffer rather than raising is what
lets a caller drive it straight from `data_received`: an incomplete frame is
the ordinary case on a stream transport, not an error, and raising on it would
route the common path through exception machinery. A frame that is malformed
*does* raise `ValueError`, so "not yet" and "never" stay distinguishable.

Masking is its own function because it is symmetric -- XOR with the key both
applies and removes it -- so the server unmasking inbound frames and a client
masking outbound ones share one implementation.
"""

from __future__ import annotations

from collections.abc import Callable

from ._native import _core

#: `ws_mask(data, key)` -- XOR with the 4-byte key, which both applies and
#: removes it.
mask: Callable[[bytes, bytes], bytes] = _core.ws_mask

#: `ws_parse_frame(data, offset=0)` -- `(fin, opcode, payload, consumed)`, or
#: `None` while the frame is incomplete. Raises `ValueError` on a malformed one.
parse_frame: Callable[..., tuple[bool, int, bytes, int] | None] = _core.ws_parse_frame

#: `ws_build_frame(opcode, payload, fin=True, mask_key=None)` -- one frame on
#: the wire.
build_frame: Callable[..., bytes] = _core.ws_build_frame

__all__ = ["build_frame", "mask", "parse_frame"]
