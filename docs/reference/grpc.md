# `wreath.grpc`

Serve gRPC methods over Wreath's native HTTP/2 server. A method is declared as
an ordinary async function and registered as a route, so `@roles`,
`@authorize`, `permissions=` and `dependencies=` mean here exactly what they
mean on a REST route and are enforced by the same middleware tape. `action=` on
a method decorator is `@authorize` written at the declaration, which is the
spelling `@mcp.tool(action=…)` uses — one authorization vocabulary across every
protocol Wreath serves.

Two transport constraints decide whether this module is usable in a given
deployment, and both come from the server rather than from here: gRPC needs
HTTP/2 **response trailers**, which only Wreath's own server emits, and Wreath
negotiates HTTP/2 through ALPN, so gRPC requires **TLS** — a plaintext
`insecure_channel` cannot reach it.

Message compression is `identity` and `gzip`, negotiated per call through
`grpc-encoding` / `grpc-accept-encoding` and applied per message. A decompressed
message is bounded by `max_message_bytes` a second time, because the wire length
says nothing about the decoded one.

For the call shapes, the status mapping, the deadline semantics and what is
deliberately not built — server reflection, a client, codings beyond gzip — see
[the guide](../guides/grpc.md).

::: wreath.grpc
