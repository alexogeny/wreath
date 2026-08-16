# `wreath.port`

The `wreath port` application codemod: static analysis, whole-tree translation,
migration inventory, and explicit behavioural comparison. Analysis and
emission never import the source application; `wreath port --verify` is the
separate runtime step that intentionally imports two ASGI targets and drives the
same declared request corpus through both.

::: wreath.port
