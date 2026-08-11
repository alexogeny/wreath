"""Phase 1 declarative emitter (design 07 §3/§7).

Source-to-source translation by **pure `ast` + position-based text splicing** —
no `ast.unparse` (loses comments/formatting) and no third-party CST. The rule is
design 07's contract: **transpile declarations, copy logic**. Only declarative spans
(imports, class headers, decorators, parameter markers, exception constructors,
middleware registration) are rewritten in the original source text; every function
body is preserved byte-for-byte, with `# TODO(wreath-port: ...)` annotation lines
inserted above anything the analyzer tagged needs-review / unsupported (and above any
construct Phase 1 does not yet rewrite, e.g. ORM models — nothing is silently skipped).

Every emitted file is re-`ast.parse`d as a round-trip guard: a structurally broken
emit is a tool bug and raises rather than being written.
"""

from __future__ import annotations

from .module import EmitError, PortResult, emit_module, port_tree

__all__ = ["EmitError", "PortResult", "emit_module", "port_tree"]
