# `wreath.service_client`

An auth-aware wrapper around `wreath.http_client.HTTPClient` for calling another
service. It binds a base path and a token source once, so the call sites read
like the API being called rather than like plumbing.

The token may be a plain `str`, a zero-arg async callable, or a
`ClientCredentials` that caches and renews the machine-to-machine token before
it expires — `ServiceClient` asks for the current one per request either way.
The response is whatever the wrapped client returns, which for `HTTPClient` is a
`ClientResponse` holding an undecoded `bytes` body. The guide is
[Calling other services](../guides/service-client.md).

::: wreath.service_client
