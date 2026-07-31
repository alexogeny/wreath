# `wreath.grpc`

Serve gRPC methods over Wreath's native HTTP/2 server. A method is declared as
an ordinary async function and registered as a route, so `@roles`,
`@authorize`, `permissions=` and `dependencies=` mean here exactly what they
mean on a REST route and are enforced by the same middleware tape.

Two transport constraints decide whether this module is usable in a given
deployment, and both come from the server rather than from here: gRPC needs
HTTP/2 **response trailers**, which only Wreath's own server emits, and Wreath
negotiates HTTP/2 through ALPN, so gRPC requires **TLS** — a plaintext
`insecure_channel` cannot reach it.

For the call shapes, the status mapping, the deadline semantics and what is
deliberately not built — server reflection, a client, compression — see
[the guide](../guides/grpc.md).

::: wreath.grpc
