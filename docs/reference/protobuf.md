# `wreath.protobuf`

Protocol Buffers, declared as ordinary Python classes and compiled to a wire
plan once at startup. Reach for it when something on the other side of the wire
speaks protobuf — a mobile client counting bytes, a constrained device on a
metered link, an OTLP receiver, a service in another language whose contract is
a `.proto`.

The declaration is the contract: field numbers are written down rather than
inferred from order, so reordering a class can never quietly change the bytes.
Everything it refuses, it refuses at import — a declaration that cannot be
compiled is a startup error, not a surprise at the first request.

For the wider argument, the wire-format details it does and does not implement,
and why unknown fields are preserved rather than rejected, see
[the guide](../guides/protobuf.md).

::: wreath.protobuf
